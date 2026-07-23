"""M1 fail-closed contracts for the temporary LegacyRubricAdapter.

The adapter is deliberately narrow: it gives content-addressed authorization to
old, unversioned rubrics while preventing model-reported points and incomplete
provenance from becoming scoring authority.
"""

from __future__ import annotations

from collections.abc import Mapping
import importlib
from copy import deepcopy
from dataclasses import is_dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import re
from types import MappingProxyType

import pytest
from sqlalchemy import select

from backend.app.db.models import Rubric
from backend.app.db.models import RubricCompilation
from backend.app.db.models import RubricVersion
from backend.app.db.models import Paper
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun
from backend.app.db.session import get_db
from backend.app.main import app
from backend.app.services.scoring import engine as engine_module
from backend.app.tests.conftest import make_sample_docx
from backend.app.tests.conftest import publish_rubric_via_api
from backend.app.tests.conftest import review_rubric_via_api


_LEGACY_TARGET = "backend.app.services.scoring.adapters.legacy_rubric"
try:
    _LEGACY_MODULE = importlib.import_module(_LEGACY_TARGET)
    _LEGACY_MODULE_ERROR = None
except ModuleNotFoundError as exc:
    if exc.name != _LEGACY_TARGET and not _LEGACY_TARGET.startswith("%s." % exc.name):
        raise
    _LEGACY_MODULE = None
    _LEGACY_MODULE_ERROR = exc


def _legacy_capability(*names):
    if _LEGACY_MODULE_ERROR is not None:
        return None, _LEGACY_MODULE_ERROR
    try:
        return tuple(getattr(_LEGACY_MODULE, name) for name in names), None
    except AttributeError as exc:
        return None, exc


_SNAPSHOT_API, _SNAPSHOT_ERROR = _legacy_capability(
    "adapt_legacy_rubric",
    "LegacyRubricError",
)
_EXECUTOR_API, _EXECUTOR_ERROR = _legacy_capability(
    "adapt_legacy_rubric",
    "apply_legacy_deductions",
    "apply_legacy_direct_compat",
    "LegacyRubricError",
)

try:
    _EVIDENCE_MODULE = importlib.import_module("backend.app.services.scoring.core.evidence")
    try:
        _EVIDENCE_VALIDATE = _EVIDENCE_MODULE.validate_evidence
        _EVIDENCE_ERROR = None
    except AttributeError as exc:
        _EVIDENCE_VALIDATE = None
        _EVIDENCE_ERROR = exc
except ModuleNotFoundError as exc:
    target = "backend.app.services.scoring.core.evidence"
    if exc.name != target and not target.startswith("%s." % exc.name):
        raise
    _EVIDENCE_MODULE = None
    _EVIDENCE_VALIDATE = None
    _EVIDENCE_ERROR = exc

requires_legacy_snapshot = pytest.mark.xfail(
    _SNAPSHOT_ERROR is not None,
    reason="M1 PR-01 LegacyRubricAdapter snapshot identity is not implemented",
    strict=True,
)
requires_legacy_executor = pytest.mark.xfail(
    _EXECUTOR_ERROR is not None,
    reason="M1 PR-04 authorized legacy deduction executor is not implemented",
    strict=True,
)
requires_legacy_evidence_composition = pytest.mark.xfail(
    _EXECUTOR_ERROR is not None or _EVIDENCE_ERROR is not None,
    reason="M1 legacy executor/real EvidenceValidationResult composition is not implemented",
    strict=True,
)


@pytest.fixture()
def legacy_snapshot_api():
    if _SNAPSHOT_ERROR is not None:
        raise _SNAPSHOT_ERROR
    return _SNAPSHOT_API


@pytest.fixture()
def legacy_api():
    if _EXECUTOR_ERROR is not None:
        raise _EXECUTOR_ERROR
    return _EXECUTOR_API


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
VALID_EVIDENCE_REF = "evidence-1"


def _db_session_from_client_fixture():
    override = app.dependency_overrides[get_db]
    generator = override()
    return generator, next(generator)


def _require_legacy_executor():
    if _EXECUTOR_ERROR is not None:
        raise _EXECUTOR_ERROR


def _create_published_paper(client, name, *, criterion=None):
    criterion = criterion or {
        "code": "C01",
        "name": "研究方法",
        "max_score": 10,
        "weight": None,
        "scoring_mode": "deductive",
        "deduction_rules_structured": [
            {"match": "未说明数据来源", "points": 2, "reason": "数据来源缺失"}
        ],
    }
    rubric_response = client.post(
        "/api/rubrics",
        json={
            "name": name,
            "version": "v1.0",
            "total_score": 10,
            "criteria": [criterion],
        },
    )
    assert rubric_response.status_code == 200, rubric_response.text
    rubric_id = rubric_response.json()["id"]
    publish_rubric_via_api(client, rubric_id)
    batch_response = client.post(
        "/api/batches",
        json={"name": f"{name}-batch", "rubric_id": rubric_id},
    )
    assert batch_response.status_code == 200, batch_response.text
    upload_response = client.post(
        "/api/papers/upload",
        data={"batch_id": batch_response.json()["id"]},
        files={
            "file": (
                f"{name}.docx",
                make_sample_docx().getvalue(),
                DOCX_MIME,
            )
        },
    )
    assert upload_response.status_code == 200, upload_response.text
    return rubric_id, upload_response.json()["id"]


def _rubric(*, rules=None, rubric_source_kind=None):
    rubric = {
        "id": "database-rubric-id",
        "name": "旧论文评分标准",
        "version": "legacy-label-v1",
        "total_score": Decimal("10.00"),
        "status": "published",
        "created_at": "2026-01-01T00:00:00Z",
        "criteria": [
            {
                "id": "database-criterion-id",
                "code": "C01",
                "name": "研究方法",
                "max_score": Decimal("10.00"),
                "weight": None,
                "description": "核验研究设计、数据来源与样本规模。",
                "evidence_hints": ["研究方法", "数据来源"],
                "deduction_rules": ["未说明数据来源扣 2 分"],
                "criterion_type": "llm_judgment",
                "scoring_mode": "deductive",
                "applies_to": "研究方法",
                "rubric_levels": [
                    {
                        "level": "合格",
                        "points": Decimal("6.00"),
                        "descriptor": "方法基本可执行",
                    }
                ],
                "sub_checks": [
                    {
                        "kind": "llm_judgment",
                        "name": "数据来源",
                        "criteria": "说明数据来源",
                        "max_points": Decimal("4.00"),
                    }
                ],
                "dimension": "内容",
                "deduction_rules_structured": rules
                if rules is not None
                else [
                    {
                        "match": "未说明数据来源",
                        "points": Decimal("2.00"),
                        "reason": "数据来源缺失",
                        "source": "excel",
                        "evidence_mode": "source_quote",
                        "database_id": "source-row-id",
                    },
                    {
                        "match": "未说明样本规模",
                        "points": Decimal("1.00"),
                        "reason": "样本规模缺失",
                        "source": "excel",
                        "evidence_mode": "source_quote",
                        "database_id": "other-source-row-id",
                    },
                ],
            }
        ],
    }
    if rubric_source_kind is not None:
        rubric["rubric_source_kind"] = rubric_source_kind
    return rubric


