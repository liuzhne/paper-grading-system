"""Billing-relevant provider usage counters (counts only, never content).

Each adapter instance owns one ``UsageMeter``.  A scorer can be shared by
several papers (synchronous batch scoring reuses one HTTP client), so callers
attribute usage to a run by taking a snapshot before and a delta after.
"""

from __future__ import annotations

import threading


USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "request_count",
    "failure_count",
)


class TokenBudgetExceededError(RuntimeError):
    """The per-paper input-token cap is spent; the request was never sent."""

    code = "TOKEN_BUDGET_EXCEEDED"

    def __init__(self, *, used: int, cap: int):
        self.used = int(used)
        self.cap = int(cap)
        super().__init__(
            "paper input-token budget exhausted (used=%d, cap=%d)" % (self.used, self.cap)
        )


def _count(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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
        with self._lock:
            self._totals["prompt_tokens"] += prompt
            self._totals["completion_tokens"] += completion
            self._totals["total_tokens"] += total
            self._totals["request_count"] += 1

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


__all__ = [
    "TokenBudgetExceededError",
    "USAGE_FIELDS",
    "UsageMeter",
    "scorer_usage_snapshot",
    "usage_delta",
    "zero_usage",
]
