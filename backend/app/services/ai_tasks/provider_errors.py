"""模型调用失败 → 条目的处理方式（方案第 6.4 节），各任务种类共用。

各操作把厂商异常包成自己的错误码（``AI_DRAFT_PROVIDER_ERROR``、``AI_PROVIDER_ERROR``……），
原始的 ``ProviderCallError`` / ``CircuitOpenError`` 留在异常链上；这里沿链找到它，按
厂商错误码决定延后、再执行一次还是立即失败。
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from email.utils import parsedate_to_datetime

from backend.app.core.config import settings
from backend.app.services.ai_tasks.errors import AITaskItemError
from backend.app.services.llm.errors import ProviderCallError
from backend.app.services.llm.rate_limit import CircuitOpenError


# 超时、5xx、网络中断：再执行 1 次。
TRANSIENT_PROVIDER_CODES = {
    "request_timeout",
    "provider_unavailable",
    "network_error",
    "capacity_unavailable",
    "unknown",
}


def cause_chain(exc):
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def retry_after_seconds(value):
    if value is None:
        return None
    text = str(value).strip()
    try:
        return max(0, int(float(text)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((when - datetime.now(timezone.utc)).total_seconds()))


def provider_item_error(exc, *, code, message, hints=None) -> AITaskItemError:
    """``hints`` 按厂商错误码给出更具体的提示（例如额度耗尽要充值）。"""

    for cause in cause_chain(exc):
        if isinstance(cause, CircuitOpenError):
            return AITaskItemError(
                code,
                message,
                disposition="defer",
                retry_after_seconds=settings.PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
            )
        if isinstance(cause, ProviderCallError):
            provider_code = cause.error.code
            text = (hints or {}).get(provider_code, message)
            if provider_code == "rate_limited":
                return AITaskItemError(
                    code,
                    text,
                    disposition="defer",
                    retry_after_seconds=retry_after_seconds(cause.error.retry_after),
                )
            if provider_code == "quota_exhausted":
                # 等待不会恢复：不调低、不重试。
                return AITaskItemError(code, text, disposition="fail")
            if provider_code in TRANSIENT_PROVIDER_CODES:
                return AITaskItemError(code, text, disposition="retry")
            return AITaskItemError(code, text, disposition="fail")
    # 成功状态里的错误响应、未分类的服务异常：按超时/5xx 处理，再试一次。
    return AITaskItemError(code, message, disposition="retry")


__all__ = [
    "TRANSIENT_PROVIDER_CODES",
    "cause_chain",
    "provider_item_error",
    "retry_after_seconds",
]
