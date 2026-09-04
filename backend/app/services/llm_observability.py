"""Fail-open OpenTelemetry/Langfuse tracing with source-side redaction."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from backend.app.core.config import settings


logger = logging.getLogger("paper_grading.observability")

_client = None
_client_key = None
_client_lock = threading.Lock()
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_BEARER_RE = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")
_LONG_ID_RE = re.compile(r"(?<!\d)\d{8,20}(?!\d)")
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "student_id",
    "student_name",
    "token",
}


@dataclass
class _NullObservation:
    trace_id: str | None = None

    def update(self, **kwargs):
        return self


class _SafeObservation:
    def __init__(self, delegate):
        self._delegate = delegate

    @property
    def trace_id(self):
        getter = getattr(self._delegate, "get_trace_id", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                return None
        return getattr(self._delegate, "trace_id", None)

    def update(self, **kwargs):
        try:
            self._delegate.update(**_project_update_kwargs(kwargs))
        except Exception as exc:  # observability must never fail scoring
            logger.warning(
                "Langfuse observation update failed: %s", exc.__class__.__name__
            )
        return self


@contextmanager
def observation(
    name: str,
    *,
    as_type: str = "span",
    metadata: dict[str, Any] | None = None,
    input: Any = None,
    model: str | None = None,
    model_parameters: dict[str, Any] | None = None,
):
    """Create a nested observation; initialization/export failures are fail-open."""

    client = _get_client()
    if client is None:
        yield _NullObservation()
        return
    kwargs = {
        "name": str(name),
        "as_type": as_type,
        "metadata": _sanitize(metadata or {}),
    }
    if input is not None:
        kwargs["input"] = project_content(input)
    if model:
        kwargs["model"] = str(model)
    if model_parameters:
        kwargs["model_parameters"] = _sanitize(model_parameters)
    manager = None
    try:
        manager = client.start_as_current_observation(**kwargs)
        delegate = manager.__enter__()
    except Exception as exc:
        logger.warning(
            "Langfuse observation start failed: %s", exc.__class__.__name__
        )
        yield _NullObservation()
        return

    safe = _SafeObservation(delegate)
    body_error = None
    try:
        yield safe
    except BaseException:
        body_error = sys.exc_info()
        safe.update(level="ERROR", status_message="application operation failed")
        raise
    finally:
        try:
            if body_error is None:
                manager.__exit__(None, None, None)
            else:
                manager.__exit__(*body_error)
        except Exception as exc:
            logger.warning(
                "Langfuse observation end failed: %s", exc.__class__.__name__
            )


def project_content(value):
    """Return metadata-only fingerprints by default, or bounded redacted content."""

    if settings.LLM_OBSERVABILITY_CONTENT_MODE == "metadata_only":
        serialized = _json_text(value)
        return {
            "content_sha256": hashlib.sha256(
                serialized.encode("utf-8")
            ).hexdigest(),
            "content_chars": len(serialized),
            "content_recorded": False,
        }
    return _sanitize(value)


def observability_status():
    return {
        "enabled": bool(settings.LLM_OBSERVABILITY_ENABLED),
        "exporter": settings.LLM_OBSERVABILITY_EXPORTER,
        "content_mode": settings.LLM_OBSERVABILITY_CONTENT_MODE,
        "success_sample_rate": settings.LLM_OBSERVABILITY_SUCCESS_SAMPLE_RATE,
        "configured": bool(
            settings.LLM_OBSERVABILITY_ENABLED
            and settings.LLM_OBSERVABILITY_EXPORTER == "langfuse"
            and settings.LANGFUSE_PUBLIC_KEY
            and settings.LANGFUSE_SECRET_KEY
            and settings.LANGFUSE_BASE_URL
        ),
    }


def reset_observability_for_tests():
    global _client, _client_key
    with _client_lock:
        old = _client
        _client = None
        _client_key = None
    shutdown = getattr(old, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown()
        except Exception:
            pass


def _get_client():
    if not settings.LLM_OBSERVABILITY_ENABLED:
        return None
    key = (
        settings.LLM_OBSERVABILITY_EXPORTER,
        settings.LANGFUSE_PUBLIC_KEY,
        settings.LANGFUSE_SECRET_KEY,
        settings.LANGFUSE_BASE_URL,
        settings.LANGFUSE_ENVIRONMENT,
        settings.LANGFUSE_RELEASE,
        settings.LLM_OBSERVABILITY_SUCCESS_SAMPLE_RATE,
        settings.LLM_OBSERVABILITY_CONTENT_MODE,
    )
    global _client, _client_key
    with _client_lock:
        if _client is not None and _client_key == key:
            return _client
        try:
            if settings.LLM_OBSERVABILITY_EXPORTER != "langfuse":
                return None
            from langfuse import Langfuse

            _client = Langfuse(
                public_key=settings.LANGFUSE_PUBLIC_KEY,
                secret_key=settings.LANGFUSE_SECRET_KEY,
                base_url=settings.LANGFUSE_BASE_URL,
                environment=settings.LANGFUSE_ENVIRONMENT,
                release=settings.LANGFUSE_RELEASE,
                sample_rate=settings.LLM_OBSERVABILITY_SUCCESS_SAMPLE_RATE,
                mask_otel_spans=_mask_otel_spans,
            )
            _client_key = key
        except Exception as exc:
            _client = None
            _client_key = None
            logger.warning(
                "Langfuse initialization failed: %s", exc.__class__.__name__
            )
        return _client


def _mask_otel_spans(*, params):
    """Last-mile defense for every span routed through Langfuse."""

    from langfuse.types import MaskOtelSpansResult
    from langfuse.types import OtelSpanPatch

    patches = {}
    for identifier, span in params.spans.items():
        replacements = {}
        deletions = []
        for key, value in span.attributes.items():
            normalized = str(key).casefold()
            if settings.LLM_OBSERVABILITY_CONTENT_MODE == "metadata_only" and any(
                marker in normalized
                for marker in (
                    "gen_ai.input.messages",
                    "gen_ai.output.messages",
                    "gen_ai.prompt",
                    "gen_ai.completion",
                    "gen_ai.system_instructions",
                    "gen_ai.retrieval.query.text",
                )
            ):
                deletions.append(key)
                continue
            if isinstance(value, str):
                masked = _sanitize_text(value)
                if masked != value:
                    replacements[key] = masked
        if replacements or deletions:
            patches[identifier] = OtelSpanPatch(
                delete_attributes=tuple(deletions),
                set_attributes=replacements,
            )
    return MaskOtelSpansResult(span_patches=patches)


def _project_update_kwargs(kwargs):
    projected = dict(kwargs)
    for key in ("input", "output"):
        if key in projected:
            projected[key] = project_content(projected[key])
    if "metadata" in projected:
        projected["metadata"] = _sanitize(projected["metadata"])
    return projected


def _sanitize(value, key=None):
    normalized_key = str(key or "").casefold().replace("-", "_")
    if normalized_key in _SENSITIVE_KEYS or normalized_key.endswith(
        ("_secret", "_token", "_key")
    ):
        return "***REDACTED***"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize(item, item_key)
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        text = _sanitize_text(value)
        limit = settings.LLM_OBSERVABILITY_MAX_CONTENT_CHARS
        return text if len(text) <= limit else text[:limit] + "...<truncated>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _sanitize_text(str(value))


def _sanitize_text(text):
    text = _BEARER_RE.sub("Bearer ***REDACTED***", str(text))
    text = _EMAIL_RE.sub("[EMAIL_REDACTED]", text)
    text = _PHONE_RE.sub("[PHONE_REDACTED]", text)
    return _LONG_ID_RE.sub("[IDENTIFIER_REDACTED]", text)


def _json_text(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
