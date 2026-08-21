"""M1 cache identity, audit projection, retention, and access contracts."""

from __future__ import annotations

import importlib
import hashlib
import inspect
import json
import sqlite3
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from types import SimpleNamespace

import pytest

from backend.app.core.config import settings
from backend.app.services.cache import llm_cache
from backend.app.services.llm.openai_adapter import OpenAIResponsesScorer
from backend.app.services.llm.openai_adapter import _envelope_score_schema
from backend.app.services.llm.openai_compatible_adapter import OpenAICompatibleChatScorer
from backend.app.services.scoring.engine import _score_with_runtime_fallback
from backend.app.services.scoring.validator import coerce_deduction_items
from backend.app.tests.m1_contract_helpers import EVIDENCE_UNIT_1
from backend.app.tests.m1_contract_helpers import prompt_envelope_payload
from backend.app.tests.m1_contract_helpers import refresh_observation_hash


OLD_PROMPT_VERSION = "2026-06-16-1"


def _load(module_name, *names):
    try:
        module = importlib.import_module(module_name)
        return tuple(getattr(module, name) for name in names), None
    except ModuleNotFoundError as exc:
        if exc.name != module_name and not module_name.startswith("%s." % exc.name):
            raise
        return None, exc
    except AttributeError as exc:
        return None, exc


_CONTRACT_API, _CONTRACT_ERROR = _load(
    "backend.app.services.scoring.core.contracts",
    "PromptEnvelopeV1",
)
_CANONICAL_API, _CANONICAL_ERROR = _load(
    "backend.app.services.scoring.core.canonical",
    "canonical_sha256",
)
_LEDGER_API, _LEDGER_ERROR = _load(
    "backend.app.services.cache.llm_cache",
    "CacheRetentionPolicy",
    "put_envelope",
    "get_entry",
    "purge_expired",
    "redact_prompt_envelope",
)
_LEGACY_SNAPSHOT_API, _LEGACY_SNAPSHOT_ERROR = _load(
    "backend.app.services.scoring.adapters.legacy_rubric",
    "adapt_legacy_rubric",
)

_CACHE_RUNTIME_MISSING = (
    _CONTRACT_ERROR is not None
    or _CANONICAL_ERROR is not None
    or _LEDGER_ERROR is not None
    or llm_cache.PROMPT_VERSION == OLD_PROMPT_VERSION
)
requires_cache_runtime = pytest.mark.xfail(
    _CACHE_RUNTIME_MISSING,
    reason="M1 PR-05 PromptEnvelope cache/runtime adapter wiring is not implemented",
    strict=True,
)
requires_production_envelope_build = pytest.mark.xfail(
    _CACHE_RUNTIME_MISSING or _LEGACY_SNAPSHOT_ERROR is not None,
    reason="M1 LegacyRubricAdapter to production LLM PromptEnvelope wiring is not implemented",
    strict=True,
)
requires_ledger = pytest.mark.xfail(
    _CONTRACT_ERROR is not None or _CANONICAL_ERROR is not None or _LEDGER_ERROR is not None,
    reason="M1 controlled cache ledger/retention boundary is not implemented",
    strict=True,
)


def _make_envelope(payload):
    if _CONTRACT_ERROR is not None:
        raise _CONTRACT_ERROR
    envelope_type = _CONTRACT_API[0]
    if hasattr(envelope_type, "from_mapping"):
        return envelope_type.from_mapping(payload)
    if hasattr(envelope_type, "model_validate"):
        return envelope_type.model_validate(payload)
    return envelope_type(**payload)


def _dump(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "to_mapping"):
        return value.to_mapping()
    if hasattr(value, "dict"):
        return value.dict()
    raise AssertionError("cache entry/envelope must expose a mapping projection")


def _field(value, name):
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name)


class _EnvelopeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _CapturingEnvelopeClient:
    def __init__(self, *, api_kind, output):
        self.api_kind = api_kind
        self.output = output
        self.payload = None

    def post(self, _url, headers=None, json=None):
        del headers
        self.payload = json
        if self.api_kind == "responses":
            return _EnvelopeResponse(
                {
                    "id": "resp-envelope",
                    "output_text": __import__("json").dumps(self.output, ensure_ascii=False),
                    "usage": {},
                }
            )
        return _EnvelopeResponse(
            {
                "id": "chat-envelope",
                "choices": [
                    {
                        "message": {
                            "content": __import__("json").dumps(
                                self.output, ensure_ascii=False
                            )
                        }
                    }
                ],
                "usage": {},
            }
        )


