"""M2 不可变 Core DTO 的可执行合同。

本文件只描述 PR-06 的数据边界，不依赖数据库、文件系统、网络，也不测试
M3 的 plan builder、持久化或评分执行。目标 DTO 尚未实现时，能力探针只把
精确缺失的公开类转换为严格 xfail；模块内部导入错误及合同偏差仍会直接失败。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from importlib import import_module
from pathlib import Path
from uuid import UUID

import pytest

from backend.app.services.scoring.core.canonical import canonical_json, canonical_sha256
from backend.app.tests.m2_contract_fixtures import (
    runtime_identity_payload,
    technical_document_payload,
    technical_policy_snapshot_payload,
    technical_submission_payload,
)


CONTRACTS_MODULE = "backend.app.services.scoring.core.contracts"
RESULTS_MODULE = "backend.app.services.scoring.core.results"

CONTRACT_NAMES = (
    "SubmissionSnapshot",
    "DocumentSnapshot",
    "AtomicRuleSnapshot",
    "CompiledRubricSnapshot",
    "RuleExecutionPlan",
    "ScoringRequest",
)
RESULT_NAMES = ("ScoringOutcome",)


class M2CapabilityUnavailable(RuntimeError):
    """仅表示本文件明确要求的 M2 公共 API 尚未出现。"""


def _load_api(module_name: str, names: tuple[str, ...]):
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as exc:
        # 只有目标模块本身不存在才属于预期能力缺失。模块内部依赖缺失必须暴露。
        if exc.name != module_name:
            raise
        error = M2CapabilityUnavailable(f"missing M2 target module: {module_name}")
        return {name: None for name in names}, {name: error for name in names}

    # Read the real module namespace.  ``hasattr``/``getattr`` could invoke a
    # module-level ``__getattr__`` and misclassify an implementation defect as
    # an intentionally missing M2 symbol.
    namespace = vars(module)
    api = {name: namespace.get(name) for name in names}
    errors = {
        name: M2CapabilityUnavailable(
            f"{module_name} does not expose required M2 API: {name}"
        )
        for name in names
        if name not in namespace
    }
    return api, errors


_CONTRACT_API, _CONTRACT_ERRORS = _load_api(CONTRACTS_MODULE, CONTRACT_NAMES)
_RESULT_API, _RESULT_ERRORS = _load_api(RESULTS_MODULE, RESULT_NAMES)


def _requires_contract(name: str):
    return pytest.mark.xfail(
        name in _CONTRACT_ERRORS,
        reason=f"M2 immutable Core DTO {name} is not implemented",
        raises=M2CapabilityUnavailable,
        strict=True,
    )


def _requires_result(name: str):
    return pytest.mark.xfail(
        name in _RESULT_ERRORS,
        reason=f"M2 result DTO {name} is not implemented",
        raises=M2CapabilityUnavailable,
        strict=True,
    )


requires_scoring_outcome = _requires_result("ScoringOutcome")


def _contract(name: str):
    error = _CONTRACT_ERRORS.get(name)
    if error is not None:
        raise M2CapabilityUnavailable(str(error))
    return _CONTRACT_API[name]


def _result(name: str):
    error = _RESULT_ERRORS.get(name)
    if error is not None:
        raise M2CapabilityUnavailable(str(error))
    return _RESULT_API[name]


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64


def _policy_payload() -> dict:
    return technical_policy_snapshot_payload(total_score="20", rounding_digits=1)


def _submission_payload() -> dict:
    return technical_submission_payload()


def _document_payload() -> dict:
    return technical_document_payload()


def _atomic_rule_payload() -> dict:
    return {
        "schema_version": "atomic-rule-snapshot@1",
        "rule_code": "proposal.risk_owner.v1",
        "criterion_code": "RISK_CONTROL",
        "direction": "deduct",
        "effect_type": "score",
        "judge_type": "deterministic",
        "checker_key": "core.required_fields.v1",
        "checker_version": "1.0.0",
        "checker_params": {"required_fields": ["owner"]},
        "evidence_policy": {
            "mode": "scoped_absence",
            "requirement": "required",
            "minimum_coverage": "1",
        },
        "max_points": "2",
        "repeat_policy": "once",
        "cap_points": None,
        "depends_on_rule_codes": [],
        "mutex_group": None,
        "levels": [],
    }


def _criterion_payload() -> dict:
    return {
        "criterion_code": "RISK_CONTROL",
        "name": "Risk control completeness",
        "max_score": "20",
        "weight": None,
        "assessment_mode": "deduct",
    }


def _compiled_rubric_payload(*, source_kind: str = "published_version") -> dict:
    payload = {
        "schema_version": "compiled-rubric-snapshot@1",
        "rubric_source_kind": source_kind,
        "rubric_snapshot_hash": HASH_E,
        "business_profile_key": "technical_proposal",
        "total_score": "20",
        "criteria": [_criterion_payload()],
        "atomic_rules": [_atomic_rule_payload()],
        "global_policy": _policy_payload(),
    }
    if source_kind == "published_version":
        payload.update(
            {
                "rubric_version_id": "rubric-version-tp-001",
                "version_hash": HASH_F,
                "hash_scheme": "rubric-content-v2",
            }
        )
    return payload


def _plan_payload() -> dict:
    return {
        "schema_version": "rule-execution-plan@1",
        "business_profile_key": "technical_proposal",
        "rubric_snapshot_hash": HASH_E,
        "policy_snapshot": _policy_payload(),
        "policy_hash": _policy_payload()["policy_hash"],
        "nodes": [
            {
                "node_kind": "atomic_rule",
                "criterion_code": "RISK_CONTROL",
                "rule_code": "proposal.risk_owner.v1",
            }
        ],
        "dependency_order": ["proposal.risk_owner.v1"],
        "checker_manifest": {
            "core.required_fields.v1": {
                "checker_version": "1.0.0",
                "implementation_hash": HASH_C,
                "params_schema": "required-fields-params@1",
                "supported_document_schemas": ["document-snapshot@1"],
                "supported_profiles": ["technical_proposal"],
                "observation_schema": "deterministic-observation@1",
            }
        },
        "plan_hash": HASH_B,
    }


def _request_payload() -> dict:
    return {
        "schema_version": "scoring-request@1",
        "submission": _submission_payload(),
        "document": _document_payload(),
        "plan": _plan_payload(),
        "runtime_identity": runtime_identity_payload(),
        "rescore_generation": 0,
        "idempotency_key": HASH_A,
    }


def _outcome_payload() -> dict:
    return {
        "schema_version": "scoring-outcome@1",
        "request_identity": {
            "idempotency_key": HASH_A,
            "document_snapshot_hash": _document_payload()["document_snapshot_hash"],
            "rubric_snapshot_hash": HASH_E,
            "plan_hash": HASH_B,
            "policy_hash": _policy_payload()["policy_hash"],
            "profile_key": "technical_proposal",
        },
        "criterion_outcomes": [
            {
                "criterion_code": "RISK_CONTROL",
                "status": "blocked",
                "auto_score": None,
                "final_score": None,
                "max_score": "20",
            }
        ],
        "rule_decisions": [
            {
                "rule_code": "proposal.risk_owner.v1",
                "status": "invalid",
                "evidence_refs": [],
            }
        ],
        "score_contributions": [],
        "review_issues": [
            {
                "code": "REQUIRED_EVIDENCE_INVALID",
                "severity": "block",
                "criterion_code": "RISK_CONTROL",
            }
        ],
        "unrounded_total": None,
        "final_total": None,
        "grade": None,
        "status": "blocked",
        "audit_identity": runtime_identity_payload(),
    }


CONTRACT_CASES = (
    pytest.param(
        "SubmissionSnapshot",
        _submission_payload,
        "metadata",
        marks=_requires_contract("SubmissionSnapshot"),
    ),
    pytest.param(
        "DocumentSnapshot",
        _document_payload,
        "sections",
        marks=_requires_contract("DocumentSnapshot"),
    ),
    pytest.param(
        "AtomicRuleSnapshot",
        _atomic_rule_payload,
        "checker_params",
        marks=_requires_contract("AtomicRuleSnapshot"),
    ),
    pytest.param(
        "CompiledRubricSnapshot",
        _compiled_rubric_payload,
        "criteria",
        marks=_requires_contract("CompiledRubricSnapshot"),
    ),
    pytest.param(
        "RuleExecutionPlan",
        _plan_payload,
        "nodes",
        marks=_requires_contract("RuleExecutionPlan"),
    ),
    pytest.param(
        "ScoringRequest",
        _request_payload,
        "runtime_identity",
        marks=_requires_contract("ScoringRequest"),
    ),
)


def _assert_canonical_round_trip(dto_type, payload: dict) -> None:
    first = dto_type.from_mapping(deepcopy(payload))
    first_mapping = first.to_mapping()
    assert first_mapping == payload
    # This is a serialization checksum of the complete DTO, not a substitute
    # for document/rubric/plan authoritative projections embedded in it.
    assert callable(first.canonical_hash)
    assert first.canonical_hash() == canonical_sha256(first_mapping)

    canonical_text = canonical_json(first_mapping)
    second = dto_type.from_mapping(json.loads(canonical_text))
    assert second.to_mapping() == payload
    assert canonical_json(second.to_mapping()) == canonical_text
    assert second.canonical_hash() == first.canonical_hash()


def _mutate_nested_input(payload: dict) -> None:
    for value in payload.values():
        if isinstance(value, dict):
            value["__mutation_after_construction__"] = True
        elif isinstance(value, list):
            value.append("mutation-after-construction")


def _assert_nested_value_is_immutable(value) -> None:
    if isinstance(value, Mapping):
        with pytest.raises(TypeError):
            value["__mutation__"] = True
        for nested in value.values():
            if isinstance(nested, Mapping) or (
                isinstance(nested, Sequence) and not isinstance(nested, (str, bytes))
            ):
                _assert_nested_value_is_immutable(nested)
        return

    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes)), (
        f"nested DTO value must be an immutable sequence, got {type(value)!r}"
    )
    with pytest.raises((AttributeError, TypeError)):
        value[0] = "mutation"
    for nested in value:
        if isinstance(nested, Mapping) or (
            isinstance(nested, Sequence) and not isinstance(nested, (str, bytes))
        ):
            _assert_nested_value_is_immutable(nested)


@pytest.mark.parametrize(("type_name", "payload_factory", "nested_field"), CONTRACT_CASES)
def test_contract_dto_round_trips_canonical_mapping(type_name, payload_factory, nested_field):
    del nested_field
    dto_type = _contract(type_name)
    _assert_canonical_round_trip(dto_type, payload_factory())


@requires_scoring_outcome
def test_scoring_outcome_round_trips_canonical_mapping():
    _assert_canonical_round_trip(_result("ScoringOutcome"), _outcome_payload())


@pytest.mark.parametrize(("type_name", "payload_factory", "nested_field"), CONTRACT_CASES)
def test_contract_dto_detaches_input_and_is_recursively_immutable(
    type_name,
    payload_factory,
    nested_field,
):
    dto_type = _contract(type_name)
    payload = payload_factory()
    expected = deepcopy(payload)
    instance = dto_type.from_mapping(payload)

    _mutate_nested_input(payload)
    assert instance.to_mapping() == expected

    with pytest.raises((AttributeError, TypeError)):
        instance.schema_version = "mutated-schema"
    _assert_nested_value_is_immutable(getattr(instance, nested_field))
    for field, original in expected.items():
        if field != nested_field and isinstance(original, (dict, list)):
            _assert_nested_value_is_immutable(getattr(instance, field))


@requires_scoring_outcome
def test_scoring_outcome_detaches_input_and_is_recursively_immutable():
    outcome_type = _result("ScoringOutcome")
    payload = _outcome_payload()
    expected = deepcopy(payload)
    outcome = outcome_type.from_mapping(payload)

    _mutate_nested_input(payload)
    assert outcome.to_mapping() == expected

    with pytest.raises((AttributeError, TypeError)):
        outcome.schema_version = "mutated-schema"
    for field, original in expected.items():
        if isinstance(original, (dict, list)):
            _assert_nested_value_is_immutable(getattr(outcome, field))


@pytest.mark.parametrize(("type_name", "payload_factory", "nested_field"), CONTRACT_CASES)
def test_contract_dto_rejects_unknown_top_level_fields(type_name, payload_factory, nested_field):
    del nested_field
    dto_type = _contract(type_name)
    payload = payload_factory()
    payload["unexpected_m2_field"] = True

    with pytest.raises((TypeError, ValueError), match=r"(?i)(unknown|unexpected)"):
        dto_type.from_mapping(payload)


@pytest.mark.parametrize(("type_name", "payload_factory", "nested_field"), CONTRACT_CASES)
def test_contract_dto_rejects_missing_schema_version(type_name, payload_factory, nested_field):
    del nested_field
    dto_type = _contract(type_name)
    payload = payload_factory()
    payload.pop("schema_version")

    with pytest.raises((TypeError, ValueError), match=r"(?i)(missing|required|schema_version)"):
        dto_type.from_mapping(payload)


@pytest.mark.parametrize(("type_name", "payload_factory", "nested_field"), CONTRACT_CASES)
def test_contract_dto_rejects_unsupported_schema_version(
    type_name,
    payload_factory,
    nested_field,
):
    del nested_field
    dto_type = _contract(type_name)
    payload = payload_factory()
    payload["schema_version"] = payload["schema_version"].split("@", 1)[0] + "@999"

    with pytest.raises((TypeError, ValueError), match=r"(?i)(schema|version|unsupported)"):
        dto_type.from_mapping(payload)


REQUIRED_FIELD_CASES = (
    pytest.param(
        "SubmissionSnapshot",
        _submission_payload,
        "profile_key",
        marks=_requires_contract("SubmissionSnapshot"),
    ),
    pytest.param(
        "DocumentSnapshot",
        _document_payload,
        "content_hash",
        marks=_requires_contract("DocumentSnapshot"),
    ),
    pytest.param(
        "AtomicRuleSnapshot",
        _atomic_rule_payload,
        "rule_code",
        marks=_requires_contract("AtomicRuleSnapshot"),
    ),
    pytest.param(
        "CompiledRubricSnapshot",
        _compiled_rubric_payload,
        "rubric_source_kind",
        marks=_requires_contract("CompiledRubricSnapshot"),
    ),
    pytest.param(
        "RuleExecutionPlan",
        _plan_payload,
        "policy_hash",
        marks=_requires_contract("RuleExecutionPlan"),
    ),
    pytest.param(
        "ScoringRequest",
        _request_payload,
        "idempotency_key",
        marks=_requires_contract("ScoringRequest"),
    ),
)


@pytest.mark.parametrize(("type_name", "payload_factory", "field"), REQUIRED_FIELD_CASES)
def test_contract_dto_rejects_missing_nonversion_identity_field(
    type_name,
    payload_factory,
    field,
):
    dto_type = _contract(type_name)
    payload = payload_factory()
    payload.pop(field)

    with pytest.raises((TypeError, ValueError), match=r"(?i)(missing|required)"):
        dto_type.from_mapping(payload)


NESTED_UNKNOWN_FIELD_CASES = (
    pytest.param(
        "SubmissionSnapshot",
        _submission_payload,
        ("artifact_refs", 0),
        marks=_requires_contract("SubmissionSnapshot"),
    ),
    pytest.param(
        "DocumentSnapshot",
        _document_payload,
        ("sections", 0),
        marks=_requires_contract("DocumentSnapshot"),
    ),
    pytest.param(
        "AtomicRuleSnapshot",
        _atomic_rule_payload,
        ("evidence_policy",),
        marks=_requires_contract("AtomicRuleSnapshot"),
    ),
    pytest.param(
        "CompiledRubricSnapshot",
        _compiled_rubric_payload,
        ("criteria", 0),
        marks=_requires_contract("CompiledRubricSnapshot"),
    ),
    pytest.param(
        "RuleExecutionPlan",
        _plan_payload,
        ("checker_manifest", "core.required_fields.v1"),
        marks=_requires_contract("RuleExecutionPlan"),
    ),
    pytest.param(
        "ScoringRequest",
        _request_payload,
        ("runtime_identity",),
        marks=_requires_contract("ScoringRequest"),
    ),
)


@pytest.mark.parametrize(("type_name", "payload_factory", "path"), NESTED_UNKNOWN_FIELD_CASES)
def test_contract_dto_rejects_unknown_nested_structural_fields(
    type_name,
    payload_factory,
    path,
):
    dto_type = _contract(type_name)
    payload = payload_factory()
    cursor = payload
    for part in path:
        cursor = cursor[part]
    cursor["database_session_or_path"] = "/tmp/runtime-only"

    with pytest.raises((TypeError, ValueError), match=r"(?i)(unknown|unexpected)"):
        dto_type.from_mapping(payload)


@requires_scoring_outcome
@pytest.mark.parametrize(
    "mutation",
    ("unknown", "missing", "unsupported", "missing-status", "nested-unknown"),
)
def test_scoring_outcome_uses_a_closed_required_schema(mutation):
    outcome_type = _result("ScoringOutcome")
    payload = _outcome_payload()
    if mutation == "unknown":
        payload["student_id"] = "must-not-enter-core"
        pattern = r"(?i)(unknown|unexpected)"
    else:
        if mutation == "missing":
            payload.pop("schema_version")
            pattern = r"(?i)(missing|required|schema_version)"
        elif mutation == "unsupported":
            payload["schema_version"] = "scoring-outcome@999"
            pattern = r"(?i)(schema|version|unsupported)"
        elif mutation == "missing-status":
            payload.pop("status")
            pattern = r"(?i)(missing|required|status)"
        else:
            payload["review_issues"][0]["database_id"] = "orm-row"
            pattern = r"(?i)(unknown|unexpected)"

    with pytest.raises((TypeError, ValueError), match=pattern):
        outcome_type.from_mapping(payload)


class _ORMLikePaper:
    id = 7
    student_id = "legacy-student"


@_requires_contract("SubmissionSnapshot")
@pytest.mark.parametrize(
    "unsupported",
    (
        0.5,
        Path("/tmp/technical-proposal.docx"),
        UUID("00000000-0000-0000-0000-000000000001"),
        _ORMLikePaper(),
    ),
    ids=("float", "path", "uuid", "orm-like"),
)
def test_dto_boundary_rejects_noncanonical_or_runtime_objects(unsupported):
    submission_type = _contract("SubmissionSnapshot")
    payload = _submission_payload()
    payload["metadata"]["runtime_object"] = unsupported

    with pytest.raises((TypeError, ValueError)):
        submission_type.from_mapping(payload)


@_requires_contract("CompiledRubricSnapshot")
def test_compiled_rubric_accepts_published_and_legacy_identity_variants():
    rubric_type = _contract("CompiledRubricSnapshot")

    published = rubric_type.from_mapping(_compiled_rubric_payload(source_kind="published_version"))
    legacy = rubric_type.from_mapping(_compiled_rubric_payload(source_kind="legacy_unversioned"))

    assert published.to_mapping()["rubric_source_kind"] == "published_version"
    assert published.to_mapping()["rubric_version_id"] == "rubric-version-tp-001"
    assert legacy.to_mapping()["rubric_source_kind"] == "legacy_unversioned"
    assert "rubric_version_id" not in legacy.to_mapping()
    assert "version_hash" not in legacy.to_mapping()
    assert "hash_scheme" not in legacy.to_mapping()


@_requires_contract("CompiledRubricSnapshot")
@pytest.mark.parametrize("field", ("rubric_version_id", "version_hash", "hash_scheme"))
def test_published_rubric_requires_complete_version_identity(field):
    rubric_type = _contract("CompiledRubricSnapshot")
    payload = _compiled_rubric_payload(source_kind="published_version")
    payload.pop(field)

    with pytest.raises((TypeError, ValueError), match=r"(?i)(published|version|missing|required)"):
        rubric_type.from_mapping(payload)


@_requires_contract("CompiledRubricSnapshot")
def test_legacy_rubric_rejects_fabricated_published_version_identity():
    rubric_type = _contract("CompiledRubricSnapshot")
    payload = _compiled_rubric_payload(source_kind="legacy_unversioned")
    payload.update(
        {
            "rubric_version_id": "fabricated-version",
            "version_hash": HASH_F,
            "hash_scheme": "rubric-content-v2",
        }
    )

    with pytest.raises((TypeError, ValueError), match=r"(?i)(legacy|version|unknown|unexpected)"):
        rubric_type.from_mapping(payload)


@_requires_contract("CompiledRubricSnapshot")
def test_compiled_rubric_rejects_unknown_source_kind():
    rubric_type = _contract("CompiledRubricSnapshot")
    payload = _compiled_rubric_payload(source_kind="published_version")
    payload["rubric_source_kind"] = "inferred_from_mutable_orm"

    with pytest.raises((TypeError, ValueError), match=r"(?i)(source|kind|unsupported)"):
        rubric_type.from_mapping(payload)


@_requires_contract("AtomicRuleSnapshot")
def test_atomic_rule_change_alters_its_canonical_snapshot_checksum():
    dto_type = _contract("AtomicRuleSnapshot")
    baseline_payload = _atomic_rule_payload()
    changed_payload = deepcopy(baseline_payload)
    changed_payload["checker_params"]["required_fields"] = ["owner", "mitigation"]

    baseline = dto_type.from_mapping(baseline_payload)
    changed = dto_type.from_mapping(changed_payload)

    assert baseline.canonical_hash() != changed.canonical_hash()


@_requires_contract("ScoringRequest")
@pytest.mark.parametrize("mismatch", ("document", "plan"))
def test_scoring_request_rejects_business_profile_mismatch(mismatch):
    request_type = _contract("ScoringRequest")
    payload = _request_payload()
    if mismatch == "document":
        payload["document"]["profile_key"] = "another_profile"
    else:
        payload["plan"]["business_profile_key"] = "another_profile"

    with pytest.raises((TypeError, ValueError), match=r"(?i)profile"):
        request_type.from_mapping(payload)


@_requires_contract("RuleExecutionPlan")
def test_plan_rejects_checker_manifest_that_does_not_support_profile():
    plan_type = _contract("RuleExecutionPlan")
    payload = _plan_payload()
    payload["checker_manifest"]["core.required_fields.v1"]["supported_profiles"] = [
        "thesis"
    ]

    with pytest.raises((TypeError, ValueError), match=r"(?i)(checker|profile|support)"):
        plan_type.from_mapping(payload)


@_requires_contract("DocumentSnapshot")
@pytest.mark.parametrize(
    "tamper",
    ("document-hash", "content-hash", "evidence-unit-id"),
)
def test_document_snapshot_rejects_tampered_content_addressed_identity(tamper):
    document_type = _contract("DocumentSnapshot")
    payload = _document_payload()
    if tamper == "document-hash":
        payload["document_snapshot_hash"] = "0" * 64
    elif tamper == "content-hash":
        payload["content_hash"] = "0" * 64
    else:
        payload["evidence_units"][0]["evidence_unit_id"] = "0" * 64

    with pytest.raises((TypeError, ValueError), match=r"(?i)(hash|identity|evidence)"):
        document_type.from_mapping(payload)


@_requires_contract("RuleExecutionPlan")
def test_plan_rejects_policy_hash_that_does_not_match_frozen_snapshot():
    plan_type = _contract("RuleExecutionPlan")
    payload = _plan_payload()
    payload["policy_hash"] = "0" * 64

    with pytest.raises((TypeError, ValueError), match=r"(?i)(policy|hash)"):
        plan_type.from_mapping(payload)


@_requires_contract("SubmissionSnapshot")
def test_workflow_profile_cannot_masquerade_as_business_profile_field():
    submission_type = _contract("SubmissionSnapshot")
    payload = _submission_payload()
    payload["workflow_profile"] = "template_driven"

    with pytest.raises((TypeError, ValueError), match=r"(?i)(unknown|unexpected)"):
        submission_type.from_mapping(payload)


def _flatten_keys_and_text(value):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _flatten_keys_and_text(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten_keys_and_text(item)
    elif isinstance(value, str):
        yield value


@requires_scoring_outcome
def test_blocked_outcome_keeps_total_null_and_contains_no_thesis_fields():
    outcome = _result("ScoringOutcome").from_mapping(_outcome_payload())
    mapping = outcome.to_mapping()

    assert mapping["status"] == "blocked"
    assert mapping["unrounded_total"] is None
    assert mapping["final_total"] is None
    assert mapping["criterion_outcomes"][0]["auto_score"] is None
    assert mapping["criterion_outcomes"][0]["final_score"] is None

    flattened = "\n".join(_flatten_keys_and_text(mapping)).casefold()
    for forbidden in (
        "paper_id",
        "student_id",
        "student_name",
        "thesis",
        "论文",
        "学生",
        "摘要",
        "参考文献",
    ):
        assert forbidden.casefold() not in flattened
