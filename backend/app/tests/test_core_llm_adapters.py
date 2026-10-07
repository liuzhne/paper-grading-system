import json as jsonlib

import pytest

from backend.app.services.llm.core_adapter import core_runtime_provider_contract
from backend.app.services.llm.core_view import build_core_request
from backend.app.services.llm.openai_adapter import OpenAIResponsesScorer
from backend.app.services.llm.openai_compatible_adapter import (
    OpenAICompatibleChatScorer,
)
from backend.app.services.scoring.core.rule_executor import _prompt_envelope
from backend.app.services.scoring.profiles.thesis import ThesisLLMRuntime
from backend.app.services.scoring.profiles.thesis import ThesisProfile
from backend.app.services.scoring.profiles.technical_proposal import (
    TechnicalProposalProfile,
)
from backend.app.tests.m2_contract_fixtures import PROFILE_KEY
from backend.app.tests.m2_contract_fixtures import PROFILE_VERSION
from backend.app.tests.m3_contract_fixtures import scoring_request_payload


class _FixtureProfile:
    profile_key = PROFILE_KEY
    profile_version = PROFILE_VERSION
    prompt_version = "technical-proposal-prompt@1"

    def build_prompt_extensions(self, *, submission_snapshot, document_snapshot):
        assert submission_snapshot["profile_key"] == self.profile_key
        assert document_snapshot["profile_key"] == self.profile_key
        return {"instructions": {"ignore_untrusted_document_instructions": True}}


def _core_envelope(scorer):
    request = scoring_request_payload()
    request["runtime_identity"]["provider"] = core_runtime_provider_contract(
        scorer,
        artifact_hash="8" * 64,
    )
    node = next(
        item
        for item in request["plan"]["nodes"]
        if item["atomic_rule_snapshot"]["judge_type"] == "semantic"
    )
    return _prompt_envelope(
        request=request,
        node=node,
        profile=_FixtureProfile(),
    )


def _provider_response(envelope, *, quote=None):
    value = envelope.to_mapping()
    rule = value["atomic_rule_snapshot"]
    unit = value["evidence_units"][0]
    return {
        "schema_version": "semantic-rule-response@2",
        "rule_code": rule["rule_code"],
        "status": "triggered",
        "level_code": rule["levels"][0]["level_code"],
        "occurrences": [
            {
                "finding_code": "legacy.atomic.%s" % rule["rule_code"],
                "evidence": [
                    {
                        "type": "source_quote",
                        "evidence_unit_id": unit["evidence_unit_id"],
                        "quote": quote or unit["normalized_text"],
                    }
                ],
            }
        ],
    }


_IDENTITY_ONLY_FIELDS = {
    "runtime_identity",
    "rubric_identity",
    "rubric_snapshot_hash",
    "plan_hash",
    "policy_hash",
    "submission",
    "evidence_selection_identity",
    "token_budget_identity",
    "profile_prompt_extensions",
}


def _assert_is_provider_view(view, envelope_value):
    assert not _IDENTITY_ONLY_FIELDS & set(view)
    assert view["rule"]["rule_code"] == envelope_value["atomic_rule_snapshot"]["rule_code"]
    assert [item["ref"] for item in view["evidence"]] == [
        "E%d" % index for index in range(1, len(view["evidence"]) + 1)
    ]
    serialized = jsonlib.dumps(view, ensure_ascii=False)
    for unit in envelope_value["evidence_units"]:
        assert unit["evidence_unit_id"] not in serialized
    metadata = envelope_value["profile_prompt_extensions"].get("metadata") or {}
    for value in metadata.values():
        if isinstance(value, str) and len(value) > 3:
            assert value not in serialized


class _Response:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, payload):
        self.payload = payload
        self.text = jsonlib.dumps(payload, ensure_ascii=False)

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _OpenAICoreClient:
    def __init__(self):
        self.payload = None
        self.output = None

    def post(self, url, headers, json):
        assert url.endswith("/responses")
        assert headers["Authorization"] == "Bearer test-key"
        self.payload = json
        return _Response(
            {
                "id": "resp_core",
                "output_text": jsonlib.dumps(
                    self.output,
                    ensure_ascii=False,
                ),
            }
        )


class _CompatibleCoreClient:
    def __init__(self):
        self.payload = None
        self.output = None

    def post(self, url, headers, json):
        assert url.endswith("/chat/completions")
        assert headers["Authorization"] == "Bearer test-key"
        self.payload = json
        return _Response(
            {
                "id": "chatcmpl_core",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": jsonlib.dumps(
                                self.output,
                                ensure_ascii=False,
                            ),
                        }
                    }
                ],
            }
        )