def _provider_payload(*, name, model, model_version, response_format, thinking_type=None):
    payload = prompt_envelope_payload()
    thinking_enabled = str(thinking_type).lower() not in {
        "none",
        "disabled",
        "false",
        "off",
    }
    payload["provider"].update(
        {
            "name": name,
            "model": model,
            "model_version": model_version,
            "thinking": {"enabled": thinking_enabled, "type": thinking_type},
            "response_format": response_format,
        }
    )
    payload["provider"]["sampling"].update(
        {"temperature": "0.25", "top_p": "0.8", "seed": None, "max_tokens": 700}
    )
    return payload


def _provider_output(payload, *, banded=False):
    rule_ref = payload["criterion"]["authorized_rules"][0]["code"]
    evidence_ref = "evidence-1"
    output = {
        "criterion_id": payload["criterion"]["code"],
        "criterion_name": payload["criterion"]["name"],
        "max_score": 10,
        "score": 8,
        "evidence_sufficient": True,
        "reason": "基于冻结证据判分",
        "deductions": [],
        "deduction_items": (
            []
            if banded
            else [{"rule_ref": rule_ref, "evidence_refs": [evidence_ref]}]
        ),
        "evidence": [
            {
                "evidence_ref": evidence_ref,
                "type": "source_quote",
                "quote": payload["evidence_units"][0]["text"],
                "location": payload["evidence_units"][0]["location"],
                "evidence_unit_id": payload["evidence_units"][0]["evidence_unit_id"],
            }
        ],
        "suggestion": "",
        "confidence": 0.9,
        "need_manual_review": False,
    }
    if banded:
        output["band_selection"] = {
            "level": "良",
            "rationale": "证据达到良好档",
            "evidence_quote": payload["evidence_units"][0]["text"],
            "evidence_location": payload["evidence_units"][0]["location"],
        }
    return output


def _legacy_rubric_fixture():
    return {
        "id": "cache-contract-rubric-row",
        "name": "PromptEnvelope legacy rubric",
        "version": "legacy-label-v1",
        "total_score": Decimal("10"),
        "status": "published",
        "criteria": [
            {
                "id": "cache-contract-criterion-row",
                "code": "C01",
                "name": "研究方法",
                "max_score": Decimal("10"),
                "weight": None,
                "scoring_mode": "deductive",
                "deduction_rules_structured": [
                    {
                        "match": "未说明数据来源",
                        "points": Decimal("2"),
                        "reason": "数据来源缺失",
                        "source": "excel",
                        "evidence_mode": "source_quote",
                    }
                ],
            }
        ],
    }


def _invoke_envelope_builder(builder, values):
    signature = inspect.signature(builder)
    parameters = signature.parameters
    canonical_names = (
        "paper",
        "criterion",
        "evidence_candidates",
        "structure_checks",
        "rubric_snapshot",
        "policy",
        "anchors",
    )
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        kwargs = {name: values[name] for name in canonical_names}
    else:
        kwargs = {
            name: values[name]
            for name, parameter in parameters.items()
            if name in values and parameter.kind != inspect.Parameter.POSITIONAL_ONLY
        }
        missing = [
            name
            for name, parameter in parameters.items()
            if parameter.default is inspect.Parameter.empty
            and parameter.kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            and name not in kwargs
        ]
        assert not missing, "unsupported PromptEnvelope builder inputs: %s" % ", ".join(missing)
    return builder(**kwargs)


@requires_cache_runtime
def test_cache_key_is_the_hash_of_the_exact_provider_envelope():
    if _CANONICAL_ERROR is not None:
        raise _CANONICAL_ERROR
    envelope = _make_envelope(prompt_envelope_payload())
    expected = _CANONICAL_API[0](_dump(envelope))

    assert llm_cache.key_of(envelope) == expected