def _field(value, *names):
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    raise AssertionError("missing contract field %r" % (names,))


def _criterion(snapshot, code="C01"):
    criteria = _field(snapshot, "criteria")
    return next(item for item in criteria if _field(item, "code") == code)


def _authorized_rules(snapshot):
    return _field(_criterion(snapshot), "authorized_rules")


def _deep_freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _canonical_hash(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evidence_policy(requirement="required"):
    return {
        "schema_version": "evidence-policy-v1",
        "default_policy": requirement,
        "requirement": requirement,
        "minimum_valid_items": 1,
        "allowed_types": ["source_quote", "deterministic_observation", "scoped_absence"],
        "review_on_optional_invalid": False,
        "absence": {
            "enabled": True,
            "policy_version": "absence-policy-v1",
            "allowed_targets": ["risk_owner"],
            "complete_scope_selectors": ["document://all-units"],
        },
    }


def _validated_evidence_result(*, sufficient, valid_evidence, issues, requirement="required"):
    evidence_policy = _evidence_policy(requirement)
    content = {
        "schema_version": "evidence-validation-result@1",
        "document_snapshot_hash": "5" * 64,
        "evidence_policy_hash": _canonical_hash(evidence_policy),
        "evidence_sufficient": sufficient,
        "valid_evidence": valid_evidence,
        "issues": issues,
        "injection_flagged": False,
    }
    return _deep_freeze({**content, "validation_hash": _canonical_hash(content)})


def _valid_evidence():
    return _validated_evidence_result(
        sufficient=True,
        valid_evidence=[
            {
                "evidence_ref": VALID_EVIDENCE_REF,
                "type": "source_quote",
                "evidence_unit_id": "a" * 64,
                "quote": "本文采用问卷调查和回归分析。",
                "location": "研究方法",
            },
        ],
        issues=[],
    )


def _effect(rule_ref, **changes):
    return {
        "rule_ref": rule_ref,
        "evidence_refs": [VALID_EVIDENCE_REF],
        **changes,
    }


def _invalid_evidence(requirement="required"):
    return _validated_evidence_result(
        sufficient=False,
        valid_evidence=[],
        issues=[{"code": "QUOTE_NOT_IN_UNIT"}],
        requirement=requirement,
    )


def _complete_absence_evidence():
    unit_ids = ["a" * 64, "b" * 64]
    return _EVIDENCE_VALIDATE(
        evidence={
            "items": [
                {
                    "evidence_ref": VALID_EVIDENCE_REF,
                    "type": "scoped_absence",
                    "document_snapshot_hash": "5" * 64,
                    "scope_selector": "document://all-units",
                    "target": "risk_owner",
                    "expected_evidence_unit_ids": unit_ids,
                    "expected_evidence_unit_ids_hash": _canonical_hash(
                        sorted(unit_ids)
                    ),
                    "checked_evidence_unit_ids": list(reversed(unit_ids)),
                    "checked_evidence_unit_ids_hash": _canonical_hash(
                        sorted(unit_ids)
                    ),
                    "coverage_completeness": "partial",
                    "evidence_policy_version": "absence-policy-v1",
                }
            ]
        },
        evidence_units={
            unit_id: {
                "evidence_unit_id": unit_id,
                "text": "冻结证据单元 %s" % index,
            }
            for index, unit_id in enumerate(unit_ids)
        },
        policy=_evidence_policy("required"),
        context={
            "decision_mode": "deductive",
            "document_snapshot_hash": "5" * 64,
            "declared_scope": {
                "selector": "document://all-units",
                "expected_evidence_unit_ids": unit_ids,
                "expected_evidence_unit_ids_hash": _canonical_hash(
                    sorted(unit_ids)
                ),
            },
        },
    )


def _frozen_policy(*, evidence_requirement="required", allow_direct=False, force_review=False):
    evidence_policy = _evidence_policy(evidence_requirement)
    content = {
        "schema_version": "scoring-policy@1",
        "hash_scheme": "core-canonical-json-v1",
        "policy_key": "corrected_thesis_policy",
        "evidence": evidence_policy,
        "evidence_policy_hash": _canonical_hash(evidence_policy),
        "review": {"on_invalid_evidence": "required_only"},
        "legacy": {
            "allow_legacy_direct_compat": allow_direct,
            "force_review": force_review,
        },
    }
    return _deep_freeze({**content, "policy_hash": _canonical_hash(content)})


def _adapt(adapt, rubric=None, compilations=()):
    return adapt(rubric or _rubric(), compilations=list(compilations))


def _replace_contract(value, **changes):
    if isinstance(value, dict):
        return {**value, **changes}
    if hasattr(value, "model_copy"):
        return value.model_copy(update=changes)
    if is_dataclass(value):
        return replace(value, **changes)
    raise AssertionError("legacy snapshot must support an immutable replacement operation")


def _frozen_sequence_like(value, items):
    return type(value)(items)


def _rehash_snapshot(snapshot):
    assert is_dataclass(snapshot), "tamper tests require the immutable dataclass snapshot"
    projection = snapshot.to_mapping()
    projection.pop("rubric_snapshot_hash")
    return replace(snapshot, rubric_snapshot_hash=_canonical_hash(projection))


@requires_legacy_snapshot
def test_legacy_adapter_builds_a_content_addressed_unversioned_snapshot(legacy_snapshot_api):
    adapt, _ = legacy_snapshot_api
    snapshot = _adapt(adapt)
    rule_codes = [_field(rule, "code") for rule in _authorized_rules(snapshot)]

    assert _field(snapshot, "rubric_source_kind") == "legacy_unversioned"
    assert _field(snapshot, "rubric_version_id") is None
    assert _field(snapshot, "version_hash") is None
    assert _field(snapshot, "schema_version")
    assert _field(snapshot, "hash_scheme") == "core-canonical-json-v1"
    assert len(_field(snapshot, "rubric_snapshot_hash")) == 64
    assert all(code.startswith("LEGACY:C01:") for code in rule_codes)
    assert all(re.fullmatch(r"LEGACY:C01:[0-9a-f]{64}", code) for code in rule_codes)
    assert len(set(rule_codes)) == 2
    assert [_field(rule, "points") for rule in _authorized_rules(snapshot)] == [Decimal("2"), Decimal("1")]


@requires_legacy_snapshot
def test_legacy_snapshot_deeply_freezes_every_m1_criterion_behavior_field(legacy_snapshot_api):
    adapt, _ = legacy_snapshot_api
    criterion = _criterion(_adapt(adapt))

    assert _field(criterion, "description") == "核验研究设计、数据来源与样本规模。"
    assert _field(criterion, "criterion_type") == "llm_judgment"
    assert _field(criterion, "applies_to") == "研究方法"
    assert _field(criterion, "dimension") == "内容"

    with pytest.raises(TypeError):
        _field(criterion, "evidence_hints").append("篡改")
    with pytest.raises(TypeError):
        _field(criterion, "deduction_rules")[0] = "篡改"
    with pytest.raises(TypeError):
        _field(criterion, "deduction_rules_structured")[0]["points"] = Decimal("9")
    with pytest.raises(TypeError):
        _field(criterion, "rubric_levels")[0]["points"] = Decimal("9")
    with pytest.raises(TypeError):
        _field(criterion, "sub_checks")[0]["max_points"] = Decimal("9")


@requires_legacy_snapshot
def test_legacy_snapshot_and_rule_codes_ignore_database_ids_and_mapping_order(legacy_snapshot_api):
    adapt, _ = legacy_snapshot_api
    first_rubric = _rubric()
    second_rubric = deepcopy(first_rubric)
    second_rubric["id"] = "different-rubric-row"
    second_rubric["version"] = "mutable-label-that-is-not-authority"
    second_rubric["created_at"] = "2030-12-31T23:59:59Z"
    second_rubric["criteria"][0]["id"] = "different-criterion-row"
    for index, rule in enumerate(second_rubric["criteria"][0]["deduction_rules_structured"]):
        rule["database_id"] = "changed-%s" % index
        second_rubric["criteria"][0]["deduction_rules_structured"][index] = dict(reversed(list(rule.items())))

    first = _adapt(adapt, first_rubric)
    second = _adapt(adapt, second_rubric)

    assert _field(first, "rubric_snapshot_hash") == _field(second, "rubric_snapshot_hash")
    assert [_field(rule, "code") for rule in _authorized_rules(first)] == [
        _field(rule, "code") for rule in _authorized_rules(second)
    ]


@requires_legacy_snapshot
def test_legacy_adapter_requires_an_explicit_provenance_query_result(legacy_snapshot_api):
    adapt, error_type = legacy_snapshot_api
    with pytest.raises((TypeError, error_type)):
        adapt(_rubric())


@requires_legacy_snapshot
@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("match", "数据来源不可核验"),
        ("points", Decimal("3")),
        ("reason", "数据来源说明不完整"),
        ("evidence_mode", "review_only"),
    ],
)
def test_legacy_rule_code_is_content_addressed_per_rule(legacy_snapshot_api, field, changed_value):
    adapt, _ = legacy_snapshot_api
    first = _adapt(adapt)
    changed_rubric = _rubric()
    changed_rubric["criteria"][0]["deduction_rules_structured"][0][field] = changed_value
    changed = _adapt(adapt, changed_rubric)

    first_codes = {_field(rule, "code") for rule in _authorized_rules(first)}
    changed_codes = {_field(rule, "code") for rule in _authorized_rules(changed)}
    assert len(first_codes & changed_codes) == 1
    assert first_codes != changed_codes


