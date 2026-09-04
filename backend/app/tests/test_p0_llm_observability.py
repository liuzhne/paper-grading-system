from types import SimpleNamespace

import httpx
import pytest

from backend.app.core.config import Settings
from backend.app.core.config import settings
from backend.app.services.llm.errors import ProviderCallError
from backend.app.services.llm.errors import project_provider_error
from backend.app.services.llm.openai_compatible_adapter import (
    OpenAICompatibleChatScorer,
)
from backend.app.services.llm.retry import is_retryable_http_error
from backend.app.services import llm_observability as tracing
from backend.app.services.scoring import observed as observed_scoring


def _http_error(status, error, headers=None):
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response = httpx.Response(
        status,
        request=request,
        json={"error": error},
        headers=headers or {},
    )
    return httpx.HTTPStatusError("unsafe raw exception", request=request, response=response)


@pytest.mark.parametrize(
    ("status", "expected", "retryable", "reducible"),
    [
        (400, "invalid_request", False, False),
        (401, "authentication_failed", False, False),
        (403, "permission_denied", False, False),
        (404, "model_or_endpoint_not_found", False, False),
        (408, "request_timeout", True, False),
        (409, "conflict", True, False),
        (413, "request_too_large", False, True),
        (422, "unprocessable_request", False, False),
        (429, "rate_limited", True, False),
        (498, "capacity_unavailable", True, False),
        (499, "canceled", False, False),
        (500, "provider_unavailable", True, False),
        (503, "provider_unavailable", True, False),
    ],
)
def test_provider_error_has_stable_http_classification(
    status, expected, retryable, reducible
):
    projected = project_provider_error(
        _http_error(status, {"type": "request_error", "message": "safe message"})
    )

    assert projected.schema_version == "provider-error@1"
    assert projected.code == expected
    assert projected.retryable is retryable
    assert projected.reducible is reducible
    assert is_retryable_http_error(
        _http_error(status, {"message": "safe message"})
    ) is retryable


def test_context_error_is_reducible_and_provider_projection_is_allowlisted():
    exc = _http_error(
        400,
        {
            "type": "invalid_request_error",
            "code": "context_length_exceeded",
            "message": "maximum context length exceeded; Authorization=secret-value",
            "private_debug": "student paper must not escape",
        },
        {
            "x-groq-request-id": "groq-request-1",
            "retry-after": "12",
            "x-ratelimit-remaining-tokens": "0",
            "set-cookie": "secret-cookie",
        },
    )

    projected = project_provider_error(exc)
    payload = projected.to_mapping()

    assert projected.code == "context_length_exceeded"
    assert projected.reducible is True
    assert projected.retryable is False
    assert projected.provider_request_id == "groq-request-1"
    assert projected.retry_after == "12"
    assert projected.rate_limit_headers == {
        "retry-after": "12",
        "x-ratelimit-remaining-tokens": "0",
    }
    assert "private_debug" not in str(payload)
    assert "secret-value" not in projected.message
    assert "secret-cookie" not in str(payload)


def test_transport_and_unknown_errors_do_not_leak_exception_text():
    request = httpx.Request("POST", "https://api.example.test")
    timeout = project_provider_error(
        httpx.ReadTimeout("paper body and secret", request=request)
    )
    unknown = project_provider_error(ValueError("paper body and secret"))

    assert timeout.code == "request_timeout"
    assert timeout.retryable is True
    assert "paper body" not in timeout.message
    assert unknown.code == "unknown"
    assert "secret" not in unknown.message


def test_observability_configuration_is_fail_closed_when_enabled():
    with pytest.raises(ValueError, match="Langfuse observability configuration missing"):
        Settings(
            _env_file=None,
            LLM_OBSERVABILITY_ENABLED=True,
            LLM_OBSERVABILITY_EXPORTER="langfuse",
        )

    configured = Settings(
        _env_file=None,
        LLM_OBSERVABILITY_ENABLED=True,
        LLM_OBSERVABILITY_EXPORTER="langfuse",
        LANGFUSE_PUBLIC_KEY="public",
        LANGFUSE_SECRET_KEY="secret",
        LANGFUSE_BASE_URL="https://langfuse.example.test",
    )
    assert configured.LLM_OBSERVABILITY_CONTENT_MODE == "metadata_only"


