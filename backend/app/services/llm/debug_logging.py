import json
import logging

from backend.app.core.config import settings
from backend.app.services.llm_observability import project_content

logger = logging.getLogger("paper_grading.llm")

SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "openai_api_key",
    "openai_compatible_api_key",
    "password",
    "secret",
    "token",
}


def log_llm_request(provider, url, headers, payload, attempt, attempts):
    if not _debug_logging_allowed():
        return
    _log(
        "LLM request",
        {
            "provider": provider,
            "attempt": attempt + 1,
            "attempts": attempts,
            "method": "POST",
            "url": url,
            "headers": _sanitize(headers),
            "payload": project_content(_sanitize(payload)),
        },
    )


def log_llm_response(provider, response, elapsed_ms, attempt, attempts):
    if not _debug_logging_allowed():
        return
    _log(
        "LLM response",
        {
            "provider": provider,
            "attempt": attempt + 1,
            "attempts": attempts,
            "status_code": getattr(response, "status_code", None),
            "elapsed_ms": round(elapsed_ms, 2),
            "headers": _sanitize(dict(getattr(response, "headers", {}) or {})),
            "body": project_content(_response_text(response)),
        },
    )


def log_llm_exception(provider, exc, attempt, attempts):
    if not _debug_logging_allowed():
        return
    _log(
        "LLM exception",
        {
            "provider": provider,
            "attempt": attempt + 1,
            "attempts": attempts,
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        },
        level=logging.WARNING,
    )


def log_llm_retry_sleep(provider, delay_seconds, attempt, attempts, reason):
    if not _debug_logging_allowed():
        return
    _log(
        "LLM retry sleep",
        {
            "provider": provider,
            "attempt": attempt + 1,
            "attempts": attempts,
            "delay_seconds": round(delay_seconds, 2),
            "reason": reason,
        },
    )


def log_llm_throttle_sleep(provider, delay_seconds):
    if not _debug_logging_allowed():
        return
    _log(
        "LLM throttle sleep",
        {
            "provider": provider,
            "delay_seconds": round(delay_seconds, 2),
            "reason": "client_side_rate_limit",
        },
    )


def _log(label, payload, level=logging.INFO):
    logger.log(level, "[%s] %s", label, _to_log_text(payload))


def _to_log_text(payload):
    text = json.dumps(payload, ensure_ascii=False, default=str, indent=2)
    text = _redact_configured_secrets(text)
    max_chars = max(1000, settings.LLM_DEBUG_LOG_MAX_CHARS)
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return "%s\n...<truncated %d chars>" % (text[:max_chars], omitted)


def _debug_logging_allowed():
    # Settings construction already rejects this combination.  Retain the
    # runtime guard for tests, hot configuration and defense in depth.
    return bool(settings.LLM_DEBUG_LOG_ENABLED and not settings.AUTH_ENABLED)


def _redact_configured_secrets(text):
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "LOCAL_LLM_API_KEY",
        "GOOGLE_SHEETS_WEBAPP_SECRET",
        "AUTH_PASSWORD",
        "AUTH_SECRET",
    ):
        value = getattr(settings, name, None)
        if value and len(str(value)) >= 4:
            text = text.replace(str(value), "***REDACTED***")
    return text


def _sanitize(value):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                sanitized[key] = "***REDACTED***"
            else:
                sanitized[key] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _is_sensitive_key(key):
    normalized = str(key).lower().replace("-", "_")
    return normalized in SENSITIVE_KEYS or normalized.endswith("_secret") or normalized.endswith("_token")


def _response_text(response):
    try:
        return response.text
    except Exception as exc:
        return "<failed to read response body: %s>" % exc
