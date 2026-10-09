"""三个协议适配器的同一套合同测试（docs/模型协议适配器改造方案.md 第 12.1 节）。

每个协议只提供一个「协议夹具」：端点路径、鉴权头、成功/截断/错误响应的线上形状。
用例本身对三个适配器一视同仁——上层（评分、起草、归类、结构识别）依赖的正是这些
共同行为。全部走 httpx.MockTransport 和真实的共享传输层，不调用任何真实模型。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from types import SimpleNamespace
from typing import Callable

import httpx
import pytest

from backend.app.core.config import settings
from backend.app.services.ai_connections import ConnectionRuntime
from backend.app.services.llm import transport
from backend.app.services.llm.anthropic_messages_adapter import AnthropicMessagesScorer
from backend.app.services.llm.errors import ProviderCallError
from backend.app.services.llm.errors import ProviderJSONOutputError
from backend.app.services.llm.factory import get_llm_scorer
from backend.app.services.llm.openai_adapter import OpenAIResponsesScorer
from backend.app.services.llm.openai_compatible_adapter import OpenAICompatibleChatScorer
from backend.app.services.llm.rate_limit import CircuitOpenError
from backend.app.services.llm.rate_limit import reset_provider_runtime_for_tests
from backend.app.tests.test_core_llm_adapters import _core_envelope
from backend.app.tests.test_core_llm_adapters import _provider_response


KEY = "sk-contract-secret-9Q4Z"


@dataclass(frozen=True)
class Protocol:
    name: str
    path: str
    build: Callable
    auth: Callable
    success: Callable
    truncated: Callable
    rate_limited: dict
    quota_exhausted: tuple


def _chat_success(text):
    return {
        "id": "chatcmpl-1",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def _responses_success(text):
    return {
        "id": "resp-1",
        "status": "completed",
        "output_text": text,
        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    }


def _messages_success(text):
    return {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        # 思考块在前、文本块在后：解析只取文本。
        "content": [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "cache_read_input_tokens": 6, "output_tokens": 7},
    }


PROTOCOLS = [
    Protocol(
        name="openai_compatible",
        path="/chat/completions",
        build=lambda client, **kw: OpenAICompatibleChatScorer(
            api_key=KEY, base_url="https://chat.contract.test/v1", model_name="chat-model",
            provider_name="openai_compatible", client=client, thinking_type="", response_format_json=False, **kw,
        ),
        auth=lambda headers: headers["authorization"] == "Bearer %s" % KEY,
        success=_chat_success,
        truncated=lambda: {"id": "c", "choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
        rate_limited={"error": {"code": "1302", "message": "too many requests"}},
        quota_exhausted=(429, {"error": {"code": "1113", "message": "balance"}}),
    ),
    Protocol(
        name="openai_responses",
        path="/responses",
        build=lambda client, **kw: OpenAIResponsesScorer(
            api_key=KEY, base_url="https://responses.contract.test/v1", model_name="responses-model",
            client=client, **{("max_output_tokens" if k == "max_tokens" else k): v for k, v in kw.items()},
        ),
        auth=lambda headers: headers["authorization"] == "Bearer %s" % KEY,
        success=_responses_success,
        truncated=lambda: {"id": "r", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        rate_limited={"error": {"type": "rate_limit_exceeded", "message": "slow down"}},
        quota_exhausted=(429, {"error": {"type": "insufficient_quota", "code": "insufficient_quota"}}),
    ),
    Protocol(
        name="anthropic_messages",
        path="/messages",
        build=lambda client, **kw: AnthropicMessagesScorer(
            api_key=KEY, base_url="https://bedrock-runtime.us-east-1.amazonaws.com/anthropic/v1",
            model_name="anthropic.claude-opus-5-5", client=client, **kw,
        ),
        auth=lambda headers: headers["x-api-key"] == KEY and headers["anthropic-version"] == "2023-06-01",
        success=_messages_success,
        truncated=lambda: {"id": "m", "type": "message", "content": [{"type": "thinking", "thinking": ""}],
                           "stop_reason": "max_tokens", "usage": {"input_tokens": 3, "output_tokens": 9}},
        rate_limited={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
        quota_exhausted=(429, {"type": "error", "error": {
            "type": "rate_limit_error", "message": "You have reached your API usage limits",
            "details": {"error_code": "enforced_spend_limit_reached"}}}),
    ),
]
IDS = [protocol.name for protocol in PROTOCOLS]


class Recorder:
    """Scripted MockTransport: each entry is a response body, (status, body[, headers]) or an exception."""

    def __init__(self, protocol, *script):
        self.protocol = protocol
        self.script = list(script)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.path.endswith(self.protocol.path)
        assert self.protocol.auth(request.headers)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        status, body, headers = (200, step, {}) if isinstance(step, dict) else (step + ({},))[:3]
        return httpx.Response(status, json=body, headers=headers, request=request)

    def scorer(self, **kwargs):
        return self.protocol.build(httpx.Client(transport=httpx.MockTransport(self)), **kwargs)


@pytest.fixture(autouse=True)
def _isolated_circuits():
    reset_provider_runtime_for_tests()
    yield
    reset_provider_runtime_for_tests()


@pytest.fixture()
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(transport.time, "sleep", recorded.append)
    return recorded


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=IDS)
def test_complete_json_returns_the_object_and_meters_usage(protocol):
    recorder = Recorder(protocol, protocol.success('{"items": [1, 2]}'))
    scorer = recorder.scorer()

    assert scorer.complete_json("系统", {"units": []}) == {"items": [1, 2]}
    assert scorer.usage_meter.snapshot() == {
        "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
        "request_count": 1, "failure_count": 0,
    }


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=IDS)
def test_schema_requests_are_sent_once_even_when_the_provider_is_down(protocol, sleeps):
    recorder = Recorder(protocol, (503, {"error": {"message": "down"}}), protocol.success("{}"))
    scorer = recorder.scorer()

    with pytest.raises(ProviderCallError) as caught:
        scorer.complete_json("系统", {}, response_schema={"type": "object", "properties": {}, "required": [],
                                                       "additionalProperties": False})
    assert caught.value.error.code == "provider_unavailable"
    assert len(recorder.requests) == 1 and sleeps == []


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=IDS)
def test_rate_limits_are_retried_within_their_own_budget(protocol, sleeps):
    recorder = Recorder(protocol, (429, protocol.rate_limited, {"retry-after": "1"}), protocol.success("{}"))
    scorer = recorder.scorer()

    assert scorer.complete_json("系统", {}, attempts_limit=1, rate_limit_retries=1) == {}
    assert len(recorder.requests) == 2 and sleeps == [pytest.approx(1.0)]


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=IDS)
def test_exhausted_quota_is_never_retried(protocol, sleeps):
    status, body = protocol.quota_exhausted
    recorder = Recorder(protocol, (status, body), protocol.success("{}"))
    scorer = recorder.scorer()

    with pytest.raises(ProviderCallError) as caught:
        scorer.complete_json("系统", {}, attempts_limit=1, rate_limit_retries=3)
    assert caught.value.error.code == "quota_exhausted"
    assert caught.value.error.retryable is False
    assert len(recorder.requests) == 1 and sleeps == []
    assert scorer.usage_meter.snapshot()["failure_count"] == 1


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=IDS)
def test_a_single_attempt_does_not_retry_timeouts(protocol, sleeps):
    recorder = Recorder(protocol, httpx.ReadTimeout("slow"))
    scorer = recorder.scorer()

    with pytest.raises(ProviderCallError) as caught:
        scorer.complete_json("系统", {}, attempts_limit=1, rate_limit_retries=2)
    assert caught.value.error.code == "request_timeout"
    assert len(recorder.requests) == 1 and sleeps == []


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=IDS)
def test_an_expired_deadline_sends_nothing(protocol, sleeps):
    recorder = Recorder(protocol, protocol.success("{}"))
    scorer = recorder.scorer()

    with pytest.raises(ProviderCallError):
        scorer.complete_json("系统", {}, attempts_limit=1, deadline=time.monotonic() - 1)
    assert recorder.requests == []


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=IDS)
def test_call_timeout_is_capped_by_the_remaining_budget(protocol, sleeps):
    seen = []

    class Client:
        def post(self, url, *, headers, json, timeout=None):
            seen.append(timeout)
            return httpx.Response(200, json=protocol.success("{}"), request=httpx.Request("POST", url))

    scorer = protocol.build(Client(), timeout_seconds=120)
    scorer.complete_json("系统", {}, attempts_limit=1, deadline=time.monotonic() + 40)
    assert 0 < seen[0] <= 40


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=IDS)
def test_truncation_is_reported_through_the_shared_error_base(protocol):
    recorder = Recorder(protocol, protocol.truncated())
    scorer = recorder.scorer()

    with pytest.raises(ProviderJSONOutputError) as caught:
        scorer.complete_json("系统", {})
    assert caught.value.reason == "output_truncated"


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=IDS)
def test_rejected_credentials_open_the_circuit_permanently(protocol, sleeps, monkeypatch):
    monkeypatch.setattr(settings, "PROVIDER_CIRCUIT_BREAKER_ENABLED", True)
    recorder = Recorder(protocol, (401, {"error": {"type": "authentication_error", "message": "bad key"}}))
    scorer = recorder.scorer()

    with pytest.raises(ProviderCallError) as caught:
        scorer.complete_json("系统", {}, attempts_limit=1)
    assert caught.value.error.code == "authentication_failed"
    with pytest.raises(CircuitOpenError):
        scorer.complete_json("系统", {}, attempts_limit=1)
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=IDS)
def test_projected_errors_never_carry_the_key_and_keep_the_request_id(protocol, sleeps):
    body = {"error": {"type": "invalid_request_error", "message": "model does not accept this field"}}
    recorder = Recorder(protocol, (400, body, {"request-id": "req_contract_1"}))
    scorer = recorder.scorer()

    with pytest.raises(ProviderCallError) as caught:
        scorer.complete_json("系统", {}, attempts_limit=1)
    error = caught.value.error
    assert error.code == "invalid_request"
    assert error.provider_request_id == "req_contract_1"
    assert KEY not in str(caught.value) and KEY not in json.dumps(error.to_mapping())


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=IDS)
def test_core_rule_decisions_are_identical_across_protocols(protocol):
    recorder = Recorder(protocol, {})
    scorer = recorder.scorer()
    envelope = _core_envelope(scorer)
    recorder.script = [protocol.success(json.dumps(_provider_response(envelope), ensure_ascii=False))]

    decoded = scorer.score_core_envelope(envelope=envelope)

    value = envelope.to_mapping()
    assert decoded["rule_code"] == value["atomic_rule_snapshot"]["rule_code"]
    assert decoded["status"] == "triggered"
    assert decoded["occurrences"][0]["locator"] == value["evidence_units"][0]["locator"]
    assert scorer.last_view_summary is not None
    # The same model JSON decodes the same way whichever protocol carried it.
    reference = Recorder(PROTOCOLS[0], PROTOCOLS[0].success(json.dumps(_provider_response(envelope), ensure_ascii=False)))
    chat = reference.scorer()
    assert decoded == chat.score_core_envelope(envelope=_core_envelope(chat))


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=IDS)
def test_core_envelopes_from_another_configuration_are_rejected_before_network(protocol):
    recorder = Recorder(protocol, {})
    scorer = recorder.scorer()
    envelope = _core_envelope(scorer).to_mapping()
    envelope["runtime_identity"]["provider"]["model"] = "another-model"

    with pytest.raises(ValueError, match="provider identity does not match"):
        scorer.score_core_envelope(envelope=envelope)
    assert recorder.requests == []


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=IDS)
def test_legacy_criterion_scoring_reports_provider_response_and_usage(protocol):
    model_output = {
        "criterion_id": "c1", "criterion_name": "研究方法", "max_score": 20, "score": 12,
        "evidence_sufficient": True, "reason": "r", "deductions": [], "deduction_items": [],
        "evidence": [], "suggestion": "s", "confidence": 0.6, "need_manual_review": False,
    }
    recorder = Recorder(protocol, protocol.success(json.dumps(model_output, ensure_ascii=False)))
    scorer = recorder.scorer()
    criterion = SimpleNamespace(id="c1", code="C01", name="研究方法", max_score=20, description="",
                                evidence_hints=[], deduction_rules=[], scoring_mode="llm_direct", rubric_levels=[])
    paper = SimpleNamespace(id="p1", title="题目")

    output = scorer.score_criterion(paper, criterion, [{"chunk_id": "k", "text": "正文", "location": "第1页"}], [])

    assert output["score"] == 12
    assert output["provider_response_id"]
    assert output["usage"]["completion_tokens"] == 7


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=IDS)
def test_legacy_prompt_envelope_round_trips_through_the_adapter(protocol, monkeypatch):
    # The legacy envelope reads the Chat defaults from settings; align them with the
    # fixture connection so all three adapters accept their own envelope.
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_THINKING_TYPE", "")
    model_output = {
        "criterion_id": "C01", "criterion_name": "研究方法", "max_score": 20, "score": 12,
        "evidence_sufficient": True, "reason": "r", "deductions": [], "deduction_items": [],
        "evidence": [], "suggestion": "s", "confidence": 0.6, "need_manual_review": False,
    }
    recorder = Recorder(protocol, protocol.success(json.dumps(model_output, ensure_ascii=False)))
    scorer = recorder.scorer(**({"temperature": settings.OPENAI_TEMPERATURE} if protocol.name == "openai_responses" else {}))
    criterion = SimpleNamespace(code="C01", name="研究方法", max_score=20, scoring_mode="llm_direct", description="")
    paper = SimpleNamespace(title="题目")

    envelope = scorer.build_prompt_envelope(
        paper, criterion, [{"text": "正文", "location": "第1页"}], [], {"criteria": []}, {}, None,
    )
    output = scorer.score_envelope(envelope)

    assert output["score"] == 12
    assert output["usage"]["prompt_tokens"] == 11


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=IDS)
def test_bound_connection_carries_its_concurrency_but_not_into_the_snapshot(protocol):
    base_url = {
        "openai_compatible": "https://chat.contract.test/v1",
        "openai_responses": "https://responses.contract.test/v1",
        "anthropic_messages": "https://api.anthropic.com/v1",
    }[protocol.name]
    runtime = ConnectionRuntime(
        connection_id="conn-1", key_version=2, organization_id="org-1", provider_type=protocol.name,
        base_url=base_url, model_name="m", provider_options={"max_concurrency": 2, "timeout_seconds": 30},
        api_key=KEY,
    )

    scorer = get_llm_scorer(runtime)

    assert scorer.max_concurrency == 2
    assert scorer.timeout_seconds == 30
    assert scorer._ai_connection_snapshot["provider_type"] == protocol.name
    assert "max_concurrency" not in scorer._ai_connection_snapshot["provider_options"]
    assert KEY not in json.dumps(scorer._ai_connection_snapshot)