@requires_cache_runtime
def test_real_runtime_gives_cache_and_provider_the_same_prompt_envelope_instance(
    cache_ledger,
    monkeypatch,
):
    payload = prompt_envelope_payload()
    payload["provider"].update(
        {
            "name": "contract-test-provider",
            "model": "contract-test-model",
            "model_version": "v1",
        }
    )
    envelope = _make_envelope(payload)
    seen = {}

    class EnvelopeScorer:
        provider = "contract-test-provider"
        model_name = "contract-test-model"
        model_version = "v1"

        def build_prompt_envelope(self, *_args, **_kwargs):
            return envelope

        def score_envelope(self, provider_envelope):
            seen["provider"] = provider_envelope
            return {"score": "8", "usage": {}}

        def score_criterion(self, *_args, **_kwargs):
            raise AssertionError("M1 runtime must send the already-built PromptEnvelope")

    def key_of_exact(cache_envelope):
        seen["key"] = cache_envelope
        return "f" * 64

    def store_exact(*args, **kwargs):
        seen["store"] = kwargs.get("envelope", args[0] if args else None)
        return "f" * 64

    def legacy_store_forbidden(*_args, **_kwargs):
        raise AssertionError("legacy request dictionaries cannot be stored after M1")

    monkeypatch.setattr(settings, "LLM_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_FALLBACK_TO_MOCK", False)
    monkeypatch.setattr(settings, "LLM_RATE_LIMIT_SLEEP_SECONDS", 0)
    monkeypatch.setattr(llm_cache, "key_of", key_of_exact)
    monkeypatch.setattr(llm_cache, "get", lambda _key: None)
    monkeypatch.setattr(llm_cache, "get_entry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(llm_cache, "put_envelope", store_exact)
    monkeypatch.setattr(llm_cache, "put", legacy_store_forbidden)

    result = _score_with_runtime_fallback(
        EnvelopeScorer(),
        paper=object(),
        criterion=object(),
        candidates=[],
        structure_checks=[],
        rubric_version=None,
        anchors=None,
    )

    assert result["score"] == "8"
    assert seen == {"key": envelope, "provider": envelope, "store": envelope}


@requires_production_envelope_build
@pytest.mark.parametrize(
    ("scorer", "expected_provider", "expected_model", "expected_model_version"),
    [
        (
            OpenAIResponsesScorer(
                api_key="test-key",
                base_url="https://openai.invalid/v1",
                model_name="gpt-contract",
                client=object(),
            ),
            "openai",
            "gpt-contract",
            "responses-api",
        ),
        (
            OpenAICompatibleChatScorer(
                api_key="test-key",
                base_url="https://compatible.invalid/v1",
                model_name="glm-contract",
                provider_name="zhipu",
                client=object(),
            ),
            "zhipu",
            "glm-contract",
            "chat-completions",
        ),
    ],
    ids=["openai-responses", "openai-compatible-chat"],
)
def test_production_cloud_adapters_build_envelope_from_actual_legacy_snapshot(
    scorer,
    expected_provider,
    expected_model,
    expected_model_version,
):
    adapt = _LEGACY_SNAPSHOT_API[0]
    snapshot = adapt(_legacy_rubric_fixture(), compilations=[])
    snapshot_criterion = _field(snapshot, "criteria")[0]
    authorized_rules = _field(snapshot_criterion, "authorized_rules")
    policy = MappingProxyType(
        {
            "schema_version": "scoring-policy@1",
            "policy_key": "corrected_thesis_policy",
            "policy_hash": "2" * 64,
            "evidence": MappingProxyType({"default_policy": "required"}),
        }
    )
    paper = SimpleNamespace(
        id="paper-contract",
        title="基于可解释模型的教学质量评价",
        profile_key="thesis",
        profile_version="thesis-v1",
        source_artifact_hash="3" * 64,
        normalized_content_hash="4" * 64,
        document_snapshot_hash="5" * 64,
    )
    criterion = SimpleNamespace(
        id="criterion-contract",
        code=_field(snapshot_criterion, "code"),
        name=_field(snapshot_criterion, "name"),
        max_score=_field(snapshot_criterion, "max_score"),
        scoring_mode="deductive",
        description="评价研究设计与数据来源。",
        evidence_hints=["数据来源"],
        deduction_rules=["未说明来源时扣分"],
        rubric_levels=[],
        authorized_rules=authorized_rules,
    )
    anchors = [
        {
            "label": "优",
            "score": 9,
            "max_score": 10,
            "excerpt": "脱敏范文说明了数据来源。",
            "rationale": "证据充分。",
        }
    ]
    fixture = prompt_envelope_payload()
    values = {
        "paper": paper,
        "criterion": criterion,
        "evidence_candidates": fixture["evidence_units"],
        "candidates": fixture["evidence_units"],
        "structure_checks": fixture["observations"],
        "observations": fixture["observations"],
        "rubric_snapshot": snapshot,
        "rubric_version": snapshot,
        "policy": policy,
        "scoring_policy": policy,
        "anchors": anchors,
        "calibration_anchors": anchors,
        "context": {
            "paper": paper,
            "criterion": criterion,
            "evidence_candidates": fixture["evidence_units"],
            "structure_checks": fixture["observations"],
            "rubric_snapshot": snapshot,
            "policy": policy,
            "anchors": anchors,
        },
    }
    values["request"] = values["context"]
    values["build_input"] = values["context"]
    values["scoring_context"] = values["context"]

    builder = getattr(scorer, "build_prompt_envelope", None)
    sender = getattr(scorer, "score_envelope", None)
    assert callable(builder)
    assert callable(sender)
    envelope = _invoke_envelope_builder(builder, values)
    dumped = _dump(envelope)

    assert isinstance(envelope, _CONTRACT_API[0])
    assert dumped["provider"]["name"] == expected_provider
    assert dumped["provider"]["model"] == expected_model
    assert dumped["provider"]["model_version"] == expected_model_version
    assert "type" in dumped["provider"]["thinking"]
    assert dumped["provider"]["response_format"] in {"json_schema", "json_object", "none"}
    assert dumped["submission"]["title"] == paper.title
    assert dumped["submission"]["document_snapshot_hash"] == paper.document_snapshot_hash
    assert dumped["rubric_snapshot_hash"] == _field(snapshot, "rubric_snapshot_hash")
    assert dumped["policy_hash"] == policy["policy_hash"]
    assert [rule["code"] for rule in dumped["criterion"]["authorized_rules"]] == [
        _field(rule, "code") for rule in authorized_rules
    ]
    assert dumped["criterion"]["authorized_rules"][0]["evidence_mode"] == "source_quote"
    assert dumped["criterion"]["authorized_rules"][0]["absence_target"] is None
    assert dumped["criterion"]["description"] == criterion.description
    assert dumped["criterion"]["evidence_hints"] == criterion.evidence_hints
    assert dumped["criterion"]["deduction_rules"] == criterion.deduction_rules
    assert dumped["calibration_anchors"][0]["excerpt"] == anchors[0]["excerpt"]
    assert dumped["evidence_units"][0]["location"] == "第三章/3.2"
    assert dumped["observations"][0]["checker_version"] == "1.0.0"
    assert dumped["coverage"]["scope_selector"] == "retrieval://candidate-units"
    assert dumped["coverage"]["authoritative_for_absence"] is False
    assert dumped["coverage"]["evidence_policy_version"] == "evidence-policy-v1"


@requires_cache_runtime
def test_openai_envelope_request_uses_only_frozen_provider_controls(monkeypatch):
    payload = _provider_payload(
        name="openai",
        model="gpt-envelope",
        model_version="responses-api",
        response_format="json_schema",
    )
    client = _CapturingEnvelopeClient(
        api_kind="responses",
        output=_provider_output(payload),
    )
    scorer = OpenAIResponsesScorer(
        api_key="test-key",
        base_url="https://openai.invalid/v1",
        model_name="gpt-envelope",
        client=client,
    )
    envelope = _make_envelope(payload)
    monkeypatch.setattr(settings, "OPENAI_TEMPERATURE", 1.75)
    monkeypatch.setattr(settings, "OPENAI_MAX_OUTPUT_TOKENS", 99)

    scorer.score_envelope(envelope)

    assert client.payload["model"] == "gpt-envelope"
    assert client.payload["temperature"] == 0.25
    assert client.payload["top_p"] == 0.8
    assert client.payload["max_output_tokens"] == 700
    assert client.payload["text"]["format"]["type"] == "json_schema"
    deduction_schema = client.payload["text"]["format"]["schema"]["properties"][
        "deduction_items"
    ]["items"]
    assert deduction_schema["required"] == ["rule_ref", "evidence_refs"]
    assert set(deduction_schema["properties"]) == {"rule_ref", "evidence_refs"}
    assert deduction_schema["properties"]["evidence_refs"]["minItems"] == 1
    evidence_schema = client.payload["text"]["format"]["schema"]["properties"][
        "evidence"
    ]["items"]
    assert "evidence_ref" in evidence_schema["required"]
    assert "务必在 deduction_items 给出每个扣分点的 points" not in client.payload["instructions"]
    assert "不得返回 points" in client.payload["instructions"]
    assert "evidence_refs" in client.payload["instructions"]


@requires_cache_runtime
def test_compatible_envelope_freezes_thinking_response_format_and_banded_schema(monkeypatch):
    payload = _provider_payload(
        name="zhipu",
        model="glm-envelope",
        model_version="chat-completions",
        response_format="json_object",
        thinking_type="enabled",
    )
    payload["provider"]["sampling"]["seed"] = 17
    payload["criterion"]["scoring_mode"] = "banded"
    payload["criterion"]["rubric_levels"] = [
        {"label": "优", "points": "10", "descriptor": "方法证据充分"},
        {"label": "良", "points": "8", "descriptor": "方法证据较充分"},
    ]
    client = _CapturingEnvelopeClient(
        api_kind="chat",
        output=_provider_output(payload, banded=True),
    )
    scorer = OpenAICompatibleChatScorer(
        api_key="test-key",
        base_url="https://compatible.invalid/v1",
        model_name="glm-envelope",
        provider_name="zhipu",
        client=client,
    )
    envelope = _make_envelope(payload)
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_THINKING_TYPE", "disabled")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON", False)
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_TEMPERATURE", 1.5)

    scorer.score_envelope(envelope)

    assert client.payload["model"] == "glm-envelope"
    assert client.payload["temperature"] == 0.25
    assert client.payload["top_p"] == 0.8
    assert client.payload["seed"] == 17
    assert client.payload["max_tokens"] == 700
    assert client.payload["thinking"] == {"type": "enabled"}
    assert client.payload["response_format"] == {"type": "json_object"}
    system_instruction = client.payload["messages"][0]["content"]
    assert "band_selection" in system_instruction
    assert "务必在 deduction_items 给出每个扣分点的 points" not in system_instruction
    sent_envelope = json.loads(client.payload["messages"][1]["content"])
    assert sent_envelope["criterion"]["rubric_levels"] == payload["criterion"]["rubric_levels"]
    assert sent_envelope["calibration_anchors"] == payload["calibration_anchors"]


