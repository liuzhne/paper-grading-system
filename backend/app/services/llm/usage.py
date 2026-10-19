"""Billing-relevant provider usage counters (counts only, never content).

Each adapter instance owns one ``UsageMeter``.  A scorer can be shared by
several papers (synchronous batch scoring reuses one HTTP client), so callers
attribute usage to a run by taking a snapshot before and a delta after.

除了计费用的输入、输出，还记模型实际做了什么：有没有思考（推理 token）、有没有因输出
上限被截断。运行身份里的「思考关闭」只是声明，这两项是观测；两者不一致时
``ThinkingConsistencyGuard`` 让本篇失败（docs/评分输出预算与截断恢复改造方案.md P1）。
"""

from __future__ import annotations

import threading


USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "request_count",
    "failure_count",
    # 推理 token：厂商报告的，或由「合计 − 输入 − 可见输出」推算的。
    "reasoning_tokens",
    # 观测到思考的应答数；其中推算得来（厂商没单独报告）的另计一份。
    "reasoning_requests",
    "inferred_reasoning_requests",
    # 因输出上限结束的应答数（finish_reason=length / max_tokens / max_output_tokens）。
    "truncated_count",
)

REASONING_REPORTED = "reported"
REASONING_INFERRED = "inferred"
THINKING_NOT_DISABLED = "PROVIDER_THINKING_NOT_DISABLED"


class TokenBudgetExceededError(RuntimeError):
    """The per-paper input-token cap is spent; the request was never sent."""

    code = "TOKEN_BUDGET_EXCEEDED"

    def __init__(self, *, used: int, cap: int):
        self.used = int(used)
        self.cap = int(cap)
        super().__init__(
            "paper input-token budget exhausted (used=%d, cap=%d)" % (self.used, self.cap)
        )


class ThinkingNotDisabledError(RuntimeError):
    """运行身份声明不思考，模型却在思考：本次及之后的调用都不算数。

    ``sent`` 区分两种情形：这次请求已经发出、应答里观测到了思考（判定不采用）；
    或者之前已经观测到，这次请求根本没发。
    """

    code = THINKING_NOT_DISABLED

    def __init__(self, *, sent: bool):
        self.sent = bool(sent)
        super().__init__(
            "provider reasoned although the run identity disables thinking (sent=%s)"
            % ("yes" if self.sent else "no")
        )


def _count(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _optional_count(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def observed_usage(
    *,
    prompt,
    completion,
    total,
    reported_reasoning=None,
    thinking_seen=False,
    finish_reason=None,
    truncated=False,
) -> dict:
    """一次应答的中性用量与行为观测（只有计数和结束原因，不含任何内容）。

    推理 token 优先用厂商报告的值。厂商不报时用「合计 − 输入 − 可见输出」推算：
    Vertex 的 OpenAI 兼容接口把思考计入合计、不计入 ``completion_tokens``，2026-10-16
    那次事故就是这样看出来的。推算值标为 ``inferred``，守卫对它更谨慎。
    ``thinking_seen`` 用于只给思考块、不单独计数的协议（Claude）。
    """

    reasoning = _optional_count(reported_reasoning)
    source = REASONING_REPORTED if reasoning else None
    if reasoning is None:
        counts = [_optional_count(value) for value in (prompt, completion, total)]
        if None not in counts and counts[2] > counts[0] + counts[1]:
            reasoning = counts[2] - counts[0] - counts[1]
            source = REASONING_INFERRED
    if source is None and thinking_seen:
        source = REASONING_REPORTED
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "reasoning_tokens": reasoning or 0,
        "reasoning_source": source,
        "finish_reason": None if finish_reason is None else str(finish_reason),
        "truncated": bool(truncated),
    }


def zero_usage() -> dict:
    return dict.fromkeys(USAGE_FIELDS, 0)


class UsageMeter:
    """Thread-safe running totals for one adapter instance."""

    def __init__(self):
        self._lock = threading.Lock()
        self._totals = zero_usage()

    def record_success(self, usage) -> None:
        """Count one answered request with the provider-reported usage."""

        usage = usage if isinstance(usage, dict) else {}
        prompt = _count(usage.get("prompt_tokens"))
        completion = _count(usage.get("completion_tokens"))
        total = _count(usage.get("total_tokens")) or prompt + completion
        source = usage.get("reasoning_source")
        with self._lock:
            self._totals["prompt_tokens"] += prompt
            self._totals["completion_tokens"] += completion
            self._totals["total_tokens"] += total
            self._totals["request_count"] += 1
            self._totals["reasoning_tokens"] += _count(usage.get("reasoning_tokens"))
            if source in (REASONING_REPORTED, REASONING_INFERRED):
                self._totals["reasoning_requests"] += 1
            if source == REASONING_INFERRED:
                self._totals["inferred_reasoning_requests"] += 1
            if usage.get("truncated"):
                self._totals["truncated_count"] += 1

    def record_failure(self) -> None:
        """Count one request that produced no usable answer (HTTP or transport)."""

        with self._lock:
            self._totals["request_count"] += 1
            self._totals["failure_count"] += 1

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._totals)


