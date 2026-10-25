from datetime import datetime
from datetime import timezone
from email.utils import parsedate_to_datetime
import time

import httpx

from backend.app.core.config import settings
from backend.app.services.llm.errors import project_provider_error

# 重试之后至少还要留这么久给那次调用，否则重试只会在截止时间被平台或预算截断。
MIN_SECONDS_FOR_RETRY_CALL = 30.0


def deadline_timeout(deadline, timeout_seconds):
    """有截止时间（time.monotonic）时，本次调用的超时取配置值与剩余时间中较小者。

    返回 (timeout, expired)。expired 为真表示已经没有剩余时间，不应再发请求。
    """

    if deadline is None:
        return timeout_seconds, False
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0.0, True
    return (remaining if timeout_seconds is None else min(float(timeout_seconds), remaining)), False


def retry_fits_before_deadline(deadline, delay_seconds, min_call_seconds=None) -> bool:
    """等待 delay 秒之后，剩余时间还够不够再做一次有意义的调用。

    ``min_call_seconds`` 默认 30 秒（评分调用）；评分助手的一步截止时间只有 20 秒，传更小的值，
    否则它的 429 短重试永远不会发生（A1 第三轮评测：10 次 429 全是 attempt=1/2 retry=no）。
    """

    if deadline is None:
        return True
    needed = MIN_SECONDS_FOR_RETRY_CALL if min_call_seconds is None else float(min_call_seconds)
    return deadline - time.monotonic() - float(delay_seconds) >= needed


def is_retryable_http_error(exc):
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    return project_provider_error(exc).retryable


def retry_delay_seconds(exc, attempt):
    retry_after = _retry_after_seconds(getattr(exc.response, "headers", {}).get("retry-after"))
    if retry_after is not None:
        return _cap_delay(retry_after)

    if exc.response.status_code == 429:
        return _cap_delay(settings.LLM_429_RETRY_DELAY_SECONDS * (attempt + 1))

    return exponential_delay_seconds(attempt)


def exponential_delay_seconds(attempt):
    return _cap_delay(settings.LLM_RETRY_BASE_DELAY_SECONDS * (2**attempt))


def retry_reason(exc):
    if isinstance(exc, (httpx.HTTPStatusError, httpx.TransportError)):
        projected = project_provider_error(exc)
        if projected.http_status is not None:
            return "%s_%s" % (projected.code, projected.http_status)
        return projected.code
    return exc.__class__.__name__


def retry_after_seconds(exc) -> float | None:
    """响应头 Retry-After 要求的等待（秒）；没有或解析不了返回 None。"""

    response = getattr(exc, "response", None)
    return _retry_after_seconds(getattr(response, "headers", {}).get("retry-after"))


def _retry_after_seconds(value):
    if not value:
        return None
    value = str(value).strip()
    try:
        return max(float(value), 0)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        # RFC 7231: HTTP-date 始终为 GMT，naive 值按 UTC 处理（而非本地时区）。
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max((retry_at - datetime.now(retry_at.tzinfo)).total_seconds(), 0)


def _cap_delay(delay):
    return max(0, min(float(delay), float(settings.LLM_RETRY_MAX_DELAY_SECONDS)))