@requires_cache_runtime
@pytest.mark.parametrize("field", ["name", "model", "model_version"])
def test_score_envelope_rejects_provider_identity_mismatch(field):
    payload = _provider_payload(
        name="openai",
        model="gpt-envelope",
        model_version="responses-api",
        response_format="json_schema",
    )
    payload["provider"][field] = "mismatched-value"
    client = _CapturingEnvelopeClient(
        api_kind="responses",
        output=_provider_output(payload),
    )
    scorer = OpenAIResponsesScorer(
        api_key="test-key",
        base_url="https://openai.invalid/v1",
        model_name="gpt-envelope",
        client=client,
    )

    with pytest.raises(ValueError, match="provider identity|%s" % field):
        scorer.score_envelope(_make_envelope(payload))
    assert client.payload is None


def test_evidence_bound_deduction_effect_survives_legacy_output_normalization():
    rule_ref = "LEGACY:C01:" + "a" * 64
    assert coerce_deduction_items(
        [{"rule_ref": rule_ref, "evidence_refs": ["evidence-1"]}]
    ) == [
        {
            "points": None,
            "reason": "",
            "rule_ref": rule_ref,
            "evidence_location": "",
            "evidence_quote": "",
            "evidence_refs": ["evidence-1"],
        }
    ]


