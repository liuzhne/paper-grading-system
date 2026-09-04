from datetime import datetime
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx

from backend.app.core.config import settings
from backend.app.services.llm.errors import project_provider_error

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