def test_openai_responses_scores_prompt_envelope_v3_without_provider_authority():
    client = _OpenAICoreClient()
    scorer = OpenAIResponsesScorer(
        api_key="test-key",
        model_name="gpt-core-test",
        client=client,
        temperature=0,
        max_output_tokens=900,
    )
    envelope = _core_envelope(scorer)
    client.output = _provider_response(envelope)

    output = ThesisLLMRuntime(scorer).score(envelope=envelope)

    value = envelope.to_mapping()
    assert output["rule_code"] == value["atomic_rule_snapshot"]["rule_code"]
    assert output["occurrences"][0]["locator"] == value["evidence_units"][0][
        "locator"
    ]
    assert set(output) == {
        "schema_version",
        "rule_code",
        "status",
        "level_code",
        "occurrences",
    }
    assert client.payload["model"] == "gpt-core-test"
    assert client.payload["max_output_tokens"] == 900
    assert "seed" not in client.payload
    assert client.payload["text"]["format"]["strict"] is True
    assert "points" not in jsonlib.dumps(
        client.payload["text"]["format"]["schema"],
        ensure_ascii=False,
    )
    # Only the derived provider view is sent; identity stays local.
    assert client.payload["input"] == build_core_request(envelope).user
    _assert_is_provider_view(jsonlib.loads(client.payload["input"]), value)


@pytest.mark.parametrize("json_mode", [False, True])
def test_openai_compatible_scores_prompt_envelope_v3_with_connection_controls(
    json_mode,
):
    client = _CompatibleCoreClient()
    scorer = OpenAICompatibleChatScorer(
        api_key="test-key",
        base_url="https://provider.example/v1",
        model_name="compatible-core-test",
        client=client,
        temperature=0.2,
        max_tokens=700,
        response_format_json=json_mode,
        thinking_type="",
        service_tier="flex",
    )
    envelope = _core_envelope(scorer)
    client.output = _provider_response(envelope)

    output = ThesisLLMRuntime(scorer).score(envelope=envelope)

    assert output["status"] == "triggered"
    assert client.payload["model"] == "compatible-core-test"
    assert client.payload["temperature"] == 0.2
    assert client.payload["max_tokens"] == 700
    if json_mode:
        assert client.payload["response_format"] == {"type": "json_object"}
    else:
        assert "response_format" not in client.payload
    assert "seed" not in client.payload
    assert "thinking" not in client.payload
    assert client.payload["service_tier"] == "flex"
    assert client.payload["messages"][1]["content"] == build_core_request(envelope).user
    _assert_is_provider_view(
        jsonlib.loads(client.payload["messages"][1]["content"]), envelope.to_mapping()
    )


@pytest.mark.parametrize("profile", [ThesisProfile(), TechnicalProposalProfile()])
def test_production_profile_freezes_real_connection_controls(profile):
    scorer = OpenAICompatibleChatScorer(
        api_key="test-key",
        base_url="https://provider.example/v1",
        model_name="compatible-core-test",
        client=_CompatibleCoreClient(),
        temperature=0.2,
        max_tokens=700,
        response_format_json=False,
        thinking_type="",
    )

    provider = profile.build_runtime_identity(scorer)["provider"]

    assert provider["name"] == "openai_compatible"
    assert provider["model"] == "compatible-core-test"
    assert provider["sampling"] == {
        "temperature": "0.2",
        "top_p": "1",
        "seed": None,
        "max_tokens": 700,
    }
    assert provider["thinking"] == {"enabled": False, "type": None}
    assert provider["response_format"] == "none"
    assert provider["response_schema"] == "atomic-rule-decisions@1"


def test_core_provider_rejects_hallucinated_quote_before_rule_execution():
    client = _CompatibleCoreClient()
    scorer = OpenAICompatibleChatScorer(
        api_key="test-key",
        base_url="https://provider.example/v1",
        model_name="compatible-core-test",
        client=client,
        response_format_json=False,
        thinking_type="",
    )
    envelope = _core_envelope(scorer)
    client.output = _provider_response(envelope, quote="不存在于冻结证据中的内容")

    with pytest.raises(ValueError, match="quote is not authorized"):
        scorer.score_core_envelope(envelope=envelope)


def test_core_provider_rejects_runtime_identity_mismatch_before_network():
    client = _CompatibleCoreClient()
    scorer = OpenAICompatibleChatScorer(
        api_key="test-key",
        base_url="https://provider.example/v1",
        model_name="compatible-core-test",
        client=client,
        response_format_json=False,
        thinking_type="",
    )
    envelope = _core_envelope(scorer).to_mapping()
    envelope["runtime_identity"]["provider"]["model"] = "different-model"

    with pytest.raises(ValueError, match="provider identity does not match"):
        scorer.score_core_envelope(envelope=envelope)

    assert client.payload is None


@pytest.mark.parametrize("adapter", [OpenAIResponsesScorer, OpenAICompatibleChatScorer])
def test_classification_transport_attempt_limit_disables_implicit_timeout_retries(adapter, monkeypatch):
    import httpx
    from backend.app.core.config import settings
    from backend.app.services.llm.errors import ProviderCallError

    monkeypatch.setattr(settings, "OPENAI_MAX_RETRIES", 2)
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_MAX_RETRIES", 2)
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        scorer = adapter(api_key="synthetic-test", base_url="https://classification-test.invalid/v1",
                         model_name="synthetic", client=client)
        with pytest.raises(ProviderCallError) as exc:
            scorer.complete_json("classify", {"units": []}, attempts_limit=1)
        assert exc.value.error.code == "request_timeout"
    assert len(calls) == 1