def test_banded_provider_schema_requires_structured_band_selection():
    schema = _envelope_score_schema("banded")

    assert "band_selection" in schema["required"]
    band = schema["properties"]["band_selection"]
    assert set(band["required"]) == {
        "level",
        "rationale",
        "evidence_quote",
        "evidence_location",
    }


_IDENTITY_MUTATIONS = [
    (("prompt_version",), llm_cache.PROMPT_VERSION + "-changed"),
    (("profile", "key"), "technical_proposal"),
    (("profile", "prompt_version"), "proposal-prompt-v1"),
    (("engine", "version"), "legacy-m1-adapter-v2"),
    (("provider", "name"), "openai"),
    (("provider", "model"), "different-model"),
    (("provider", "model_version"), "v2"),
    (("provider", "sampling", "temperature"), "0.2"),
    (("provider", "sampling", "top_p"), "0.9"),
    (("provider", "sampling", "seed"), 99),
    (("provider", "sampling", "max_tokens"), 1024),
    (("provider", "thinking", "enabled"), True),
    (("provider", "thinking", "type"), "enabled"),
    (("provider", "response_format"), "json_object"),
    (("provider", "response_schema"), "criterion-score-v3"),
    (("rubric_snapshot_hash",), "8" * 64),
    (("policy_hash",), "9" * 64),
    (("criterion", "code"), "C02"),
    (("criterion", "name"), "实验结果"),
    (("criterion", "max_score"), "20"),
    (("criterion", "scoring_mode"), "banded"),
    (("criterion", "description"), "新的评分说明"),
    (("criterion", "evidence_hints"), ["实验设计"]),
    (("criterion", "deduction_rules"), ["缺少实验设计时扣分"]),
    (("criterion", "rubric_levels"), [{"label": "优", "points": "10"}]),
    (("criterion", "authorized_rules", 0, "points"), "3"),
    (("criterion", "authorized_rules", 0, "description"), "规则文本变化"),
    (("criterion", "authorized_rules", 0, "evidence_mode"), "review_only"),
    (("criterion", "authorized_rules", 0, "absence_target"), "risk_owner"),
    (("submission", "title"), "标题变化"),
    (("submission", "source_artifact_hash"), "a" * 64),
    (("submission", "normalized_content_hash"), "b" * 64),
    (("submission", "document_snapshot_hash"), "c" * 64),
    (("evidence_units", 0, "text"), "证据正文变化"),
    (("evidence_units", 0, "location"), "第三章/3.3"),
    (("evidence_units", 0, "section_title"), "3.3 实验设计"),
    (("observations", 0, "checker_key"), "thesis.structure.required_sections.v2"),
    (("observations", 0, "checker_version"), "2.0.0"),
    (("observations", 0, "locator", "section_ordinal"), 4),
    (("observations", 0, "observation_code"), "METHOD_SECTION_MISSING"),
    (("observations", 0, "measured_value"), False),
    (("observations", 0, "expected_value"), False),
    (("calibration_anchors", 0, "excerpt"), "新的脱敏范文内容"),
    (("coverage", "declared_scope"), ["全文"]),
    (("coverage", "scope_selector"), "retrieval://reranked-candidate-units"),
    (("coverage", "authoritative_for_absence"), True),
    (("coverage", "expected_evidence_unit_ids"), [EVIDENCE_UNIT_1]),
    (("coverage", "checked_evidence_unit_ids"), [EVIDENCE_UNIT_1]),
    (("coverage", "completeness"), "partial"),
    (("coverage", "evidence_policy_version"), "thesis-evidence-v2"),
]