@requires_legacy_snapshot
def test_missing_historical_evidence_mode_defaults_to_review_only_without_text_inference(
    legacy_snapshot_api,
):
    adapt, _ = legacy_snapshot_api
    historical = _adapt(
        adapt,
        _rubric(
            rules=[
                {
                    "match": "未说明数据来源",
                    "points": Decimal("2"),
                    "reason": "数据来源缺失",
                }
            ]
        ),
    )
    explicit = _adapt(
        adapt,
        _rubric(
            rules=[
                {
                    "match": "未说明数据来源",
                    "points": Decimal("2"),
                    "reason": "数据来源缺失",
                    "evidence_mode": "source_quote",
                }
            ]
        ),
    )

    historical_rule = _authorized_rules(historical)[0]
    explicit_rule = _authorized_rules(explicit)[0]
    assert _field(historical_rule, "evidence_mode") == "review_only"
    assert _field(historical_rule, "absence_target") is None
    assert _field(historical_rule, "code") != _field(explicit_rule, "code")
    assert _field(historical, "rubric_snapshot_hash") != _field(
        explicit, "rubric_snapshot_hash"
    )


@requires_legacy_snapshot
def test_invalid_explicit_legacy_evidence_mode_is_rejected(legacy_snapshot_api):
    adapt, error_type = legacy_snapshot_api
    with pytest.raises(error_type, match="evidence_mode"):
        _adapt(
            adapt,
            _rubric(
                rules=[
                    {
                        "match": "未说明数据来源",
                        "points": Decimal("2"),
                        "reason": "数据来源缺失",
                        "evidence_mode": "infer_from_text",
                    }
                ]
            ),
        )


@requires_legacy_snapshot
@pytest.mark.parametrize("points", [Decimal("0"), Decimal("-1"), Decimal("11")])
def test_invalid_structured_rule_points_never_create_legacy_authority(legacy_snapshot_api, points):
    adapt, error_type = legacy_snapshot_api
    rubric = _rubric(
        rules=[{"match": "方法不完整", "points": points, "reason": "方法问题"}]
    )

    try:
        snapshot = _adapt(adapt, rubric)
    except error_type:
        return

    assert _authorized_rules(snapshot) == []


