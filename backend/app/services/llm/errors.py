"""Stable, provider-neutral LLM failure projection.

Only allowlisted and bounded provider metadata may cross this boundary.  Raw
request payloads, response bodies and credentials deliberately remain on the
original exception chain and are never included in ``ProviderError`` text.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx


class ProviderErrorCode(str, Enum):
    CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded"
    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    MODEL_OR_ENDPOINT_NOT_FOUND = "model_or_endpoint_not_found"
    REQUEST_TIMEOUT = "request_timeout"
    CONFLICT = "conflict"
    REQUEST_TOO_LARGE = "request_too_large"
    UNPROCESSABLE_REQUEST = "unprocessable_request"
    RATE_LIMITED = "rate_limited"
    CAPACITY_UNAVAILABLE = "capacity_unavailable"
    CANCELED = "canceled"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NETWORK_ERROR = "network_error"
    UNKNOWN = "unknown"


class ProviderErrorScope(str, Enum):
    REQUEST = "request"
    CONNECTION = "connection"
    PROVIDER = "provider"
    NETWORK = "network"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderError:
    schema_version: str
    code: str
    scope: str
    retryable: bool
    reducible: bool
    http_status: int | None
    provider_error_type: str | None
    provider_error_code: str | None
    message: str
    provider_request_id: str | None
    retry_after: str | None
    rate_limit_headers: dict[str, str]

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


class ProviderCallError(RuntimeError):
    """Safe public exception retaining the original failure through chaining."""

    def __init__(self, provider: str, error: ProviderError):
        self.provider = str(provider)
        self.error = error
        super().__init__(
            "%s provider request failed [%s]: %s"
            % (self.provider, error.code, error.message)
        )


_CONTEXT_MARKERS = (
    "context_length_exceeded",
    "context length",
    "maximum context",
    "max context",
    "too many tokens",
    "token limit",
    "request too large for model",
)
_REQUEST_ID_HEADERS = (
    "x-request-id",
    "request-id",
    "x-groq-request-id",
    "cf-ray",
)
_RATE_LIMIT_HEADER_NAMES = {
    "retry-after",
    "x-ratelimit-limit-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
}
_BEARER_RE = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|secret|password|token)\s*[:=]\s*[^\s,;]+"
)


def project_provider_error(exc: BaseException) -> ProviderError:
    if isinstance(exc, ProviderCallError):
        return exc.error
    if isinstance(exc, httpx.TimeoutException):
        return _project_non_http(
            ProviderErrorCode.REQUEST_TIMEOUT,
            ProviderErrorScope.NETWORK,
            retryable=True,
            message="provider request timed out",
        )
    if isinstance(exc, httpx.TransportError):
        return _project_non_http(
            ProviderErrorCode.NETWORK_ERROR,
            ProviderErrorScope.NETWORK,
            retryable=True,
            message="provider transport failed",
        )
    if not isinstance(exc, httpx.HTTPStatusError):
        return _project_non_http(
            ProviderErrorCode.UNKNOWN,
            ProviderErrorScope.UNKNOWN,
            retryable=False,
            message="unexpected provider failure",
        )

    response = exc.response
    status = int(response.status_code)
    error_type, provider_code, provider_message = _response_error_fields(response)
    searchable = " ".join(
        value for value in (error_type, provider_code, provider_message) if value
    ).casefold()
    code, scope, retryable, reducible = _classify_http(status, searchable)
    headers = {str(k).casefold(): str(v) for k, v in response.headers.items()}
    rate_headers = {
        name: _bounded(headers[name], 200)
        for name in sorted(_RATE_LIMIT_HEADER_NAMES)
        if name in headers
    }
    request_id = next(
        (headers[name] for name in _REQUEST_ID_HEADERS if headers.get(name)),
        None,
    )
    return ProviderError(
        schema_version="provider-error@1",
        code=code.value,
        scope=scope.value,
        retryable=retryable,
        reducible=reducible,
        http_status=status,
        provider_error_type=_safe_text(error_type, 120),
        provider_error_code=_safe_text(provider_code, 120),
        message=_safe_text(provider_message, 500) or _default_message(code),
        provider_request_id=_safe_text(request_id, 200),
        retry_after=rate_headers.get("retry-after"),
        rate_limit_headers=rate_headers,
    )


def raise_provider_call_error(provider: str, exc: BaseException):
    if isinstance(exc, ProviderCallError):
        raise exc
    raise ProviderCallError(provider, project_provider_error(exc)) from exc


def _classify_http(status: int, searchable: str):
    if status == 400 and any(marker in searchable for marker in _CONTEXT_MARKERS):
        return (
            ProviderErrorCode.CONTEXT_LENGTH_EXCEEDED,
            ProviderErrorScope.REQUEST,
            False,
            True,
        )
    table = {
        400: (ProviderErrorCode.INVALID_REQUEST, ProviderErrorScope.REQUEST, False, False),
        401: (ProviderErrorCode.AUTHENTICATION_FAILED, ProviderErrorScope.CONNECTION, False, False),
        403: (ProviderErrorCode.PERMISSION_DENIED, ProviderErrorScope.CONNECTION, False, False),
        404: (ProviderErrorCode.MODEL_OR_ENDPOINT_NOT_FOUND, ProviderErrorScope.CONNECTION, False, False),
        408: (ProviderErrorCode.REQUEST_TIMEOUT, ProviderErrorScope.NETWORK, True, False),
        409: (ProviderErrorCode.CONFLICT, ProviderErrorScope.REQUEST, True, False),
        413: (ProviderErrorCode.REQUEST_TOO_LARGE, ProviderErrorScope.REQUEST, False, True),
        422: (ProviderErrorCode.UNPROCESSABLE_REQUEST, ProviderErrorScope.REQUEST, False, False),
        429: (ProviderErrorCode.RATE_LIMITED, ProviderErrorScope.CONNECTION, True, False),
        498: (ProviderErrorCode.CAPACITY_UNAVAILABLE, ProviderErrorScope.PROVIDER, True, False),
        499: (ProviderErrorCode.CANCELED, ProviderErrorScope.REQUEST, False, False),
    }
    if status in table:
        return table[status]
    if 500 <= status <= 599:
        return (
            ProviderErrorCode.PROVIDER_UNAVAILABLE,
            ProviderErrorScope.PROVIDER,
            True,
            False,
        )
    return ProviderErrorCode.UNKNOWN, ProviderErrorScope.UNKNOWN, False, False


def _response_error_fields(response):
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return None, None, None
    if not isinstance(payload, dict):
        return None, None, None
    error = payload.get("error")
    if isinstance(error, dict):
        return error.get("type"), error.get("code"), error.get("message")
    if isinstance(error, str):
        return None, None, error
    return payload.get("type"), payload.get("code"), payload.get("message")


def _project_non_http(code, scope, *, retryable, message):
    return ProviderError(
        schema_version="provider-error@1",
        code=code.value,
        scope=scope.value,
        retryable=retryable,
        reducible=False,
        http_status=None,
        provider_error_type=None,
        provider_error_code=None,
        message=message,
        provider_request_id=None,
        retry_after=None,
        rate_limit_headers={},
    )


def _safe_text(value, limit):
    if value in (None, ""):
        return None
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = _BEARER_RE.sub("Bearer ***REDACTED***", text)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: "%s=***REDACTED***" % match.group(1), text)
    return _bounded(text, limit)


def _bounded(value, limit):
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "...<truncated>"


def _default_message(code):
    return {
        ProviderErrorCode.CONTEXT_LENGTH_EXCEEDED: "request exceeds model context limit",
        ProviderErrorCode.INVALID_REQUEST: "provider rejected the request",
        ProviderErrorCode.AUTHENTICATION_FAILED: "provider authentication failed",
        ProviderErrorCode.PERMISSION_DENIED: "provider denied the request",
        ProviderErrorCode.MODEL_OR_ENDPOINT_NOT_FOUND: "provider model or endpoint was not found",
        ProviderErrorCode.REQUEST_TIMEOUT: "provider request timed out",
        ProviderErrorCode.CONFLICT: "provider reported a request conflict",
        ProviderErrorCode.REQUEST_TOO_LARGE: "provider request is too large",
        ProviderErrorCode.UNPROCESSABLE_REQUEST: "provider could not process the request",
        ProviderErrorCode.RATE_LIMITED: "provider rate limit reached",
        ProviderErrorCode.CAPACITY_UNAVAILABLE: "provider capacity unavailable",
        ProviderErrorCode.CANCELED: "provider request canceled",
        ProviderErrorCode.PROVIDER_UNAVAILABLE: "provider temporarily unavailable",
        ProviderErrorCode.NETWORK_ERROR: "provider transport failed",
        ProviderErrorCode.UNKNOWN: "unexpected provider failure",
    }[code]