def _synchronize_dependent_identity(payload, path):
    """Keep identity fixtures semantically valid after an upstream change."""

    if path[:1] == ("profile",):
        payload["rubric_snapshot_hash"] = "a" * 64
        payload["policy_hash"] = "b" * 64
        payload["submission"]["document_snapshot_hash"] = "c" * 64
        if path == ("profile", "key"):
            payload["criterion"]["name"] = "技术方案完整性"

    if path[:1] == ("criterion",):
        payload["rubric_snapshot_hash"] = "a" * 64
        if path == ("criterion", "code"):
            payload["criterion"]["authorized_rules"][0]["code"] = (
                "LEGACY:%s:%s" % (payload["criterion"]["code"], "a" * 64)
            )
        if path == ("criterion", "max_score"):
            payload["policy_hash"] = "b" * 64
        if path[:3] == ("criterion", "authorized_rules", 0):
            payload["criterion"]["authorized_rules"][0]["code"] = (
                "LEGACY:%s:%s" % (payload["criterion"]["code"], "b" * 64)
            )
        if path == ("criterion", "authorized_rules", 0, "absence_target"):
            payload["criterion"]["authorized_rules"][0][
                "evidence_mode"
            ] = "scoped_absence"

    if path == ("provider", "thinking", "enabled"):
        payload["provider"]["thinking"]["type"] = "enabled"
    if path == ("provider", "thinking", "type"):
        payload["provider"]["thinking"]["enabled"] = True

    if path[:1] == ("calibration_anchors",):
        payload["calibration_anchors_hash"] = llm_cache.canonical_sha256(
            payload["calibration_anchors"]
        )

    if path == ("submission", "normalized_content_hash"):
        payload["submission"]["document_snapshot_hash"] = "c" * 64
        _replace_evidence_unit_ids(payload)

    if path[:1] == ("evidence_units",):
        payload["submission"]["source_artifact_hash"] = "a" * 64
        payload["submission"]["normalized_content_hash"] = "b" * 64
        payload["submission"]["document_snapshot_hash"] = "c" * 64
        _replace_evidence_unit_ids(payload)

    if path[:1] == ("observations",):
        refresh_observation_hash(payload["observations"][path[1]])
        payload["submission"]["document_snapshot_hash"] = "c" * 64

    if path == ("coverage", "checked_evidence_unit_ids"):
        expected = set(payload["coverage"]["expected_evidence_unit_ids"])
        checked = set(payload["coverage"]["checked_evidence_unit_ids"])
        payload["coverage"]["completeness"] = "complete" if checked == expected else "partial"

    if path == ("coverage", "expected_evidence_unit_ids"):
        expected = set(payload["coverage"]["expected_evidence_unit_ids"])
        payload["coverage"]["checked_evidence_unit_ids"] = [
            unit_id
            for unit_id in payload["coverage"]["checked_evidence_unit_ids"]
            if unit_id in expected
        ]
        payload["coverage"]["completeness"] = "complete"

    if path == ("coverage", "completeness") and payload["coverage"]["completeness"] == "partial":
        payload["coverage"]["checked_evidence_unit_ids"] = [
            payload["coverage"]["expected_evidence_unit_ids"][0]
        ]

    if path == ("coverage", "evidence_policy_version"):
        payload["policy_hash"] = "b" * 64

    if path == ("coverage", "declared_scope"):
        payload["policy_hash"] = "b" * 64

    if path == ("coverage", "authoritative_for_absence"):
        payload["coverage"]["scope_selector"] = "document://all-units"