@requires_legacy_snapshot
@pytest.mark.parametrize(
    ("path", "changed_value"),
    [
        (("criteria", 0, "code"), "C02"),
        (("criteria", 0, "max_score"), Decimal("12")),
        (("criteria", 0, "description"), "替换后的评分描述"),
        (("criteria", 0, "evidence_hints", 0), "数据不可核验"),
        (("criteria", 0, "deduction_rules", 0), "数据来源不明扣 3 分"),
        (("criteria", 0, "criterion_type"), "deterministic"),
        (("criteria", 0, "scoring_mode"), "llm_direct"),
        (("criteria", 0, "applies_to"), "全文"),
        (("criteria", 0, "rubric_levels", 0, "descriptor"), "方法设计不完整"),
        (("criteria", 0, "sub_checks", 0, "criteria"), "检查数据授权"),
        (("criteria", 0, "dimension"), "规范性"),
        (("criteria", 0, "deduction_rules_structured", 0, "match"), "数据来源不可靠"),
        (("criteria", 0, "deduction_rules_structured", 0, "points"), Decimal("3")),
        (("criteria", 0, "deduction_rules_structured", 0, "reason"), "来源说明不完整"),
        (("criteria", 0, "deduction_rules_structured", 0, "evidence_mode"), "review_only"),
    ],
)
def test_scoring_content_changes_legacy_snapshot_hash(legacy_snapshot_api, path, changed_value):
    adapt, _ = legacy_snapshot_api
    changed = _rubric()
    cursor = changed
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = changed_value
    if path == ("criteria", 0, "max_score"):
        changed["total_score"] = changed_value

    assert _field(_adapt(adapt), "rubric_snapshot_hash") != _field(
        _adapt(adapt, changed), "rubric_snapshot_hash"
    )


@requires_legacy_executor
def test_legacy_executor_rejects_an_explicitly_replaced_old_snapshot_hash(legacy_api):
    adapt, apply_deductions, _, error_type = legacy_api
    snapshot = _adapt(adapt)
    tampered = replace(snapshot, rubric_snapshot_hash="0" * 64)

    with pytest.raises(error_type, match="snapshot|hash|快照|哈希"):
        apply_deductions(
            snapshot=tampered,
            criterion_code="C01",
            model_effects=[],
            evidence_result=_valid_evidence(),
            policy=_frozen_policy(evidence_requirement="required"),
        )


@requires_legacy_executor
def test_legacy_executor_rejects_dataclass_replaced_criterion_behavior_with_stale_hash(legacy_api):
    adapt, _, direct, error_type = legacy_api
    snapshot = _adapt(adapt)
    criterion = _criterion(snapshot)
    changed_criterion = replace(criterion, description="篡改后的评分行为")
    tampered = replace(
        snapshot,
        criteria=_frozen_sequence_like(snapshot.criteria, [changed_criterion]),
    )

    with pytest.raises(error_type, match="snapshot|hash|快照|哈希"):
        direct(
            snapshot=tampered,
            criterion_code="C01",
            model_score=Decimal("7"),
            evidence_result=_valid_evidence(),
            policy=_frozen_policy(allow_direct=True),
        )


@requires_legacy_executor
def test_legacy_executor_rejects_rule_points_tamper_even_with_rehashed_outer_snapshot(legacy_api):
    adapt, apply_deductions, _, error_type = legacy_api
    snapshot = _adapt(adapt)
    criterion = _criterion(snapshot)
    rules = _field(criterion, "authorized_rules")
    changed_rule = replace(rules[0], points=Decimal("3"))
    changed_criterion = replace(
        criterion,
        authorized_rules=_frozen_sequence_like(
            rules,
            [changed_rule, *rules[1:]],
        ),
    )
    tampered = _rehash_snapshot(
        replace(
            snapshot,
            criteria=_frozen_sequence_like(snapshot.criteria, [changed_criterion]),
        )
    )

    with pytest.raises(error_type, match="rule|semantic|structured|hash|规则|哈希"):
        apply_deductions(
            snapshot=tampered,
            criterion_code="C01",
            model_effects=[{"rule_ref": changed_rule.code}],
            evidence_result=_valid_evidence(),
            policy=_frozen_policy(evidence_requirement="required"),
        )


@requires_legacy_executor
def test_legacy_executor_rejects_rule_scope_tamper_even_with_rehashed_outer_snapshot(legacy_api):
    adapt, apply_deductions, _, error_type = legacy_api
    snapshot = _adapt(adapt)
    criterion = _criterion(snapshot)
    rules = _field(criterion, "authorized_rules")
    changed_rule = replace(rules[0], criterion_code="C02")
    changed_criterion = replace(
        criterion,
        authorized_rules=_frozen_sequence_like(
            rules,
            [changed_rule, *rules[1:]],
        ),
    )
    tampered = _rehash_snapshot(
        replace(
            snapshot,
            criteria=_frozen_sequence_like(snapshot.criteria, [changed_criterion]),
        )
    )

    with pytest.raises(error_type, match="scope|criterion|rule|范围|评分项|规则"):
        apply_deductions(
            snapshot=tampered,
            criterion_code="C01",
            model_effects=[],
            evidence_result=_valid_evidence(),
            policy=_frozen_policy(evidence_requirement="required"),
        )


@requires_legacy_executor
@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("schema_version", "legacy-rubric-snapshot@999"),
        ("hash_scheme", "unsupported-hash-scheme"),
        ("rubric_source_kind", "published_version"),
        ("rubric_version_id", "version-1"),
        ("version_hash", "f" * 64),
    ],
)
def test_legacy_executor_rejects_invalid_snapshot_identity_even_when_rehashed(
    legacy_api, field, changed_value
):
    adapt, _, direct, error_type = legacy_api
    tampered = _rehash_snapshot(replace(_adapt(adapt), **{field: changed_value}))

    with pytest.raises(error_type, match="legacy|schema|hash|version|source|版本|哈希"):
        direct(
            snapshot=tampered,
            criterion_code="C01",
            model_score=Decimal("7"),
            evidence_result=_valid_evidence(),
            policy=_frozen_policy(allow_direct=True),
        )


@requires_legacy_executor
def test_legacy_executor_rejects_duplicate_criterion_identity_after_rehash(legacy_api):
    adapt, apply_deductions, _, error_type = legacy_api
    snapshot = _adapt(adapt)
    criterion = _criterion(snapshot)
    tampered = _rehash_snapshot(
        replace(
            snapshot,
            criteria=_frozen_sequence_like(snapshot.criteria, [criterion, criterion]),
        )
    )

    with pytest.raises(error_type, match="duplicate|criterion|重复|评分项"):
        apply_deductions(
            snapshot=tampered,
            criterion_code="C01",
            model_effects=[],
            evidence_result=_valid_evidence(),
            policy=_frozen_policy(evidence_requirement="required"),
        )


