import json as jsonlib

import pytest

from backend.app.services.llm.core_adapter import core_runtime_provider_contract
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
    assert jsonlib.loads(client.payload["input"]) == value


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
    assert jsonlib.loads(client.payload["messages"][1]["content"]) == (
        envelope.to_mapping()
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