def _replace_evidence_unit_ids(payload):
    replacements = {}
    for index, unit in enumerate(payload["evidence_units"], start=1):
        old = unit["evidence_unit_id"]
        new = hashlib.sha256(("changed-evidence-unit-%02d" % index).encode("utf-8")).hexdigest()
        unit["evidence_unit_id"] = new
        replacements[old] = new
    for field in ("expected_evidence_unit_ids", "checked_evidence_unit_ids"):
        payload["coverage"][field] = [replacements.get(value, value) for value in payload["coverage"][field]]


@requires_cache_runtime
@pytest.mark.parametrize(("path", "changed_value"), _IDENTITY_MUTATIONS)
def test_every_scoring_relevant_input_change_causes_cache_miss(path, changed_value):
    base_payload = prompt_envelope_payload()
    changed_payload = deepcopy(base_payload)
    cursor = changed_payload
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = changed_value
    _synchronize_dependent_identity(changed_payload, path)

    base = llm_cache.key_of(_make_envelope(base_payload))
    changed = llm_cache.key_of(_make_envelope(changed_payload))

    assert changed != base, "cache identity ignored %s" % ".".join(map(str, path))


@requires_cache_runtime
def test_expected_and_checked_evidence_unit_sets_have_order_independent_cache_identity():
    base_payload = prompt_envelope_payload()
    reordered = deepcopy(base_payload)
    reordered["coverage"]["expected_evidence_unit_ids"].reverse()
    reordered["coverage"]["checked_evidence_unit_ids"].reverse()

    assert llm_cache.key_of(_make_envelope(reordered)) == llm_cache.key_of(_make_envelope(base_payload))


@pytest.mark.xfail(
    llm_cache.PROMPT_VERSION == OLD_PROMPT_VERSION,
    reason="M1 provider-ready PromptEnvelope changes require a cache version bump",
    strict=True,
)
def test_m1_prompt_change_bumps_prompt_version():
    assert llm_cache.PROMPT_VERSION != OLD_PROMPT_VERSION


@pytest.fixture()
def cache_ledger(monkeypatch, tmp_path):
    if _LEDGER_ERROR is not None:
        raise _LEDGER_ERROR
    if _CONTRACT_ERROR is not None:
        raise _CONTRACT_ERROR
    if _CANONICAL_ERROR is not None:
        raise _CANONICAL_ERROR
    monkeypatch.setattr(settings, "STORAGE_ROOT", tmp_path)
    return _LEDGER_API


@requires_ledger
def test_cache_ledger_separates_original_envelope_from_redacted_audit_projection(cache_ledger):
    retention_type, put_envelope, get_entry, _, redact = cache_ledger
    envelope = _make_envelope(prompt_envelope_payload())
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    policy = retention_type(
        retention_seconds=3600,
        store_controlled_original=True,
        allowed_scopes=("scoring", "audit"),
    )

    stored = put_envelope(
        envelope=envelope,
        response={"score": "8"},
        policy=policy,
        access_scope="scoring",
        now=now,
    )
    key = stored if isinstance(stored, str) else _field(stored, "key")
    entry = get_entry(key, requester_scope="audit", now=now)
    projection = _dump(redact(envelope))

    assert _field(entry, "original_envelope_hash") == key
    assert _dump(_field(entry, "original_envelope")) == _dump(envelope)
    assert _dump(_field(entry, "redacted_audit_projection")) == projection
    assert projection != _dump(envelope)
    assert "基于可解释模型的教学质量评价" not in str(projection)
    assert "第三章说明了问卷来源和回归分析方法" not in str(projection)
    # Calibration anchors are curated/de-identified audit inputs, not student
    # content, so their exact rationale remains available to authorized audit.
    assert projection["calibration_anchors"][0]["excerpt"] == (
        "范文明确说明了数据来源和回归分析步骤。"
    )


@requires_ledger
def test_cache_access_scope_is_enforced_and_expired_entries_are_not_returned(cache_ledger):
    retention_type, put_envelope, get_entry, _, _ = cache_ledger
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    policy = retention_type(
        retention_seconds=60,
        store_controlled_original=False,
        allowed_scopes=("scoring",),
    )
    stored = put_envelope(
        envelope=_make_envelope(prompt_envelope_payload()),
        response={"score": "8"},
        policy=policy,
        access_scope="scoring",
        now=now,
    )
    key = stored if isinstance(stored, str) else _field(stored, "key")

    try:
        unauthorized = get_entry(key, requester_scope="audit", now=now)
    except (PermissionError, LookupError):
        unauthorized = None
    assert unauthorized is None
    entry = get_entry(key, requester_scope="scoring", now=now + timedelta(seconds=59))
    assert entry is not None
    assert _field(entry, "original_envelope") is None
    assert get_entry(key, requester_scope="scoring", now=now + timedelta(seconds=61)) is None


