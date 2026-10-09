"""Claude 适配器（anthropic_messages）独有的约定：请求形状、结构化输出两种模式、
响应解析、复现身份、错误识别、连接校验与测试连接。全部用 Mock，不调用真实模型。"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from backend.app.core.config import settings
from backend.app.services.ai_connection_protocol import ProtocolEndpointMissing
from backend.app.services.ai_connections import ConnectionRuntime
from backend.app.services.ai_connections import validate_provider_options_for
from backend.app.services.ai_connections import verify_connection_runtime
from backend.app.services.llm.anthropic_messages_adapter import AnthropicMessagesScorer
from backend.app.services.llm.anthropic_messages_adapter import MessagesJSONOutputError
from backend.app.services.llm.anthropic_messages_adapter import parse_messages_json
from backend.app.services.llm.anthropic_messages_adapter import resolve_structured_output
from backend.app.services.llm.anthropic_messages_adapter import sanitize_schema
from backend.app.services.llm.anthropic_messages_adapter import usage_from_messages
from backend.app.services.llm.core_adapter import core_runtime_provider_contract
from backend.app.services.llm.core_view import build_core_request
from backend.app.services.llm.debug_logging import log_llm_request
from backend.app.services.llm.errors import project_provider_error
from backend.app.services.llm.legacy_prompts import envelope_score_schema
from backend.app.services.rubric_import.ai_rule_drafter import _draft_output_schema
from backend.app.services.scoring.profiles.thesis import ThesisLLMRuntime
from backend.app.services.scoring.profiles.thesis import ThesisProfile
from backend.app.tests.test_core_llm_adapters import _core_envelope
from backend.app.tests.test_core_llm_adapters import _provider_response


KEY = "bedrock-api-key-ABSK-7Q2Z"
BEDROCK = "https://bedrock-runtime.us-east-1.amazonaws.com/anthropic/v1"
MANTLE = "https://bedrock-mantle.us-east-1.api.aws/anthropic/v1"
ANTHROPIC = "https://api.anthropic.com/v1"
UNSUPPORTED = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength",
    "maxLength", "maxItems", "uniqueItems", "minProperties", "maxProperties", "pattern",
}


def _message(text, *, stop_reason="end_turn", usage=None):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude",
        "content": [{"type": "text", "text": text}], "stop_reason": stop_reason,
        "usage": usage or {"input_tokens": 10, "output_tokens": 4},
    }


class Capture:
    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        return httpx.Response(200, json=body, request=request)

    @property
    def payload(self):
        return json.loads(self.requests[-1].content)

    def scorer(self, base_url=BEDROCK, **kwargs):
        return AnthropicMessagesScorer(
            api_key=KEY, base_url=base_url, model_name="anthropic.claude-opus-5-5",
            client=httpx.Client(transport=httpx.MockTransport(self)), **kwargs,
        )


def _walk_keys(value, found=None):
    """Every schema keyword in use, skipping the names inside ``properties``."""

    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "properties" and isinstance(item, dict):
                found.add(key)
                for schema in item.values():
                    _walk_keys(schema, found)
                continue
            found.add(key)
            _walk_keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_keys(item, found)
    return found


# -- request shape -----------------------------------------------------------------


def test_default_request_sends_no_sampling_thinking_or_effort():
    capture = Capture(_message("{}"))
    scorer = capture.scorer()

    scorer.complete_json("系统指令", {"x": 1})

    request = capture.requests[0]
    assert str(request.url) == BEDROCK + "/messages"
    assert request.headers["x-api-key"] == KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers
    assert "anthropic-beta" not in request.headers
    payload = capture.payload
    assert payload == {
        "model": "anthropic.claude-opus-5-5",
        "max_tokens": settings.ANTHROPIC_MAX_TOKENS,
        "system": "系统指令",
        "messages": [{"role": "user", "content": '{"x": 1}'}],
    }


def test_explicit_controls_are_sent_as_configured():
    capture = Capture(_message("{}"))
    scorer = capture.scorer(temperature=0.3, thinking_type="adaptive", effort="low", max_tokens=2048)

    scorer.complete_json("系统", {})

    payload = capture.payload
    assert payload["temperature"] == 0.3 and "top_p" not in payload
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "low"}
    assert payload["max_tokens"] == 2048


@pytest.mark.parametrize("kwargs", [
    {"temperature": 0.2, "top_p": 0.9},
    {"thinking_type": "enabled"},
    {"effort": "extreme"},
    {"structured_output": "strict"},
])
def test_invalid_controls_fail_before_any_request(kwargs):
    capture = Capture(_message("{}"))
    with pytest.raises(ValueError):
        capture.scorer(**kwargs)
    assert capture.requests == []


@pytest.mark.parametrize("base_url, options, expected", [
    (ANTHROPIC, {}, "json_schema"),
    (BEDROCK, {}, "off"),
    (MANTLE, {}, "off"),
    ("https://api.deepseek.com/anthropic/v1", {}, "off"),
    (BEDROCK, {"structured_output": "json_schema"}, "json_schema"),
    (ANTHROPIC, {"structured_output": "off"}, "off"),
])
def test_structured_output_defaults_only_on_verified_hosts(base_url, options, expected):
    assert resolve_structured_output(base_url, options) == expected


def test_structured_mode_sends_a_sanitized_schema_next_to_effort():
    capture = Capture(_message('{"rule_groups": []}'))
    scorer = capture.scorer(base_url=ANTHROPIC, effort="medium")
    schema = _draft_output_schema(maximum=10, allowed_source_refs=["S1", "S2"])

    scorer.complete_json("起草", {}, response_schema=schema)

    output_config = capture.payload["output_config"]
    assert output_config["effort"] == "medium"
    assert output_config["format"]["type"] == "json_schema"
    sent = output_config["format"]["schema"]
    assert sent == sanitize_schema(schema)
    assert not (_walk_keys(sent) & UNSUPPORTED)
    # Structure and allowed values survive; only the constraints are dropped.
    assert _walk_keys(sent) >= {"type", "properties", "required", "additionalProperties", "enum", "items"}


def test_off_mode_sends_the_same_prompt_without_a_format():
    capture = Capture(_message('{"rule_groups": []}'))
    scorer = capture.scorer(base_url=MANTLE, effort="low")

    scorer.complete_json("起草", {}, response_schema={"type": "object", "properties": {}, "required": [],
                                                    "additionalProperties": False})

    assert capture.payload["output_config"] == {"effort": "low"}
    assert capture.payload["system"] == "起草"


def test_sanitizer_keeps_property_names_that_look_like_keywords():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["pattern", "maximum"],
        "properties": {
            "pattern": {"type": "string", "pattern": "^a$", "minLength": 1},
            "maximum": {"type": "number", "maximum": 5},
            "refs": {"type": "array", "minItems": 1, "maxItems": 3, "uniqueItems": True,
                     "items": {"type": "string", "enum": ["a"]}},
            "many": {"type": "array", "minItems": 2, "items": {"anyOf": [{"type": "string", "maxLength": 3},
                                                                       {"type": "null"}]}},
        },
    }

    assert sanitize_schema(schema) == {
        "type": "object",
        "additionalProperties": False,
        "required": ["pattern", "maximum"],
        "properties": {
            "pattern": {"type": "string"},
            "maximum": {"type": "number"},
            "refs": {"type": "array", "minItems": 1, "items": {"type": "string", "enum": ["a"]}},
            "many": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        },
    }


@pytest.mark.parametrize("mode", ["llm_direct", "deductive", "banded"])
def test_every_scoring_schema_sanitizes_to_supported_keywords(mode):
    assert not (_walk_keys(sanitize_schema(envelope_score_schema(mode))) & UNSUPPORTED)


# -- response parsing --------------------------------------------------------------


def test_text_after_thinking_blocks_is_parsed_and_fences_are_stripped():
    data = _message("ignored")
    data["content"] = [
        {"type": "thinking", "thinking": "", "signature": "s"},
        {"type": "redacted_thinking", "data": "x"},
        {"type": "text", "text": '```json\n{"ok": true}\n```'},
    ]
    assert parse_messages_json(data) == {"ok": True}


@pytest.mark.parametrize("data, reason", [
    (_message('{"ok": true}', stop_reason="refusal"), "refused"),
    (_message('{"ok": tr', stop_reason="max_tokens"), "output_truncated"),
    (_message('{"ok": true}', stop_reason="pause_turn"), "incomplete_output"),
    (_message("   "), "empty_content"),
    (_message("not json"), "invalid_json"),
    ({"type": "error", "error": {"type": "api_error"}}, "error_envelope"),
    ({"type": "message", "content": "text"}, "invalid_envelope"),
    ([], "invalid_envelope"),
])
def test_unusable_answers_name_their_reason(data, reason):
    with pytest.raises(MessagesJSONOutputError) as caught:
        parse_messages_json(data)
    assert caught.value.reason == reason


def test_usage_counts_cached_input_as_prompt_tokens():
    usage = usage_from_messages({"usage": {
        "input_tokens": 50, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 200, "output_tokens": 30,
    }})
    assert usage == {"prompt_tokens": 1250, "completion_tokens": 30, "total_tokens": 1280}
    assert usage_from_messages({}) == {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}


# -- reproducibility identity ------------------------------------------------------


def test_identity_records_what_is_sent():
    plain = Capture(_message("{}")).scorer()
    tuned = Capture(_message("{}")).scorer(top_p=0.8, thinking_type="disabled", effort="low", max_tokens=900)

    plain_identity = core_runtime_provider_contract(plain, artifact_hash="a" * 64)
    tuned_identity = core_runtime_provider_contract(tuned, artifact_hash="a" * 64)

    assert plain_identity["name"] == "anthropic"
    assert plain_identity["model_version"] == "messages-2023-06-01"
    assert plain_identity["sampling"] == {"temperature": "1", "top_p": "1", "seed": None,
                                          "max_tokens": settings.ANTHROPIC_MAX_TOKENS}
    assert plain_identity["thinking"] == {"enabled": False, "type": None}
    assert plain_identity["response_format"] == "none"
    assert tuned_identity["model_version"] == "messages-2023-06-01;effort=low"
    assert tuned_identity["sampling"]["top_p"] == "0.8"
    assert tuned_identity["thinking"] == {"enabled": False, "type": "disabled"}
    structured = Capture(_message("{}")).scorer(base_url=ANTHROPIC, thinking_type="adaptive")
    identity = core_runtime_provider_contract(structured, artifact_hash="a" * 64)
    assert identity["response_format"] == "json_schema"
    assert identity["thinking"] == {"enabled": True, "type": "adaptive"}


def test_profiles_freeze_the_claude_connection_controls():
    scorer = Capture(_message("{}")).scorer(effort="high", max_tokens=1500)

    provider = ThesisProfile().build_runtime_identity(scorer)["provider"]

    assert provider["model_version"] == "messages-2023-06-01;effort=high"
    assert provider["sampling"]["max_tokens"] == 1500
    assert provider["response_schema"] == "atomic-rule-decisions@1"


@pytest.mark.parametrize("base_url", [BEDROCK, ANTHROPIC])
def test_core_rule_sends_exactly_the_provider_view(base_url):
    capture = Capture(_message("{}"))
    scorer = capture.scorer(base_url=base_url)
    envelope = _core_envelope(scorer)
    capture.bodies = [_message(json.dumps(_provider_response(envelope), ensure_ascii=False))]

    output = ThesisLLMRuntime(scorer).score(envelope=envelope)

    request = build_core_request(envelope)
    assert output["status"] == "triggered"
    assert capture.payload["system"] == request.system
    assert capture.payload["messages"] == [{"role": "user", "content": request.user}]
    if base_url == ANTHROPIC:
        assert capture.payload["output_config"]["format"]["schema"] == sanitize_schema(request.schema)
    else:
        assert "output_config" not in capture.payload


def test_a_legacy_envelope_built_for_other_controls_is_rejected_before_network():
    capture = Capture(_message("{}"))
    scorer = capture.scorer(effort="low")
    other = Capture(_message("{}")).scorer(effort="low", max_tokens=777)
    envelope = other.build_prompt_envelope(
        type("Paper", (), {"title": "T"})(), type("C", (), {"code": "C1", "name": "N", "max_score": 5})(),
        [{"text": "正文", "location": "1"}], [], {"criteria": []}, {}, None,
    )

    with pytest.raises(ValueError, match="sampling"):
        scorer.score_envelope(envelope)
    assert capture.requests == []


# -- error recognition -------------------------------------------------------------


def _error(status, body, headers=None):
    request = httpx.Request("POST", BEDROCK + "/messages")
    response = httpx.Response(status, json=body, headers=headers or {}, request=request)
    return httpx.HTTPStatusError("x", request=request, response=response)


@pytest.mark.parametrize("status, error_type, message, expected, retryable", [
    (529, "overloaded_error", "Overloaded", "capacity_unavailable", True),
    (402, "billing_error", "payment", "quota_exhausted", False),
    (400, "invalid_request_error", "You have reached your specified API usage limits. You will regain access on 2026-11-01.",
     "quota_exhausted", False),
    (400, "invalid_request_error", "You have reached your specified workspace API usage limits.", "quota_exhausted", False),
    (400, "invalid_request_error", "prompt is too long: 215000 tokens > 200000 maximum", "context_length_exceeded", False),
    (400, "invalid_request_error", "temperature: not supported for this model", "invalid_request", False),
    (401, "authentication_error", "invalid x-api-key", "authentication_failed", False),
    (403, "permission_error", "no access", "permission_denied", False),
    (429, "rate_limit_error", "Number of requests has exceeded your rate limit", "rate_limited", True),
    (500, "api_error", "internal", "provider_unavailable", True),
    (504, "timeout_error", "timeout", "provider_unavailable", True),
])
def test_claude_error_types_project_onto_the_neutral_codes(status, error_type, message, expected, retryable):
    projected = project_provider_error(_error(status, {"type": "error", "error": {"type": error_type, "message": message},
                                                       "request_id": "req_x"}))
    assert (projected.code, projected.retryable) == (expected, retryable)
    assert projected.provider_error_type == error_type


def test_spend_cap_429_is_quota_and_headers_are_kept():
    headers = {
        "request-id": "req_011", "anthropic-ratelimit-requests-remaining": "0",
        "anthropic-ratelimit-output-tokens-reset": "2026-10-09T10:00:00Z",
    }
    projected = project_provider_error(_error(429, {"type": "error", "error": {
        "type": "rate_limit_error", "message": "monthly cap", "details": {"error_code": "enforced_spend_limit_reached"},
    }}, headers))
    assert projected.code == "quota_exhausted" and projected.retryable is False
    assert projected.provider_error_code == "enforced_spend_limit_reached"
    assert projected.provider_request_id == "req_011"
    assert projected.rate_limit_headers["anthropic-ratelimit-requests-remaining"] == "0"


def test_bedrock_error_type_header_and_request_id_are_used_for_diagnostics():
    projected = project_provider_error(_error(
        429, {"message": "Too many requests, please wait before trying again."},
        {"x-amzn-errortype": "ThrottlingException:http://internal.amazon.com/coral/com.amazon.bedrock/",
         "x-amzn-requestid": "aws-req-1"},
    ))
    assert projected.code == "rate_limited" and projected.retryable is True
    assert projected.provider_error_type == "ThrottlingException"
    assert projected.provider_request_id == "aws-req-1"


def test_a_spend_limit_lookalike_on_another_status_stays_what_it_is():
    projected = project_provider_error(_error(404, {"error": {
        "type": "invalid_request_error", "message": "You have reached your specified API usage limits"}}))
    assert projected.code == "model_or_endpoint_not_found"


def test_debug_log_redacts_the_x_api_key_header(monkeypatch, caplog):
    monkeypatch.setattr(settings, "LLM_DEBUG_LOG_ENABLED", True)
    caplog.set_level(logging.INFO, logger="paper_grading.llm")

    log_llm_request("anthropic", BEDROCK, {"x-api-key": KEY, "anthropic-version": "2023-06-01"}, {}, 0, 1)

    assert KEY not in caplog.text
    assert "***REDACTED***" in caplog.text


# -- connection options and the test probe -----------------------------------------


@pytest.mark.parametrize("provider_type, options, ok", [
    ("anthropic_messages", {"effort": "low", "structured_output": "off", "thinking_type": "adaptive"}, True),
    ("anthropic_messages", {"temperature": 0.2}, True),
    ("anthropic_messages", {"temperature": 0.2, "top_p": 0.9}, False),
    ("anthropic_messages", {"thinking_type": "enabled"}, False),
    ("anthropic_messages", {"response_format_json": True}, False),
    ("anthropic_messages", {"service_tier": "flex"}, False),
    ("anthropic_messages", {"max_output_tokens": 100}, False),
    ("anthropic_messages", {"effort": "turbo"}, False),
    ("anthropic_messages", {"structured_output": "strict"}, False),
    ("openai_compatible", {"effort": "low"}, False),
    ("openai_responses", {"structured_output": "off"}, False),
    ("openai_compatible", {"temperature": 0.2, "top_p": 0.9, "thinking_type": "enabled"}, True),
])
def test_options_are_validated_for_the_resolved_protocol(provider_type, options, ok):
    if ok:
        assert validate_provider_options_for(provider_type, options) == options
    else:
        with pytest.raises(ValueError):
            validate_provider_options_for(provider_type, options)


class _ProbeClient:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def __call__(self, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, url, *, json, headers):
        self.calls.append((url, json, headers))
        return httpx.Response(self.status, json={}, request=httpx.Request("POST", url))


def _runtime(base_url, **options):
    return ConnectionRuntime(
        connection_id="c", key_version=1, organization_id="o", provider_type="anthropic_messages",
        base_url=base_url, model_name="anthropic.claude-opus-5-5", provider_options=options, api_key=KEY,
    )


@pytest.mark.parametrize("base_url, has_format", [(ANTHROPIC, True), (MANTLE, False)])
def test_connection_test_sends_the_same_controls_as_scoring(base_url, has_format, monkeypatch):
    client = _ProbeClient(200)
    monkeypatch.setattr("backend.app.services.ai_connections.validate_outbound_base_url", lambda value: value)
    monkeypatch.setattr("backend.app.services.ai_connections.httpx.Client", client)

    result = verify_connection_runtime(_runtime(base_url, effort="low", thinking_type="adaptive", temperature=0.4))

    assert result == {"provider_type": "anthropic_messages", "model_name": "anthropic.claude-opus-5-5"}
    url, payload, headers = client.calls[0]
    assert url == base_url + "/messages"
    assert headers == {"x-api-key": KEY, "anthropic-version": "2023-06-01"}
    assert payload["temperature"] == 0.4
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"]["effort"] == "low"
    assert ("format" in payload["output_config"]) is has_format


@pytest.mark.parametrize("status, missing, hint", [(404, True, False), (405, True, False), (400, False, True), (401, False, False)])
def test_connection_test_failures_stay_generic_but_400_points_at_the_settings(status, missing, hint, monkeypatch):
    monkeypatch.setattr("backend.app.services.ai_connections.validate_outbound_base_url", lambda value: value)
    monkeypatch.setattr("backend.app.services.ai_connections.httpx.Client", _ProbeClient(status))

    with pytest.raises(ValueError) as caught:
        verify_connection_runtime(_runtime(MANTLE))

    assert isinstance(caught.value, ProtocolEndpointMissing) is missing
    assert str(caught.value).startswith("AI connection test failed")
    assert ("结构化输出" in str(caught.value)) is hint
    assert KEY not in str(caught.value)


def test_creating_a_claude_connection_detects_and_normalizes_offline(client, monkeypatch):
    from backend.app.tests.test_ai_connections import _login

    _login(client, monkeypatch, "claude-owner")
    monkeypatch.setattr(
        "backend.app.api.routes.ai_connections.verify_connection_runtime",
        lambda runtime: pytest.fail("a URL that decides the protocol must not be probed on save"),
    )

    response = client.post("/api/ai-connections", json={
        "name": "Bedrock Claude", "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com/anthropic",
        "model_name": "anthropic.claude-opus-5-5", "provider_options": {"effort": "low"},
        "api_key": KEY,
    })

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["provider_type"] == "anthropic_messages"
    assert body["base_url"] == BEDROCK
    assert body["provider_options"] == {"effort": "low"}
    assert KEY not in response.text

    patched = client.patch("/api/ai-connections/%s" % body["id"], json={
        "base_url": "https://bedrock-mantle.us-east-1.api.aws/anthropic/v1/messages",
        "provider_options": {"structured_output": "off"},
    })
    assert patched.status_code == 200, patched.text
    assert patched.json()["base_url"] == MANTLE
    rejected = client.patch("/api/ai-connections/%s" % body["id"], json={"provider_options": {"service_tier": "flex"}})
    assert rejected.status_code == 400


def test_structure_recognition_treats_claude_truncation_like_the_others():
    from backend.app.services.rubric_import.extraction import llm_structure

    class Truncating:
        provider = "anthropic"

        def complete_json(self, instructions, payload, *, default_max_tokens=None):
            raise MessagesJSONOutputError("output_truncated")

    with pytest.raises(llm_structure.StructureError) as caught:
        llm_structure.recognize_structure([], Truncating(), failure_codes=[])
    assert caught.value.code == "STRUCTURE_OUTPUT_TRUNCATED"