def scorer_usage_snapshot(scorer) -> dict | None:
    meter = getattr(scorer, "usage_meter", None)
    if not isinstance(meter, UsageMeter):
        return None
    return meter.snapshot()


def usage_delta(after, before) -> dict:
    """Usage between two snapshots; a missing snapshot counts as zero."""

    after = after or zero_usage()
    before = before or zero_usage()
    return {
        field: max(0, _count(after.get(field)) - _count(before.get(field)))
        for field in USAGE_FIELDS
    }


def thinking_claimed_off(thinking) -> bool:
    """运行身份是否声明了不思考（``thinking.enabled`` 明确为 false）。"""

    return isinstance(thinking, dict) and thinking.get("enabled") is False


class ThinkingConsistencyGuard:
    """核对运行身份里的「思考关闭」与模型的实际行为，一篇论文一个实例。

    - 厂商报告了推理 token、或应答里有思考块：立即判定不一致；
    - 只有推算值：连续两次应答都推算出推理 token 才判定（个别厂商的合计口径可能
      含别的计数，一次不足为凭）；中间有一次没有推理的应答就重新计数。
    判定之后，本篇剩下的调用一律不发（``before_call`` 抛错），不再花钱。
    """

    INFERRED_STREAK = 2

    def __init__(self):
        self.tripped = False
        self._inferred_streak = 0

    def before_call(self, thinking) -> None:
        if self.tripped and thinking_claimed_off(thinking):
            raise ThinkingNotDisabledError(sent=False)

    def after_call(self, thinking, delta) -> bool:
        """这次调用是否暴露了不一致；``delta`` 为本次调用的用量差（无计量时为 None）。"""

        if not thinking_claimed_off(thinking) or not delta:
            return False
        answered = _count(delta.get("request_count")) - _count(delta.get("failure_count"))
        reasoning = _count(delta.get("reasoning_requests"))
        inferred = _count(delta.get("inferred_reasoning_requests"))
        if reasoning > inferred:
            self.tripped = True
        elif inferred:
            self._inferred_streak += inferred
            self.tripped = self._inferred_streak >= self.INFERRED_STREAK
        elif answered > 0:
            self._inferred_streak = 0
        return self.tripped


def metered_call(scorer, call, *, guard=None, thinking=None, before_send=None, on_usage=None):
    """发一次模型调用：先做发送前检查，再按用量差核对思考，返回调用结果。

    ``before_send(snapshot)`` 是调用方自己的发送前检查（例如单篇输入上限），在思考守卫之前。
    ``on_usage(delta)`` 收到本次调用的用量差（没有发出请求时为 None），成功、失败都会收到。
    调用本身抛错时照样计算用量差、照样核对：截断恰恰是思考耗尽额度最常见的样子，
    这时要报告真正的原因，而不是截断。
    """

    if on_usage is not None:
        on_usage(None)
    before = scorer_usage_snapshot(scorer)
    try:
        if before_send is not None:
            before_send(before)
        if guard is not None:
            guard.before_call(thinking)
    except Exception:
        # 请求没有发出：用量差确定为零（而不是「无计量」），规则任务据此记 0 次尝试。
        if on_usage is not None:
            on_usage(zero_usage())
        raise
    failure = None
    result = None
    try:
        result = call()
    except Exception as exc:  # noqa: BLE001 - re-raised below, after the usage check
        failure = exc
    after = scorer_usage_snapshot(scorer)
    delta = None if before is None or after is None else usage_delta(after, before)
    if on_usage is not None:
        on_usage(delta)
    if guard is not None and guard.after_call(thinking, delta):
        raise ThinkingNotDisabledError(sent=True) from failure
    if failure is not None:
        raise failure
    return result


__all__ = [
    "REASONING_INFERRED",
    "REASONING_REPORTED",
    "THINKING_NOT_DISABLED",
    "ThinkingConsistencyGuard",
    "ThinkingNotDisabledError",
    "TokenBudgetExceededError",
    "USAGE_FIELDS",
    "UsageMeter",
    "metered_call",
    "observed_usage",
    "scorer_usage_snapshot",
    "thinking_claimed_off",
    "usage_delta",
    "zero_usage",
]