@requires_ledger
def test_cache_retention_purge_makes_expired_entry_unavailable(cache_ledger):
    retention_type, put_envelope, get_entry, purge_expired, _ = cache_ledger
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    policy = retention_type(
        retention_seconds=60,
        store_controlled_original=False,
        allowed_scopes=("scoring",),
    )
    stored = put_envelope(
        envelope=_make_envelope(prompt_envelope_payload()),
        response={"score": "8"},
        policy=policy,
        access_scope="scoring",
        now=now,
    )
    key = stored if isinstance(stored, str) else _field(stored, "key")

    purge_expired(now=now + timedelta(seconds=61))
    assert get_entry(key, requester_scope="scoring", now=now + timedelta(seconds=61)) is None


@requires_ledger
def test_cache_envelope_first_write_is_immutable_and_cannot_expand_access(cache_ledger):
    retention_type, put_envelope, get_entry, _, _ = cache_ledger
    envelope = _make_envelope(prompt_envelope_payload())
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    key = put_envelope(
        envelope=envelope,
        response={"version": 1},
        policy=retention_type(
            retention_seconds=60,
            store_controlled_original=False,
            allowed_scopes=("scoring",),
        ),
        access_scope="scoring",
        now=now,
    )

    second_key = put_envelope(
        envelope=envelope,
        response={"version": 2},
        policy=retention_type(
            retention_seconds=3600,
            store_controlled_original=True,
            allowed_scopes=("scoring", "audit"),
        ),
        access_scope="audit",
        now=now + timedelta(seconds=10),
    )

    assert second_key == key
    assert get_entry(key, requester_scope="audit", now=now + timedelta(seconds=30)) is None
    entry = get_entry(key, requester_scope="scoring", now=now + timedelta(seconds=30))
    assert _field(entry, "response") == {"version": 1}
    assert _field(entry, "original_envelope") is None
    assert _field(entry, "allowed_scopes") == ("scoring",)
    assert _field(entry, "expires_at") == now + timedelta(seconds=60)


@requires_ledger
def test_cache_read_rejects_stored_identity_or_original_mismatch(cache_ledger):
    retention_type, put_envelope, get_entry, _, _ = cache_ledger
    envelope = _make_envelope(prompt_envelope_payload())
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    key = put_envelope(
        envelope=envelope,
        response={"score": "8"},
        policy=retention_type(
            retention_seconds=3600,
            store_controlled_original=True,
            allowed_scopes=("audit",),
        ),
        access_scope="audit",
        now=now,
    )
    database = settings.STORAGE_ROOT / "llm_cache.sqlite"
    with sqlite3.connect(str(database)) as connection:
        connection.execute(
            "UPDATE llm_envelope_cache SET original_envelope_hash = ? WHERE key = ?",
            ("e" * 64, key),
        )
        connection.commit()
    assert get_entry(key, requester_scope="audit", now=now) is None

    changed = prompt_envelope_payload()
    changed["submission"]["title"] = "另一份有效但身份不同的原始 envelope"
    with sqlite3.connect(str(database)) as connection:
        connection.execute(
            "UPDATE llm_envelope_cache SET original_envelope_hash = ?, original_envelope = ? "
            "WHERE key = ?",
            (key, json.dumps(changed, ensure_ascii=False), key),
        )
        connection.commit()
    assert get_entry(key, requester_scope="audit", now=now) is None


@requires_ledger
@pytest.mark.parametrize(
    ("column", "corrupt_value"),
    [
        ("allowed_scopes", "not-json"),
        ("redacted_projection", "not-json"),
        ("response", "not-json"),
        ("created_at", "not-a-timestamp"),
        ("expires_at", "not-a-timestamp"),
    ],
)
def test_cache_corruption_in_any_serialized_ledger_field_is_a_safe_miss(
    cache_ledger,
    column,
    corrupt_value,
):
    retention_type, put_envelope, get_entry, _, _ = cache_ledger
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    key = put_envelope(
        envelope=_make_envelope(prompt_envelope_payload()),
        response={"score": "8"},
        policy=retention_type(
            retention_seconds=3600,
            store_controlled_original=True,
            allowed_scopes=("audit",),
        ),
        access_scope="audit",
        now=now,
    )
    database = settings.STORAGE_ROOT / "llm_cache.sqlite"
    with sqlite3.connect(str(database)) as connection:
        connection.execute(
            "UPDATE llm_envelope_cache SET %s = ? WHERE key = ?" % column,
            (corrupt_value, key),
        )
        connection.commit()

    assert get_entry(key, requester_scope="audit", now=now) is None