@requires_legacy_executor
def test_legacy_executor_rejects_duplicate_rule_identity_after_rehash(legacy_api):
    adapt, apply_deductions, _, error_type = legacy_api
    snapshot = _adapt(adapt)
    criterion = _criterion(snapshot)
    structured = _field(criterion, "deduction_rules_structured")
    rules = _field(criterion, "authorized_rules")
    changed_criterion = replace(
        criterion,
        deduction_rules_structured=_frozen_sequence_like(
            structured,
            [*structured, structured[0]],
        ),
        authorized_rules=_frozen_sequence_like(rules, [*rules, rules[0]]),
    )
    tampered = _rehash_snapshot(
        replace(
            snapshot,
            criteria=_frozen_sequence_like(snapshot.criteria, [changed_criterion]),
        )
    )

    with pytest.raises(error_type, match="duplicate|rule|重复|规则"):
        apply_deductions(
            snapshot=tampered,
            criterion_code="C01",
            model_effects=[],
            evidence_result=_valid_evidence(),
            policy=_frozen_policy(evidence_requirement="required"),
        )


@requires_legacy_snapshot
@pytest.mark.parametrize("status", ["created", "parsed", "validated", "failed", "published"])
def test_any_existing_provenance_cannot_fall_back_to_legacy_unversioned(legacy_snapshot_api, status):
    adapt, error_type = legacy_snapshot_api
    compilation = {"id": "compilation-1", "status": status, "rubric_id": "database-rubric-id"}

    with pytest.raises(error_type, match="provenance|legacy|发布|来源"):
        _adapt(adapt, compilations=[compilation])


@requires_legacy_executor
def test_locked_published_version_is_not_displaced_by_unpublished_compilation(request):
    _require_legacy_executor()
    client = request.getfixturevalue("client")
    rubric_id, paper_id = _create_published_paper(client, "M1-unpublished-provenance")

    db_generator, db = _db_session_from_client_fixture()
    try:
        rubric = db.scalar(select(Rubric).where(Rubric.id == rubric_id))
        db.execute(
            RubricCompilation.__table__.insert().values(
                id="m1-unpublished-compilation",
                rubric_id=rubric_id,
                status="created",
                parser_version="m1-contract-parser-v1",
                compiler_version="m1-contract-compiler-v1",
                model_provider=None,
                model_name=None,
                sampling_params={},
                prompt_version="m1-contract-prompt-v1",
                raw_parse_output={},
                raw_model_output={},
                validation_result={},
                blockers=[],
                warnings=[],
                human_changes=[],
                created_by=rubric.created_by,
            )
        )
        db.commit()
    finally:
        db.close()
        db_generator.close()

    score_response = client.post(f"/api/papers/{paper_id}/score")
    assert score_response.status_code == 200, score_response.text
    with client.session_factory() as db:
        paper = db.get(Paper, paper_id)
        run = db.get(ScoringRun, score_response.json()["id"])
        assert paper.batch.rubric_version_id is not None
        assert run.rubric_version_id == paper.batch.rubric_version_id


@requires_legacy_executor
def test_formal_version_scoring_never_reaches_the_old_arbitrary_points_function(request, monkeypatch):
    _require_legacy_executor()
    client = request.getfixturevalue("client")
    _rubric_id, paper_id = _create_published_paper(
        client,
        "M1-formal-no-arbitrary-points",
    )

    if hasattr(engine_module, "_apply_deductive"):
        def old_arbitrary_points_forbidden(*_args, **_kwargs):
            raise AssertionError("formal RubricVersion must not call the old arbitrary-points reducer")

        monkeypatch.setattr(engine_module, "_apply_deductive", old_arbitrary_points_forbidden)

    response = client.post(f"/api/papers/{paper_id}/score")
    assert response.status_code == 200, response.text
    assert client.get("/api/scoring-runs", params={"paper_id": paper_id}).json()


@requires_legacy_executor
def test_published_m4_deductive_rule_persists_auditable_block_instead_of_full_score(
    request,
):
    _require_legacy_executor()
    client = request.getfixturevalue("client")
    _rubric_id, paper_id = _create_published_paper(
        client,
        "M1-historical-rule-review-only",
    )

    score_response = client.post(f"/api/papers/{paper_id}/score")
    assert score_response.status_code == 200, score_response.text
    run = score_response.json()
    assert run["final_total_score"] is None
    assert run["need_manual_review"] is True

    items_response = client.get(f"/api/scoring-runs/{run['id']}/items")
    assert items_response.status_code == 200, items_response.text
    items = items_response.json()
    assert len(items) == 1
    assert items[0]["ai_score"] is None
    assert items[0]["auto_score_status"] in {"invalid", "blocked"}

    db_generator, db = _db_session_from_client_fixture()
    try:
        stored = db.scalar(
            select(ScoreItem).where(ScoreItem.scoring_run_id == run["id"])
        )
        assert stored is not None
        assert stored.rule_results_schema_version == "rule-results@2"
        assert stored.rule_results
        assert all(
            result.get("rule_code")
            for result in stored.rule_results
        )
    finally:
        db.close()
        db_generator.close()


@requires_legacy_executor
@pytest.mark.parametrize(
    ("case_name", "criterion", "message"),
    [
        (
            "hybrid",
            {
                "code": "C01",
                "name": "混合评分项",
                "max_score": 10,
                "weight": None,
                "criterion_type": "hybrid",
                "scoring_mode": "llm_direct",
                "sub_checks": [
                    {
                        "kind": "llm_judgment",
                        "name": "语义子检查",
                        "criteria": "核验研究方法",
                        "max_points": 10,
                    }
                ],
            },
            "hybrid",
        ),
        (
            "banded-without-levels",
            {
                "code": "C01",
                "name": "分档评分项",
                "max_score": 10,
                "weight": None,
                "criterion_type": "llm_judgment",
                "scoring_mode": "banded",
                "rubric_levels": [],
            },
            "rubric level",
        ),
    ],
)
def test_publish_gate_rejects_unsupported_criteria_before_batch_creation(
    request,
    case_name,
    criterion,
    message,
):
    client = request.getfixturevalue("client")
    create_response = client.post(
        "/api/rubrics",
        json={
            "name": f"M1-authoritative-gate-{case_name}",
            "version": "v1.0",
            "total_score": 10,
            "criteria": [criterion],
        },
    )
    assert create_response.status_code == 200, create_response.text
    rubric_id = create_response.json()["id"]
    identity = review_rubric_via_api(client, rubric_id)

    response = client.post(
        f"/api/rubrics/{rubric_id}/publish",
        json={
            "compilation_id": identity["compilation_id"],
            "reason": "不支持的旧评分项必须在发布前被拦截",
        },
    )

    assert response.status_code == 400, response.text
    assert "validated" in response.text.lower() or "不可发布" in response.text
    with client.session_factory() as db:
        compilation = db.get(RubricCompilation, identity["compilation_id"])
        assert compilation.status == "blocked"
        assert any(
            blocker.get("code") == "MISSING_EXECUTABLE_SCORING_MODE"
            for blocker in compilation.blockers
        )
    batch = client.post(
        "/api/batches",
        json={"name": f"{case_name}-batch", "rubric_id": rubric_id},
    )
    assert batch.status_code == 400, batch.text


