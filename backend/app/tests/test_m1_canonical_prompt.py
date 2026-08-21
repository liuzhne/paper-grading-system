"""M1 contracts for canonical JSON and the provider-ready prompt envelope.

These are strict expected-failure tests until the first M1 Core primitives are
implemented.  ``strict=True`` is intentional: once an implementation appears,
an accidental pass under the old xfail condition fails the suite instead of
silently hiding the stale gate.
"""

from __future__ import annotations

import importlib
from copy import deepcopy
from datetime import datetime
from decimal import Decimal

import pytest

from backend.app.services.cache import llm_cache
from backend.app.tests.m1_contract_helpers import EVIDENCE_UNIT_1
from backend.app.tests.m1_contract_helpers import EVIDENCE_UNIT_2
from backend.app.tests.m1_contract_helpers import prompt_envelope_payload


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


_CANONICAL_API, _CANONICAL_ERROR = _load(
    "backend.app.services.scoring.core.canonical",
    "canonical_json",
    "canonical_sha256",
)
_ENVELOPE_API, _ENVELOPE_ERROR = _load(
    "backend.app.services.scoring.core.contracts",
    "PromptEnvelopeV1",
)

requires_canonical = pytest.mark.xfail(
    _CANONICAL_ERROR is not None,
    reason="M1 canonical JSON/hash primitive is not implemented",
    strict=True,
)
requires_envelope = pytest.mark.xfail(
    _ENVELOPE_ERROR is not None or _CANONICAL_ERROR is not None,
    reason="M1 PromptEnvelopeV1 contract is not implemented",
    strict=True,
)


@pytest.fixture()
def canonical_api():
    if _CANONICAL_ERROR is not None:
        raise _CANONICAL_ERROR
    return _CANONICAL_API


@pytest.fixture()
def envelope_api():
    if _ENVELOPE_ERROR is not None:
        raise _ENVELOPE_ERROR
    if _CANONICAL_ERROR is not None:
        raise _CANONICAL_ERROR
    return _ENVELOPE_API[0], *_CANONICAL_API


def _as_text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _model_dump(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "to_mapping"):
        return value.to_mapping()
    if hasattr(value, "dict"):
        return value.dict()
    raise AssertionError("PromptEnvelopeV1 must expose model_dump(), to_mapping(), or dict()")


def _make_envelope(envelope_type, payload):
    if hasattr(envelope_type, "from_mapping"):
        return envelope_type.from_mapping(payload)
    if hasattr(envelope_type, "model_validate"):
        return envelope_type.model_validate(payload)
    return envelope_type(**payload)


def _envelope_hash(envelope, canonical_sha256):
    method = getattr(envelope, "canonical_hash", None)
    if callable(method):
        return method()
    return canonical_sha256(_model_dump(envelope))


@requires_canonical
def test_canonical_json_uses_utf8_sorted_compact_objects(canonical_api):
    canonical_json, _ = canonical_api
    actual = _as_text(canonical_json({"中文": "证据", "a": 1, "nested": {"z": None, "b": True}}))
    assert actual == '{"a":1,"nested":{"b":true,"z":null},"中文":"证据"}'


@requires_canonical
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1.2300"), '{"value":"1.23"}'),
        (Decimal("0.000"), '{"value":"0"}'),
        (Decimal("-0"), '{"value":"0"}'),
        (Decimal("1000.000"), '{"value":"1000"}'),
        (Decimal("0.00000100"), '{"value":"0.000001"}'),
    ],
)
def test_canonical_decimal_is_a_plain_normalized_string(canonical_api, value, expected):
    canonical_json, _ = canonical_api
    assert _as_text(canonical_json({"value": value})) == expected