@pytest.mark.parametrize("adapter", [OpenAIResponsesScorer, OpenAICompatibleChatScorer])
@pytest.mark.parametrize("explicit_timeout, expected", [(None, 120), (25, 25)])
def test_classification_timeout_is_scoped_and_respects_explicit_connection(adapter, explicit_timeout, expected):
    import httpx

    observed = []
    def respond(request):
        observed.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"output_text": '{"items":[]}', "choices": [{"message": {"content": '{"items":[]}'}}]})

    with httpx.Client(transport=httpx.MockTransport(respond), timeout=25) as client:
        scorer = adapter(api_key="synthetic-test", base_url="https://timeout-test.invalid/v1",
                         model_name="synthetic", client=client, timeout_seconds=explicit_timeout)
        scorer.complete_json("classify", {}, default_timeout_seconds=120)
        scorer.complete_json("other task", {})
    assert observed == [expected, 25]


def test_openai_responses_omits_default_top_p_but_keeps_it_in_identity():
    # Bailian kimi-k3 rejects an explicit top_p=1.0 with HTTP 400 even though
    # 1 is the Responses default; every semantic rule of a batch failed so.
    client = _OpenAICoreClient()
    scorer = OpenAIResponsesScorer(
        api_key="test-key",
        model_name="gpt-core-test",
        client=client,
        temperature=0,
    )
    envelope = _core_envelope(scorer)
    client.output = _provider_response(envelope)

    ThesisLLMRuntime(scorer).score(envelope=envelope)

    sampling = envelope.to_mapping()["runtime_identity"]["provider"]["sampling"]
    assert sampling["top_p"] == "1"
    assert "top_p" not in client.payload
    assert client.payload["temperature"] == 0.0


def test_openai_responses_names_reasoning_truncation_for_rule_tasks():
    from backend.app.services.llm.openai_adapter import ResponsesJSONOutputError
    from backend.app.services.scoring.core.failures import (
        project_rule_execution_failure,
    )

    class _TruncatedClient(_OpenAICoreClient):
        def post(self, url, headers, json):
            self.payload = json
            return _Response(
                {
                    "id": "resp_truncated",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [{"type": "reasoning", "content": None}],
                }
            )

    scorer = OpenAIResponsesScorer(
        api_key="test-key",
        model_name="gpt-core-test",
        client=_TruncatedClient(),
        temperature=0,
    )
    envelope = _core_envelope(scorer)

    with pytest.raises(ResponsesJSONOutputError) as caught:
        ThesisLLMRuntime(scorer).score(envelope=envelope)

    assert caught.value.reason == "output_truncated"
    assert project_rule_execution_failure(caught.value)[0] == (
        "PROVIDER_OUTPUT_TRUNCATED"
    )


def test_circuit_open_rule_failure_is_not_reported_as_bad_input():
    from backend.app.services.llm.rate_limit import CircuitOpenError
    from backend.app.services.scoring.core.failures import (
        project_rule_execution_failure,
    )

    code, message = project_rule_execution_failure(CircuitOpenError())

    assert code == "PROVIDER_CIRCUIT_OPEN"
    assert "input" not in message


def test_connection_top_p_reaches_identity_and_chat_payload():
    # Bailian kimi-k3 rejects top_p=1.0; its connection pins 0.95 instead.
    from backend.app.services.ai_connections import ConnectionRuntime
    from backend.app.services.llm.factory import get_llm_scorer

    runtime = ConnectionRuntime(
        connection_id="conn-kimi",
        key_version=1,
        organization_id="org-1",
        provider_type="openai_compatible",
        base_url="https://provider.example/v1",
        model_name="kimi-k3",
        provider_options={"top_p": 0.95, "thinking_type": "disabled"},
        api_key="test-key",
    )
    scorer = get_llm_scorer(runtime)
    scorer.close()
    client = _CompatibleCoreClient()
    scorer.client = client
    scorer._owns_client = False
    envelope = _core_envelope(scorer)
    client.output = _provider_response(envelope)
    ThesisLLMRuntime(scorer).score(envelope=envelope)

    sampling = envelope.to_mapping()["runtime_identity"]["provider"]["sampling"]
    assert sampling["top_p"] == "0.95"
    assert client.payload["top_p"] == 0.95
    assert client.payload["thinking"] == {"type": "disabled"}


def test_connection_top_p_must_be_a_probability_mass():
    from backend.app.services.ai_connections import validate_provider_options

    assert validate_provider_options({"top_p": 0.95}) == {"top_p": 0.95}
    for invalid in (0, 1.5):
        with pytest.raises(ValueError):
            validate_provider_options({"top_p": invalid})