@requires_legacy_executor
def test_authorized_deduction_uses_frozen_points_not_model_reported_points(legacy_api):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(adapt)
    rule_code = _field(_authorized_rules(snapshot)[0], "code")

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[_effect(rule_code, points=Decimal("9"))],
        evidence_result=_valid_evidence(),
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _field(result, "auto_score") == Decimal("8")
    assert _field(result, "auto_score_status") == "calculated"
    assert _field(_field(result, "applied_effects")[0], "points") == Decimal("2")


@requires_legacy_evidence_composition
def test_legacy_executor_consumes_the_real_validator_result_without_reconstruction(legacy_api):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(adapt)
    rule_code = _field(_authorized_rules(snapshot)[0], "code")
    unit_id = "a" * 64
    expected_ids = [unit_id]
    evidence_result = _EVIDENCE_VALIDATE(
        evidence={
            "items": [
                {
                    "evidence_ref": VALID_EVIDENCE_REF,
                    "type": "source_quote",
                    "evidence_unit_id": unit_id,
                    "quote": "本文采用问卷调查和回归分析。",
                    "location": {"section_title": "研究方法", "paragraph": 1},
                }
            ]
        },
        evidence_units={
            unit_id: {
                "evidence_unit_id": unit_id,
                "text": "本文采用问卷调查和回归分析。",
                "section_id": "section-method",
                "section_path": ["正文", "研究方法"],
                "section_ordinal": 1,
                "unit_ordinal": 0,
            }
        },
        policy=_evidence_policy("required"),
        context={
            "decision_mode": "deductive",
            "document_snapshot_hash": "5" * 64,
            "declared_scope": {
                "selector": "document://all-units",
                "expected_evidence_unit_ids": expected_ids,
                "expected_evidence_unit_ids_hash": _canonical_hash(sorted(expected_ids)),
            },
        },
    )

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[_effect(rule_code)],
        evidence_result=evidence_result,
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _field(evidence_result, "validation_hash")
    assert _field(
        _field(evidence_result, "valid_evidence")[0], "evidence_ref"
    ) == VALID_EVIDENCE_REF
    assert _field(result, "auto_score") == Decimal("8")
    assert _field(result, "auto_score_status") == "calculated"


@requires_legacy_evidence_composition
def test_core_rejects_every_occurrence_of_a_duplicate_response_evidence_ref():
    unit_id = "a" * 64
    result = _EVIDENCE_VALIDATE(
        evidence={
            "items": [
                {
                    "evidence_ref": VALID_EVIDENCE_REF,
                    "type": "source_quote",
                    "evidence_unit_id": unit_id,
                    "quote": "本文采用问卷调查。",
                    "location": "研究方法",
                },
                {
                    "evidence_ref": VALID_EVIDENCE_REF,
                    "type": "source_quote",
                    "evidence_unit_id": unit_id,
                    "quote": "本文采用问卷调查。",
                    "location": "研究方法",
                },
            ]
        },
        evidence_units={
            unit_id: {
                "evidence_unit_id": unit_id,
                "text": "本文采用问卷调查。",
            }
        },
        policy=_evidence_policy("required"),
        context={"document_snapshot_hash": "5" * 64},
    )

    assert _field(result, "valid_evidence") == []
    assert _field(result, "evidence_sufficient") is False
    assert [
        _field(issue, "code") for issue in _field(result, "issues")
    ] == ["EVIDENCE_REF_DUPLICATE", "EVIDENCE_REF_DUPLICATE"]


@requires_legacy_executor
def test_rule_ref_without_evidence_refs_cannot_authorize_a_deduction(legacy_api):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(adapt)
    rule_code = _field(_authorized_rules(snapshot)[1], "code")

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[{"rule_ref": rule_code}],
        evidence_result=_valid_evidence(),
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _field(result, "auto_score") == Decimal("10")
    assert _field(result, "applied_effects") == []
    assert _field(result, "need_manual_review", "requires_review") is True
    assert any(
        _field(issue, "code") == "EVIDENCE_REFS_REQUIRED"
        for issue in _field(result, "validation_issues")
    )


@requires_legacy_executor
def test_unknown_evidence_ref_discards_only_that_effect_and_requires_review(legacy_api):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(adapt)
    rule_code = _field(_authorized_rules(snapshot)[0], "code")

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[
            {
                "rule_ref": rule_code,
                "evidence_refs": ["not-a-validated-ref"],
            }
        ],
        evidence_result=_valid_evidence(),
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _field(result, "auto_score") == Decimal("10")
    assert _field(result, "applied_effects") == []
    assert _field(result, "final_total_blocked") is False
    assert _field(result, "need_manual_review", "requires_review") is True
    assert any(
        _field(issue, "code") == "EVIDENCE_REF_NOT_VALIDATED"
        for issue in _field(result, "validation_issues")
    )


@requires_legacy_executor
def test_bad_bound_effect_does_not_remove_another_valid_effect(legacy_api):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(adapt)
    first_rule, second_rule = _authorized_rules(snapshot)

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[
            _effect(_field(first_rule, "code")),
            {
                "rule_ref": _field(second_rule, "code"),
                "evidence_refs": ["not-a-validated-ref"],
            },
        ],
        evidence_result=_valid_evidence(),
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _field(result, "auto_score") == Decimal("8")
    assert [
        _field(effect, "rule_ref")
        for effect in _field(result, "applied_effects")
    ] == [_field(first_rule, "code")]
    assert _field(result, "final_total_blocked") is False
    assert _field(result, "need_manual_review", "requires_review") is True
    applied = _field(result, "applied_effects")[0]
    assert _field(applied, "evidence_refs") == [VALID_EVIDENCE_REF]
    assert _field(_field(applied, "evidence")[0], "evidence_ref") == VALID_EVIDENCE_REF


@requires_legacy_executor
def test_mixed_authorized_and_unknown_effects_keep_only_the_authorized_effect(legacy_api):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(adapt)
    rule_code = _field(_authorized_rules(snapshot)[0], "code")

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[
            _effect(rule_code),
            {"rule_ref": "LEGACY:C01:" + "f" * 64},
        ],
        evidence_result=_valid_evidence(),
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _field(result, "auto_score") == Decimal("8")
    assert [_field(effect, "rule_ref") for effect in _field(result, "applied_effects")] == [rule_code]
    assert _field(result, "auto_score_status") == "calculated"
    assert _field(result, "need_manual_review", "requires_review") is True
    assert _field(result, "validation_issues")