@requires_canonical
@pytest.mark.parametrize("value", [1.25, float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_all_floats(canonical_api, value):
    canonical_json, _ = canonical_api
    with pytest.raises((TypeError, ValueError)):
        canonical_json({"value": value})


@requires_canonical
def test_canonical_json_rejects_non_string_object_keys(canonical_api):
    canonical_json, _ = canonical_api
    with pytest.raises((TypeError, ValueError)):
        canonical_json({1: "database id must not be stringified implicitly"})


@requires_canonical
@pytest.mark.parametrize(
    "value",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        {"unordered"},
        datetime(2026, 7, 18),
        object(),
    ],
)
def test_canonical_json_rejects_non_finite_or_non_schema_values(canonical_api, value):
    canonical_json, _ = canonical_api
    with pytest.raises((TypeError, ValueError)):
        canonical_json({"value": value})


@requires_canonical
def test_canonical_json_preserves_array_order_and_string_codepoints(canonical_api):
    canonical_json, _ = canonical_api
    nfc = "é"
    nfd = "e\u0301"

    first = _as_text(canonical_json({"items": ["b", "a"], "text": nfc}))
    reordered = _as_text(canonical_json({"items": ["a", "b"], "text": nfc}))
    decomposed = _as_text(canonical_json({"items": ["b", "a"], "text": nfd}))

    assert first != reordered
    assert first != decomposed


@requires_canonical
def test_canonical_hash_is_lowercase_sha256_and_mapping_order_independent(canonical_api):
    _, canonical_sha256 = canonical_api
    first = canonical_sha256({"b": Decimal("2.00"), "a": [1, 2]})
    second = canonical_sha256({"a": [1, 2], "b": Decimal("2")})

    assert first == second
    assert len(first) == 64
    assert first == first.lower()
    assert set(first) <= set("0123456789abcdef")


@requires_envelope
def test_prompt_envelope_is_strict_immutable_and_contains_every_m1_identity(envelope_api):
    envelope_type, _, canonical_sha256 = envelope_api
    payload = prompt_envelope_payload()
    envelope = _make_envelope(envelope_type, payload)
    payload["submission"]["title"] = "构造后的外部突变"
    dumped = _model_dump(envelope)

    assert dumped["schema_version"] == "prompt-envelope@1"
    assert dumped["prompt_version"] == llm_cache.PROMPT_VERSION
    assert dumped["profile"]["key"] == "thesis"
    assert dumped["provider"]["sampling"]["max_tokens"] == 512
    assert dumped["provider"]["thinking"] == {"enabled": False, "type": None}
    assert dumped["provider"]["response_format"] == "none"
    assert dumped["criterion"]["description"] == "评价研究设计、数据来源与分析方法。"
    assert dumped["criterion"]["evidence_hints"] == ["数据来源", "分析方法"]
    assert dumped["criterion"]["deduction_rules"] == ["未说明数据来源时扣分"]
    assert dumped["submission"]["title"] == "基于可解释模型的教学质量评价"
    assert dumped["submission"]["source_artifact_hash"] == "3" * 64
    assert dumped["submission"]["normalized_content_hash"] == "4" * 64
    assert dumped["submission"]["document_snapshot_hash"] == "5" * 64
    assert dumped["rubric_snapshot_hash"] == "1" * 64
    assert dumped["policy_hash"] == "2" * 64
    assert dumped["evidence_units"][0]["location"] == "第三章/3.2"
    assert dumped["evidence_units"][0]["section_title"] == "3.2 数据与方法"
    assert dumped["calibration_anchors"][0]["label"] == "优"
    expected_set_hash = canonical_sha256([EVIDENCE_UNIT_1, EVIDENCE_UNIT_2])
    assert dumped["coverage"]["checked_evidence_unit_ids_hash"] == expected_set_hash
    assert dumped["coverage"]["expected_evidence_unit_ids_hash"] == expected_set_hash
    assert dumped["coverage"]["scope_selector"] == "retrieval://candidate-units"
    assert dumped["coverage"]["authoritative_for_absence"] is False

    with pytest.raises((AttributeError, TypeError, ValueError)):
        envelope.prompt_version = "mutated"

    submission = envelope.submission
    with pytest.raises((AttributeError, TypeError, ValueError)):
        if isinstance(submission, dict):
            submission["title"] = "nested mutation"
        else:
            submission.title = "nested mutation"

    criterion = envelope.criterion
    with pytest.raises((AttributeError, TypeError, ValueError)):
        if isinstance(criterion, dict):
            criterion["authorized_rules"].append({"code": "mutated"})
        else:
            criterion.authorized_rules.append({"code": "mutated"})

    coverage = envelope.coverage
    with pytest.raises((AttributeError, TypeError, ValueError)):
        if isinstance(coverage, dict):
            coverage["checked_evidence_unit_ids"].append("mutated")
        else:
            coverage.checked_evidence_unit_ids.append("mutated")


@requires_envelope
@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("unexpected",), "not allowed"),
        (("provider", "sampling", "temperature"), 0.0),
        (("evidence_units", 0, "chunk_id"), "legacy-db-id"),
    ],
)
def test_prompt_envelope_rejects_unknown_fields_floats_and_legacy_chunk_ids(envelope_api, path, value):
    envelope_type, _, _ = envelope_api
    payload = prompt_envelope_payload()
    cursor = payload
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = value

    with pytest.raises((TypeError, ValueError)):
        _make_envelope(envelope_type, payload)