def test_metadata_only_projection_records_identity_not_content(monkeypatch):
    monkeypatch.setattr(settings, "LLM_OBSERVABILITY_CONTENT_MODE", "metadata_only")
    projected = tracing.project_content(
        {
            "student_name": "张三",
            "student_id": "20260001",
            "messages": [{"content": "论文完整正文"}],
        }
    )

    assert projected["content_recorded"] is False
    assert projected["content_chars"] > 0
    assert len(projected["content_sha256"]) == 64
    assert "张三" not in str(projected)
    assert "论文完整正文" not in str(projected)


def test_redacted_projection_masks_common_pii_and_credentials(monkeypatch):
    monkeypatch.setattr(settings, "LLM_OBSERVABILITY_CONTENT_MODE", "redacted")
    projected = tracing.project_content(
        {
            "student_name": "张三",
            "student_id": "20260001",
            "authorization": "Bearer abc.secret",
            "text": "mail me at student@example.com or 13800138000",
        }
    )

    assert projected["student_name"] == "***REDACTED***"
    assert projected["student_id"] == "***REDACTED***"
    assert projected["authorization"] == "***REDACTED***"
    assert projected["text"] == "mail me at [EMAIL_REDACTED] or [PHONE_REDACTED]"


class _BrokenObservationClient:
    def start_as_current_observation(self, **kwargs):
        raise RuntimeError("collector unavailable")


def test_observability_failure_is_fail_open(monkeypatch):
    monkeypatch.setattr(tracing, "_get_client", lambda: _BrokenObservationClient())

    with tracing.observation("llm_generation", input={"text": "private"}) as span:
        span.update(output={"result": "still works"})
        result = "business result"

    assert result == "business result"


class _AlwaysBadRequestClient:
    def post(self, url, headers, json):
        request = httpx.Request("POST", url)
        return httpx.Response(
            400,
            request=request,
            json={
                "error": {
                    "code": "context_length_exceeded",
                    "message": "maximum context length exceeded",
                }
            },
            headers={"x-groq-request-id": "req-core-1"},
        )


def test_adapter_raises_safe_provider_call_error(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_MAX_RETRIES", 2)
    scorer = OpenAICompatibleChatScorer(
        api_key="test-key",
        base_url="https://api.example.test/v1",
        provider_name="groq",
        client=_AlwaysBadRequestClient(),
    )
    criterion = SimpleNamespace(
        id="criterion-1",
        code="C01",
        name="研究方法",
        max_score=20,
        evidence_hints=[],
        description="",
    )
    paper = SimpleNamespace(id="paper-1", title="private title")

    with pytest.raises(ProviderCallError) as caught:
        scorer.score_criterion(paper, criterion, [], [])

    assert caught.value.error.code == "context_length_exceeded"
    assert caught.value.error.provider_request_id == "req-core-1"
    assert "private title" not in str(caught.value)


def test_scoring_run_trace_uses_persisted_request_identity(monkeypatch):
    captured = {}

    class _Span:
        def update(self, **kwargs):
            captured["update"] = kwargs

    class _Manager:
        def __enter__(self):
            return _Span()

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_observation(name, **kwargs):
        captured["name"] = name
        captured["kwargs"] = kwargs
        return _Manager()

    class _Outcome:
        def to_mapping(self):
            return {
                "status": "completed",
                "review_issues": [],
                "final_total": "90",
            }

    monkeypatch.setattr(observed_scoring, "observation", fake_observation)
    fake_score = lambda **kwargs: _Outcome()
    request = {
        "idempotency_key": "a" * 64,
        "rescore_generation": 0,
        "document": {"document_snapshot_hash": "b" * 64},
        "plan": {
            "rubric_snapshot_hash": "c" * 64,
            "plan_hash": "d" * 64,
            "policy_hash": "e" * 64,
        },
        "runtime_identity": {
            "profile_key": "thesis",
            "profile_version": "profile@1",
            "prompt_version": "prompt@1",
            "provider": {"name": "groq", "model": "model"},
        },
    }

    result = observed_scoring.score_submission_observed(
        request=request,
        checker_registry=object(),
        llm_runtime=object(),
        profile=object(),
        organization_id="org-1",
        batch_job_id="job-1",
        score_fn=fake_score,
    )

    assert isinstance(result, _Outcome)
    assert captured["name"] == "scoring_run"
    assert captured["kwargs"]["metadata"]["scoring_request_id"] == "a" * 64
    assert captured["kwargs"]["metadata"]["organization_id"] == "org-1"
    assert captured["update"]["output"]["status"] == "completed"