@requires_legacy_executor
def test_duplicate_legacy_rule_hits_cannot_deduct_the_same_frozen_rule_twice(legacy_api):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(adapt)
    rule_code = _field(_authorized_rules(snapshot)[0], "code")

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[_effect(rule_code), _effect(rule_code)],
        evidence_result=_valid_evidence(),
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _field(result, "auto_score") == Decimal("8")
    assert [_field(effect, "rule_ref") for effect in _field(result, "applied_effects")] == [rule_code]


@requires_legacy_executor
@pytest.mark.parametrize(
    "effect",
    [
        {"rule_ref": "LEGACY:C01:" + "f" * 64},
        {"rule_ref": ""},
        {"rule_ref": None},
        {"rule_ref": "KNOWN", "points": Decimal("-1")},
        {"rule_ref": "KNOWN", "points": Decimal("11")},
    ],
)
def test_unauthorized_or_malformed_effect_has_no_score_effect_and_requires_review(legacy_api, effect):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(adapt)
    if effect.get("rule_ref") == "KNOWN":
        effect = {
            **effect,
            "rule_ref": _field(_authorized_rules(snapshot)[0], "code"),
            "evidence_refs": [VALID_EVIDENCE_REF],
        }

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[effect],
        evidence_result=_valid_evidence(),
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _field(result, "auto_score") == Decimal("10")
    assert _field(result, "auto_score_status") == "calculated"
    assert _field(result, "need_manual_review", "requires_review") is True
    assert _field(result, "applied_effects") == []
    assert _field(result, "validation_issues")


@requires_legacy_executor
def test_rule_code_is_scoped_to_its_criterion(legacy_api):
    adapt, apply_deductions, _, _ = legacy_api
    rubric = _rubric()
    second = deepcopy(rubric["criteria"][0])
    second.update({"id": "criterion-2", "code": "C02", "name": "实验结果"})
    rubric["criteria"].append(second)
    rubric["total_score"] = Decimal("20")
    snapshot = _adapt(adapt, rubric)
    foreign_rule = _field(_field(_criterion(snapshot, "C02"), "authorized_rules")[0], "code")

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[{"rule_ref": foreign_rule}],
        evidence_result=_valid_evidence(),
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _field(result, "applied_effects") == []
    assert _field(result, "auto_score") == Decimal("10")
    assert _field(result, "auto_score_status") == "calculated"
    assert _field(result, "need_manual_review", "requires_review") is True


@requires_legacy_executor
def test_required_invalid_evidence_nulls_criterion_score_and_blocks_final_total(legacy_api):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(adapt)
    rule_code = _field(_authorized_rules(snapshot)[0], "code")

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[{"rule_ref": rule_code}],
        evidence_result=_invalid_evidence(),
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _field(result, "auto_score") is None
    assert _field(result, "auto_score_status") in {"invalid", "blocked"}
    assert _field(result, "final_total_blocked") is True
    assert _field(result, "applied_effects") == []


@requires_legacy_executor
def test_forged_sufficiency_boolean_without_validated_evidence_cannot_authorize_score(legacy_api):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(adapt)
    rule_code = _field(_authorized_rules(snapshot)[0], "code")
    forged = MappingProxyType(
        {
            "evidence_sufficient": True,
            "valid_evidence": (),
            "issues": ({"code": "NO_VALIDATED_EVIDENCE"},),
            "injection_flagged": False,
        }
    )

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[{"rule_ref": rule_code}],
        evidence_result=forged,
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _field(result, "auto_score") is None
    assert _field(result, "auto_score_status") in {"invalid", "blocked"}
    assert _field(result, "final_total_blocked") is True


@requires_legacy_executor
def test_forged_nonempty_evidence_without_validator_identity_cannot_authorize_score(legacy_api):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(adapt)
    rule_code = _field(_authorized_rules(snapshot)[0], "code")
    forged = MappingProxyType(
        {
            "evidence_sufficient": True,
            "valid_evidence": (
                {
                    "evidence_unit_id": "a" * 64,
                    "quote": "本文采用问卷调查和回归分析。",
                },
            ),
            "issues": (),
            "injection_flagged": False,
        }
    )

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[{"rule_ref": rule_code}],
        evidence_result=forged,
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _field(result, "auto_score") is None
    assert _field(result, "auto_score_status") in {"invalid", "blocked"}
    assert _field(result, "final_total_blocked") is True


def test_frozen_policy_is_deeply_immutable_and_hashes_behavioral_content():
    required = _frozen_policy(evidence_requirement="required", allow_direct=False)
    optional = _frozen_policy(evidence_requirement="optional", allow_direct=False)
    direct = _frozen_policy(evidence_requirement="required", allow_direct=True)

    assert _field(required, "policy_hash") != _field(optional, "policy_hash")
    assert _field(required, "policy_hash") != _field(direct, "policy_hash")
    with pytest.raises(TypeError):
        required["evidence"]["default_policy"] = "optional"


@requires_legacy_executor
def test_optional_invalid_evidence_discards_only_the_effect(legacy_api):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(adapt)
    rule_code = _field(_authorized_rules(snapshot)[0], "code")

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[{"rule_ref": rule_code}],
        evidence_result=_invalid_evidence(requirement="optional"),
        policy=_frozen_policy(evidence_requirement="optional"),
    )

    assert _field(result, "auto_score") == Decimal("10")
    assert _field(result, "applied_effects") == []
    assert _field(result, "final_total_blocked") is False


@requires_legacy_executor
def test_historical_review_only_rule_blocks_instead_of_silently_awarding_full_score(
    legacy_api,
):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(
        adapt,
        _rubric(
            rules=[
                {
                    "match": "未说明数据来源",
                    "points": Decimal("2"),
                    "reason": "数据来源缺失",
                }
            ]
        ),
    )

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[],
        evidence_result=_valid_evidence(),
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _field(result, "auto_score") is None
    assert _field(result, "auto_score_status") == "blocked"
    assert _field(result, "final_total_blocked") is True
    assert any(
        _field(issue, "code") == "LEGACY_RULE_REVIEW_ONLY"
        for issue in _field(result, "validation_issues")
    )