@requires_envelope
def test_prompt_envelope_rejects_calibration_anchor_hash_mismatch(envelope_api):
    envelope_type, _, _ = envelope_api
    payload = prompt_envelope_payload()
    payload["calibration_anchors"][0]["excerpt"] = "篡改后的范文"

    with pytest.raises((TypeError, ValueError), match="calibration_anchors_hash"):
        _make_envelope(envelope_type, payload)


@requires_envelope
def test_retrieval_candidate_coverage_cannot_authorize_absence(envelope_api):
    envelope_type, _, _ = envelope_api
    payload = prompt_envelope_payload()
    payload["coverage"]["authoritative_for_absence"] = True

    with pytest.raises((TypeError, ValueError), match="absence|retrieval"):
        _make_envelope(envelope_type, payload)


@requires_envelope
def test_extended_criterion_contract_remains_closed(envelope_api):
    envelope_type, _, _ = envelope_api
    payload = prompt_envelope_payload()
    payload["criterion"]["live_database_hint"] = "not allowed"

    with pytest.raises((TypeError, ValueError), match="unknown fields"):
        _make_envelope(envelope_type, payload)


@requires_envelope
def test_prompt_envelope_canonicalizes_expected_and_checked_ids_as_declared_sets(envelope_api):
    envelope_type, _, canonical_sha256 = envelope_api
    first_payload = prompt_envelope_payload()
    second_payload = deepcopy(first_payload)
    second_payload["coverage"]["expected_evidence_unit_ids"].reverse()
    second_payload["coverage"]["checked_evidence_unit_ids"].reverse()

    first = _make_envelope(envelope_type, first_payload)
    second = _make_envelope(envelope_type, second_payload)

    assert _envelope_hash(first, canonical_sha256) == _envelope_hash(second, canonical_sha256)
    first_coverage = _model_dump(first)["coverage"]
    assert set(first_coverage["expected_evidence_unit_ids"]) == {EVIDENCE_UNIT_1, EVIDENCE_UNIT_2}
    assert set(first_coverage["checked_evidence_unit_ids"]) == {EVIDENCE_UNIT_1, EVIDENCE_UNIT_2}


@requires_envelope
@pytest.mark.parametrize(
    "case",
    ["duplicate-expected", "duplicate-checked", "expected-outside-snapshot", "checked-outside-expected"],
)
def test_prompt_envelope_rejects_duplicate_or_out_of_snapshot_coverage_ids(
    envelope_api,
    case,
):
    envelope_type, _, _ = envelope_api
    payload = prompt_envelope_payload()
    coverage = payload["coverage"]
    if case == "duplicate-expected":
        coverage["expected_evidence_unit_ids"] = [
            EVIDENCE_UNIT_2,
            EVIDENCE_UNIT_1,
            EVIDENCE_UNIT_2,
        ]
    elif case == "duplicate-checked":
        coverage["checked_evidence_unit_ids"] = [
            EVIDENCE_UNIT_2,
            EVIDENCE_UNIT_1,
            EVIDENCE_UNIT_2,
        ]
    elif case == "expected-outside-snapshot":
        coverage["expected_evidence_unit_ids"] = [EVIDENCE_UNIT_1, "f" * 64]
        coverage["checked_evidence_unit_ids"] = [EVIDENCE_UNIT_1, "f" * 64]
    else:
        coverage["expected_evidence_unit_ids"] = [EVIDENCE_UNIT_1]
        coverage["checked_evidence_unit_ids"] = [EVIDENCE_UNIT_1, EVIDENCE_UNIT_2]
        coverage["completeness"] = "partial"

    with pytest.raises((TypeError, ValueError)):
        _make_envelope(envelope_type, payload)


@requires_envelope
@pytest.mark.parametrize("field", ["evidence_units", "observations"])
def test_prompt_envelope_keeps_provider_evidence_and_observation_array_order(envelope_api, field):
    envelope_type, _, canonical_sha256 = envelope_api
    first_payload = prompt_envelope_payload()
    second_payload = deepcopy(first_payload)
    second_payload[field].reverse()

    first = _make_envelope(envelope_type, first_payload)
    second = _make_envelope(envelope_type, second_payload)

    assert _envelope_hash(first, canonical_sha256) != _envelope_hash(second, canonical_sha256)