@requires_legacy_executor
def test_scoped_absence_rule_blocks_when_coverage_is_not_authoritative(legacy_api):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(
        adapt,
        _rubric(
            rules=[
                {
                    "match": "未说明责任主体",
                    "points": Decimal("2"),
                    "reason": "责任主体缺失",
                    "evidence_mode": "scoped_absence",
                    "absence_target": "risk_owner",
                }
            ]
        ),
    )

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[],
        evidence_result=_valid_evidence(),
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _field(result, "auto_score") is None
    assert _field(result, "auto_score_status") == "blocked"
    assert _field(result, "final_total_blocked") is True
    assert any(
        _field(issue, "code") == "SCOPED_ABSENCE_COVERAGE_NOT_AUTHORIZED"
        for issue in _field(result, "validation_issues")
    )


@requires_legacy_evidence_composition
def test_complete_scoped_absence_must_be_bound_and_applied_or_remain_blocked(
    legacy_api,
):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(
        adapt,
        _rubric(
            rules=[
                {
                    "match": "未说明责任主体",
                    "points": Decimal("2"),
                    "reason": "责任主体缺失",
                    "evidence_mode": "scoped_absence",
                    "absence_target": "risk_owner",
                }
            ]
        ),
    )
    rule_code = _field(_authorized_rules(snapshot)[0], "code")
    evidence_result = _complete_absence_evidence()

    unresolved = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[],
        evidence_result=evidence_result,
        policy=_frozen_policy(evidence_requirement="required"),
    )
    applied = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[_effect(rule_code)],
        evidence_result=evidence_result,
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _field(evidence_result, "coverage_completeness") == "complete"
    assert _field(unresolved, "auto_score") is None
    assert _field(unresolved, "final_total_blocked") is True
    assert any(
        _field(issue, "code") == "SCOPED_ABSENCE_RULE_UNRESOLVED"
        for issue in _field(unresolved, "validation_issues")
    )
    assert _field(applied, "auto_score") == Decimal("8")
    assert _field(applied, "final_total_blocked") is False


@requires_legacy_executor
def test_legacy_deductive_without_numeric_rules_never_silently_awards_full_score(legacy_api):
    adapt, apply_deductions, _, _ = legacy_api
    snapshot = _adapt(
        adapt,
        _rubric(rules=[{"match": "方法不完整", "points": None, "reason": "需人工判断"}]),
    )

    result = apply_deductions(
        snapshot=snapshot,
        criterion_code="C01",
        model_effects=[],
        evidence_result=_valid_evidence(),
        policy=_frozen_policy(evidence_requirement="required"),
    )

    assert _authorized_rules(snapshot) == []
    assert _field(result, "auto_score") is None
    assert _field(result, "auto_score_status") == "blocked"
    assert _field(result, "need_manual_review", "requires_review") is True
    assert _field(result, "final_total_blocked") is True


@requires_legacy_executor
def test_legacy_direct_compat_accepts_only_valid_bounded_legacy_scores(legacy_api):
    adapt, _, direct, _ = legacy_api
    snapshot = _adapt(adapt)

    result = direct(
        snapshot=snapshot,
        criterion_code="C01",
        model_score=Decimal("7.5"),
        evidence_result=_valid_evidence(),
        policy=_frozen_policy(allow_direct=True),
    )

    assert _field(result, "auto_score") == Decimal("7.5")
    assert _field(result, "auto_score_status") == "calculated"


@requires_legacy_executor
def test_frozen_policy_must_explicitly_allow_legacy_direct_compat(legacy_api):
    adapt, _, direct, _ = legacy_api
    result = direct(
        snapshot=_adapt(adapt),
        criterion_code="C01",
        model_score=Decimal("7.5"),
        evidence_result=_valid_evidence(),
        policy=_frozen_policy(allow_direct=False),
    )

    assert _field(result, "auto_score") is None
    assert _field(result, "auto_score_status") == "blocked"
    assert _field(result, "need_manual_review", "requires_review") is True


@requires_legacy_executor
@pytest.mark.parametrize("model_score", [Decimal("-0.01"), Decimal("10.01")])
def test_legacy_direct_compat_rejects_out_of_range_model_scores(legacy_api, model_score):
    adapt, _, direct, _ = legacy_api
    result = direct(
        snapshot=_adapt(adapt),
        criterion_code="C01",
        model_score=model_score,
        evidence_result=_valid_evidence(),
        policy=_frozen_policy(allow_direct=True),
    )

    assert _field(result, "auto_score") is None
    assert _field(result, "auto_score_status") in {"invalid", "blocked"}


@requires_legacy_executor
def test_legacy_direct_compat_rejects_invalid_evidence_and_can_force_review(legacy_api):
    adapt, _, direct, _ = legacy_api
    snapshot = _adapt(adapt)

    invalid = direct(
        snapshot=snapshot,
        criterion_code="C01",
        model_score=Decimal("7"),
        evidence_result=_invalid_evidence(),
        policy=_frozen_policy(allow_direct=True),
    )
    forced = direct(
        snapshot=snapshot,
        criterion_code="C01",
        model_score=Decimal("7"),
        evidence_result=_valid_evidence(),
        policy=_frozen_policy(allow_direct=True, force_review=True),
    )

    assert _field(invalid, "auto_score") is None
    assert _field(invalid, "final_total_blocked") is True
    assert _field(forced, "auto_score") == Decimal("7")
    assert _field(forced, "auto_score_status") == "calculated"
    assert _field(forced, "need_manual_review", "requires_review") is True


@requires_legacy_executor
def test_formal_rubric_version_cannot_use_legacy_direct_compat(legacy_api):
    adapt, _, direct, error_type = legacy_api
    snapshot = _adapt(adapt)
    formal = _replace_contract(
        snapshot,
        rubric_source_kind="published_version",
        rubric_version_id="version-1",
        version_hash="f" * 64,
    )

    with pytest.raises(error_type, match="legacy|rubric_source_kind|正式|版本"):
        direct(
            snapshot=formal,
            criterion_code="C01",
            model_score=Decimal("7"),
            evidence_result=_valid_evidence(),
            policy=_frozen_policy(allow_direct=True),
        )


@requires_legacy_executor
def test_formal_rubric_version_cannot_use_legacy_arbitrary_points_executor(legacy_api):
    adapt, apply_deductions, _, error_type = legacy_api
    snapshot = _adapt(adapt)
    formal = _replace_contract(
        snapshot,
        rubric_source_kind="published_version",
        rubric_version_id="version-1",
        version_hash="f" * 64,
    )

    with pytest.raises(error_type, match="legacy|rubric_source_kind|正式|版本"):
        apply_deductions(
            snapshot=formal,
            criterion_code="C01",
            model_effects=[{"rule_ref": "C01", "points": Decimal("3")}],
            evidence_result=_valid_evidence(),
            policy=_frozen_policy(evidence_requirement="required"),
        )
