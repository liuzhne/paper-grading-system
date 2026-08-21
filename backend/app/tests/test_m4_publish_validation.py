"""M4.2 publication-time executability validation contracts.

The validator is intentionally specified before the production implementation.
Only the exact absence of ``validate_publishable_rubric`` is an expected strict
XFAIL.  Once the symbol exists, import defects, signature drift and behavioral
errors are ordinary failures.

The validator owns no transaction and mutates no ORM state.  It returns a
stably ordered sequence of structured blockers; the lifecycle facade decides
whether a publication attempt may proceed.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import importlib
import json
from types import ModuleType

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.sqlite import enable_sqlite_foreign_keys
from backend.app.services.scoring.core.checker_registry import VersionedCheckerRegistry
from backend.app.services.scoring.core.policy import build_corrected_thesis_policy
from backend.app.tests.m2_contract_fixtures import PROFILE_KEY
from backend.app.tests.m3_contract_fixtures import (
    CHECKER_KEY,
    checker_registration_payload,
)
from backend.app.tests.test_rubric_version_lifecycle import _make_full_graph


VALIDATOR_MODULE = "backend.app.services.rubrics.executable_validator"
VALIDATOR_SYMBOL = "validate_publishable_rubric"


class M4CapabilityUnavailable(RuntimeError):
    """The only error allowed to convert a missing M4 capability to XFAIL."""


def _probe_module(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        target_missing = exc.name == name or (
            exc.name is not None and name.startswith(exc.name + ".")
        )
        if not target_missing:
            raise
        return None


_VALIDATOR_MODULE = _probe_module(VALIDATOR_MODULE)
_VALIDATE_PUBLISHABLE = (
    None
    if _VALIDATOR_MODULE is None
    else vars(_VALIDATOR_MODULE).get(VALIDATOR_SYMBOL)
)


requires_publish_validator = pytest.mark.xfail(
    condition=not callable(_VALIDATE_PUBLISHABLE),
    reason=(
        "M4 capability is not implemented: "
        f"{VALIDATOR_MODULE}.{VALIDATOR_SYMBOL}"
    ),
    raises=M4CapabilityUnavailable,
    strict=True,
)


def _require_validator():
    if not callable(_VALIDATE_PUBLISHABLE):
        raise M4CapabilityUnavailable(
            f"{VALIDATOR_MODULE}.{VALIDATOR_SYMBOL} is not implemented"
        )
    return _VALIDATE_PUBLISHABLE


@pytest.fixture()
def m4_publish_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    enable_sqlite_foreign_keys(engine)
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _audit_event(graph, rule, *, action="approve", actor_id=None, occurred_at=None):
    actor_id = actor_id or graph.user.id
    occurred_at = occurred_at or rule.reviewed_at
    before, after = (
        ("review", "approved") if action == "approve" else ("review", "rejected")
    )
    return {
        "change_id": f"m4-{rule.rule_code.lower()}-{action}",
        "rule_code": rule.rule_code,
        "field_path": f"/atomic_rules/{rule.rule_code}/status",
        "action": action,
        "before": before,
        "after": after,
        "actor_id": actor_id,
        "occurred_at": occurred_at.isoformat(timespec="microseconds"),
        "reason": f"M4 fixture {action}",
    }


def _checker_registry():
    registry = VersionedCheckerRegistry()

    def validate_params(params):
        if not isinstance(params, dict) or set(params) != {"required_fields"}:
            raise ValueError("required_fields is required")
        values = params["required_fields"]
        if not isinstance(values, list) or not values or not all(
            isinstance(item, str) and item for item in values
        ):
            raise ValueError("required_fields must be a non-empty string array")

    registry.register(
        registration=checker_registration_payload(),
        checker=lambda **_: {"observations": []},
        validate_params=validate_params,
    )
    return registry


def _runtime(*, include_business=True, include_workflow=True):
    return {
        "checker_registry": _checker_registry(),
        "business_profile_registry": (
            {
                PROFILE_KEY: {
                    "profile_key": PROFILE_KEY,
                    "profile_version": "technical-proposal-test-profile@1",
                }
            }
            if include_business
            else {}
        ),
        "workflow_profile_registry": (
            {
                "template_driven": {
                    "profile_key": "template_driven",
                    "profile_version": "template-driven@1",
                    "require_template_link_confirmation": True,
                }
            }
            if include_workflow
            else {}
        ),
    }


def _valid_graph(db, suffix: str):
    graph = _make_full_graph(db, f"m4-publish-{suffix}")
    graph.rubric.total_score = 4
    graph.criterion.max_score = 2
    graph.criterion.weight = None
    graph.criterion.criterion_type = "deterministic"
    graph.criterion.scoring_mode = "deductive"
    graph.criterion.sub_checks = []
    semantic_criterion = models.RubricCriterion(
        rubric_id=graph.rubric.id,
        code="COHERENCE",
        name="研究过程连贯性",
        max_score=2,
        weight=None,
        criterion_type="llm_judgment",
        scoring_mode="banded",
        applies_to="第三章 研究方法",
        display_order=1,
    )
    db.add(semantic_criterion)
    db.flush()
    graph.semantic.criterion_id = semantic_criterion.id
    graph.semantic_criterion = semantic_criterion
    graph.semantic.levels[-1].points = 2
    graph.compilation.status = "validated"
    graph.compilation.validation_result = {"valid": True}
    graph.compilation.blockers = []
    graph.version.business_profile_key = PROFILE_KEY
    graph.version.workflow_profile = "template_driven"
    graph.version.global_policy = build_corrected_thesis_policy(
        4, "points"
    ).to_mapping()

    graph.deterministic.checker_key = CHECKER_KEY
    graph.deterministic.checker_params = {"required_fields": ["risk_owner"]}
    graph.deterministic.evidence_policy = {
        "mode": "scoped_absence",
        "requirement": "required",
        "minimum_coverage": "1",
    }
    graph.deterministic.cap_points = None
    graph.deterministic.mutex_group = None
    graph.deterministic.status = "approved"
    graph.semantic.mutex_group = None
    graph.semantic.status = "approved"
    graph.semantic.max_points = None
    graph.semantic.repeat_policy = None
    graph.semantic.cap_points = None
    graph.semantic.evidence_policy = {
        "mode": "source_quote",
        "requirement": "required",
        "minimum_coverage": "1",
    }
    graph.compilation.human_changes = [
        _audit_event(graph, graph.deterministic),
        _audit_event(graph, graph.semantic),
    ]
    db.commit()
    return graph


def _as_mapping(value) -> dict:
    if isinstance(value, dict):
        return deepcopy(value)
    method = getattr(value, "to_mapping", None)
    assert callable(method), "publication blockers must be mappings or expose to_mapping()"
    mapped = method()
    assert isinstance(mapped, dict)
    return deepcopy(mapped)


def _blockers(result) -> list[dict]:
    assert result is not None, "validator must return an empty sequence for a valid graph"
    assert not isinstance(result, (str, bytes, dict)), (
        "validator must return a sequence of structured blockers, not text or one object"
    )
    values = [_as_mapping(item) for item in result]
    for blocker in values:
        assert set(blocker) == {"code", "field_path", "identity", "message"}
        assert isinstance(blocker["code"], str) and blocker["code"]
        assert isinstance(blocker["field_path"], str) and blocker["field_path"].startswith("/")
        assert isinstance(blocker["message"], str) and blocker["message"].strip()
        identity = blocker["identity"]
        assert isinstance(identity, dict) and identity
        assert identity.get("rubric_id")
        assert identity.get("compilation_id")
    sort_keys = [
        (
            item["code"],
            item["field_path"],
            json.dumps(item["identity"], ensure_ascii=False, sort_keys=True),
        )
        for item in values
    ]
    assert sort_keys == sorted(sort_keys), "publication blockers must have stable ordering"
    return values


def _validate(db, graph, *, runtime=None, submission_profile_key=None):
    validator = _require_validator()
    runtime = runtime or _runtime()
    return _blockers(
        validator(
            db,
            graph.rubric.id,
            graph.compilation.id,
            checker_registry=runtime["checker_registry"],
            business_profile_registry=runtime["business_profile_registry"],
            workflow_profile_registry=runtime["workflow_profile_registry"],
            submission_profile_key=submission_profile_key,
        )
    )


@requires_publish_validator
def test_valid_complete_graph_has_no_publication_blockers(m4_publish_db):
    graph = _valid_graph(m4_publish_db, "valid")
    assert _validate(m4_publish_db, graph) == []


@requires_publish_validator
def test_default_thesis_checker_params_match_runtime_contract():
    _require_validator()
    registry_type = vars(_VALIDATOR_MODULE).get(
        "DefaultPublicationCheckerRegistry"
    )
    if registry_type is None:
        raise M4CapabilityUnavailable(
            f"{VALIDATOR_MODULE}.DefaultPublicationCheckerRegistry is not implemented"
        )
    registry = registry_type()
    registry.validate_publication_params(
        "thesis.legacy_required_fields.v1",
        {"criterion_code": "C1", "applies_to": "global"},
    )
    for invalid in (
        {},
        {"criterion_code": "C1"},
        {"criterion_code": "", "applies_to": "global"},
        {
            "criterion_code": "C1",
            "applies_to": "global",
            "unexpected": True,
        },
    ):
        with pytest.raises(ValueError):
            registry.validate_publication_params(
                "thesis.legacy_required_fields.v1", invalid
            )


@requires_publish_validator
def test_legal_review_only_criterion_with_none_review_rule_is_publishable(
    m4_publish_db,
):
    graph = _valid_graph(m4_publish_db, "valid-review-only")
    graph.criterion.scoring_mode = "review_only"
    graph.deterministic.direction = "none"
    graph.deterministic.effect_type = "review"
    graph.deterministic.max_points = None
    graph.deterministic.repeat_policy = None
    graph.deterministic.cap_points = None
    m4_publish_db.commit()

    assert _validate(m4_publish_db, graph) == []


@requires_publish_validator
def test_validator_is_read_only_and_returns_repeatable_results(m4_publish_db):
    graph = _valid_graph(m4_publish_db, "pure")
    graph.compilation.blockers = [{"code": "SOURCE_PARSE_FAILED"}]
    m4_publish_db.commit()
    before = (
        graph.rubric.status,
        graph.compilation.status,
        deepcopy(graph.compilation.human_changes),
        [(rule.rule_code, rule.status) for rule in (graph.deterministic, graph.semantic)],
    )

    first = _validate(m4_publish_db, graph)
    second = _validate(m4_publish_db, graph)

    assert first == second
    assert before == (
        graph.rubric.status,
        graph.compilation.status,
        graph.compilation.human_changes,
        [(rule.rule_code, rule.status) for rule in (graph.deterministic, graph.semantic)],
    )


def _compilation_not_validated(graph):
    graph.compilation.status = "parsed"


def _validation_result_invalid(graph):
    graph.compilation.validation_result = {"valid": False}


def _compilation_has_blockers(graph):
    graph.compilation.blockers = [{"code": "IMPORT_INCOMPLETE"}]


def _rule_not_approved(graph):
    graph.deterministic.status = "review"


def _rule_reviewer_missing(graph):
    graph.deterministic.reviewed_by = None


def _rule_review_time_missing(graph):
    graph.deterministic.reviewed_at = None


def _rule_approval_event_missing(graph):
    graph.compilation.human_changes = [
        item
        for item in graph.compilation.human_changes
        if item["rule_code"] != graph.deterministic.rule_code
    ]


def _rule_approval_event_mismatches_reviewer(graph):
    event = graph.compilation.human_changes[0]
    graph.compilation.human_changes = [
        {**event, "actor_id": "different-reviewer"},
        graph.compilation.human_changes[1],
    ]


def _rule_approval_event_mismatches_time(graph):
    event = graph.compilation.human_changes[0]
    graph.compilation.human_changes = [
        {
            **event,
            "occurred_at": (graph.deterministic.reviewed_at + timedelta(seconds=1)).isoformat(
                timespec="microseconds"
            ),
        },
        graph.compilation.human_changes[1],
    ]


def _deterministic_checker_missing(graph):
    graph.deterministic.checker_key = None


def _checker_unknown(graph):
    graph.deterministic.checker_key = "technical_proposal.unknown.v1"


def _checker_params_invalid(graph):
    graph.deterministic.checker_params = {"required_fields": []}


def _semantic_evidence_missing(graph):
    graph.semantic.evidence_policy = {}


def _semantic_evidence_mode_invalid(graph):
    graph.semantic.evidence_policy["mode"] = "free_form_model_claim"


def _semantic_evidence_requirement_invalid(graph):
    graph.semantic.evidence_policy["requirement"] = "trust_model"


def _semantic_evidence_coverage_invalid(graph):
    graph.semantic.evidence_policy["minimum_coverage"] = "-0.1"


def _criterion_without_atomic_rules(graph):
    graph.deterministic.criterion_id = graph.semantic_criterion.id


def _band_levels_missing(graph):
    for level in list(graph.semantic.levels):
        graph.semantic.levels.remove(level)


def _band_level_out_of_range(graph):
    graph.semantic.levels[-1].points = 3


def _dependency_unknown(graph):
    graph.deterministic.depends_on_rule_codes = ["DOES-NOT-EXIST"]


def _dependency_cycle(graph):
    graph.deterministic.depends_on_rule_codes = [graph.semantic.rule_code]
    graph.semantic.depends_on_rule_codes = [graph.deterministic.rule_code]


def _mutex_singleton(graph):
    graph.deterministic.mutex_group = "METHOD-EXCLUSIVE"


def _template_link_pending(graph):
    graph.link.review_status = "pending"
    graph.link.reviewed_by = None
    graph.link.reviewed_at = None


def _global_policy_unsupported(graph):
    graph.version.global_policy = {
        **graph.version.global_policy,
        "schema_version": "scoring-policy@999",
    }


def _criteria_total_mismatch(graph):
    graph.rubric.total_score = 5


def _llm_direct(graph):
    graph.criterion.criterion_type = "llm_judgment"
    graph.criterion.scoring_mode = "llm_direct"


def _hybrid_contains_llm_direct(graph):
    graph.criterion.criterion_type = "hybrid"
    graph.criterion.scoring_mode = "deductive"
    graph.criterion.sub_checks = [
        {"code": "SAFE", "scoring_mode": "deductive"},
        {"code": "DIRECT", "scoring_mode": "llm_direct"},
    ]


def _bonus_not_enabled(graph):
    graph.deterministic.direction = "bonus"


def _band_score_fields_not_empty(graph):
    graph.semantic.max_points = 1


def _band_mixes_deduct(graph):
    graph.deterministic.criterion_id = graph.semantic_criterion.id


def _band_has_multiple_score_rules(graph):
    rule = graph.deterministic
    rule.criterion_id = graph.semantic_criterion.id
    rule.direction = "band"
    rule.effect_type = "score"
    rule.judge_type = "semantic"
    rule.checker_key = None
    rule.checker_params = {}
    rule.evidence_policy = {
        "mode": "source_quote",
        "requirement": "required",
        "minimum_coverage": "1",
    }
    rule.max_points = None
    rule.repeat_policy = None
    rule.cap_points = None
    rule.mutex_group = None
    rule.levels.extend(
        [
            models.RuleLevel(
                level_code="ALT-LOW",
                points=0,
                descriptor="alternate low",
                display_order=0,
            ),
            models.RuleLevel(
                level_code="ALT-HIGH",
                points=2,
                descriptor="alternate high",
                display_order=1,
            ),
        ]
    )


def _deduct_max_points_missing(graph):
    graph.deterministic.max_points = None


def _deduct_max_points_zero(graph):
    graph.deterministic.max_points = 0


def _deduct_max_points_above_criterion(graph):
    graph.deterministic.max_points = 3


def _deduct_repeat_policy_missing(graph):
    graph.deterministic.repeat_policy = None


def _deduct_once_has_cap(graph):
    graph.deterministic.repeat_policy = "once"
    graph.deterministic.cap_points = 1


def _deduct_capped_missing_cap(graph):
    graph.deterministic.repeat_policy = "capped"
    graph.deterministic.cap_points = None


def _deduct_capped_zero_cap(graph):
    graph.deterministic.repeat_policy = "capped"
    graph.deterministic.cap_points = 0


def _deduct_capped_above_criterion(graph):
    graph.deterministic.repeat_policy = "capped"
    graph.deterministic.cap_points = 3


def _deduct_has_levels(graph):
    graph.deterministic.levels.append(
        models.RuleLevel(
            level_code="ILLEGAL",
            points=1,
            descriptor="deduct rule must not carry levels",
            display_order=0,
        )
    )


def _direction_effect_pair_invalid(graph):
    graph.deterministic.effect_type = "review"


def _none_rule_keeps_score_fields(graph):
    graph.deterministic.direction = "none"
    graph.deterministic.effect_type = "review"


def _review_only_contains_score_rule(graph):
    graph.criterion.scoring_mode = "review_only"


def _review_only_empty(graph):
    graph.criterion.scoring_mode = "review_only"
    graph.deterministic.criterion_id = graph.semantic_criterion.id


def _review_only_with_only_non_review_effect(graph, effect_type):
    graph.criterion.scoring_mode = "review_only"
    rule = graph.deterministic
    rule.direction = "none"
    rule.effect_type = effect_type
    rule.max_points = None
    rule.repeat_policy = None
    rule.cap_points = None


def _review_only_only_report(graph):
    _review_only_with_only_non_review_effect(graph, "report_only")


def _review_only_only_block(graph):
    _review_only_with_only_non_review_effect(graph, "block_submission")


def _mutex_crosses_criteria(graph):
    graph.deterministic.mutex_group = "CROSS-CRITERION"
    graph.semantic.mutex_group = "CROSS-CRITERION"


@pytest.mark.parametrize(
    ("mutation", "expected_code", "identity_key"),
    [
        (_compilation_not_validated, "compilation_not_validated", "compilation_id"),
        (_validation_result_invalid, "compilation_not_validated", "compilation_id"),
        (_compilation_has_blockers, "compilation_has_blockers", "compilation_id"),
        (_rule_not_approved, "rule_not_approved", "rule_code"),
        (_rule_reviewer_missing, "rule_review_incomplete", "rule_code"),
        (_rule_review_time_missing, "rule_review_incomplete", "rule_code"),
        (_rule_approval_event_missing, "rule_approval_audit_missing", "rule_code"),
        (_rule_approval_event_mismatches_reviewer, "rule_approval_audit_mismatch", "rule_code"),
        (_rule_approval_event_mismatches_time, "rule_approval_audit_mismatch", "rule_code"),
        (_deterministic_checker_missing, "deterministic_checker_missing", "rule_code"),
        (_checker_unknown, "checker_not_registered", "rule_code"),
        (_checker_params_invalid, "checker_params_invalid", "rule_code"),
        (_semantic_evidence_missing, "semantic_evidence_policy_missing", "rule_code"),
        (_semantic_evidence_mode_invalid, "semantic_evidence_policy_invalid", "rule_code"),
        (
            _semantic_evidence_requirement_invalid,
            "semantic_evidence_policy_invalid",
            "rule_code",
        ),
        (
            _semantic_evidence_coverage_invalid,
            "semantic_evidence_policy_invalid",
            "rule_code",
        ),
        (_criterion_without_atomic_rules, "criterion_rules_missing", "criterion_code"),
        (_band_levels_missing, "band_levels_invalid", "rule_code"),
        (_band_level_out_of_range, "band_levels_invalid", "rule_code"),
        (_dependency_unknown, "dependency_unknown", "rule_code"),
        (_dependency_cycle, "dependency_cycle", "rule_code"),
        (_mutex_singleton, "mutex_definition_invalid", "rule_code"),
        (_template_link_pending, "template_link_pending", "template_item_code"),
        (_global_policy_unsupported, "global_policy_unsupported", "rubric_version_id"),
        (_criteria_total_mismatch, "weight_policy_invalid", "criterion_code"),
        (_llm_direct, "llm_direct_not_publishable", "criterion_code"),
        (_hybrid_contains_llm_direct, "hybrid_llm_direct_not_publishable", "criterion_code"),
        (_bonus_not_enabled, "bonus_not_enabled", "rule_code"),
        (_band_score_fields_not_empty, "band_rule_invalid", "rule_code"),
        (_band_mixes_deduct, "band_criterion_invalid", "criterion_code"),
        (_band_has_multiple_score_rules, "band_criterion_invalid", "criterion_code"),
        (_deduct_max_points_missing, "deduct_rule_invalid", "rule_code"),
        (_deduct_max_points_zero, "deduct_rule_invalid", "rule_code"),
        (_deduct_max_points_above_criterion, "deduct_rule_invalid", "rule_code"),
        (_deduct_repeat_policy_missing, "deduct_rule_invalid", "rule_code"),
        (_deduct_once_has_cap, "deduct_rule_invalid", "rule_code"),
        (_deduct_capped_missing_cap, "deduct_rule_invalid", "rule_code"),
        (_deduct_capped_zero_cap, "deduct_rule_invalid", "rule_code"),
        (_deduct_capped_above_criterion, "deduct_rule_invalid", "rule_code"),
        (_deduct_has_levels, "deduct_rule_invalid", "rule_code"),
        (_direction_effect_pair_invalid, "direction_effect_invalid", "rule_code"),
        (_none_rule_keeps_score_fields, "none_rule_invalid", "rule_code"),
        (_review_only_contains_score_rule, "review_only_criterion_invalid", "criterion_code"),
        (_review_only_empty, "review_only_criterion_invalid", "criterion_code"),
        (_review_only_only_report, "review_only_criterion_invalid", "criterion_code"),
        (_review_only_only_block, "review_only_criterion_invalid", "criterion_code"),
        (_mutex_crosses_criteria, "mutex_definition_invalid", "rule_code"),
    ],
)
@requires_publish_validator
def test_each_m4_publication_blocker_is_machine_readable_and_located(
    m4_publish_db,
    mutation,
    expected_code,
    identity_key,
):
    graph = _valid_graph(m4_publish_db, expected_code)
    mutation(graph)
    m4_publish_db.commit()

    blockers = _validate(m4_publish_db, graph)

    matching = [item for item in blockers if item["code"] == expected_code]
    assert matching, f"missing expected publication blocker {expected_code!r}"
    assert all(item["identity"].get(identity_key) for item in matching)


@pytest.mark.parametrize(
    ("runtime", "submission_profile_key", "expected_code"),
    [
        (_runtime(include_business=False), None, "business_profile_unregistered"),
        (_runtime(include_workflow=False), None, "workflow_profile_unregistered"),
        (_runtime(), "thesis", "submission_profile_mismatch"),
    ],
)
@requires_publish_validator
def test_profile_registration_and_submission_compatibility_are_publish_blockers(
    m4_publish_db,
    runtime,
    submission_profile_key,
    expected_code,
):
    graph = _valid_graph(m4_publish_db, expected_code)
    blockers = _validate(
        m4_publish_db,
        graph,
        runtime=runtime,
        submission_profile_key=submission_profile_key,
    )
    assert expected_code in {item["code"] for item in blockers}


@requires_publish_validator
def test_registered_business_profile_still_requires_checker_profile_support(
    m4_publish_db,
):
    graph = _valid_graph(m4_publish_db, "checker-profile-support")
    graph.version.business_profile_key = "thesis"
    m4_publish_db.commit()
    runtime = _runtime()
    runtime["business_profile_registry"]["thesis"] = {
        "profile_key": "thesis",
        "profile_version": "thesis-v1",
    }

    blockers = _validate(m4_publish_db, graph, runtime=runtime)

    assert "checker_profile_unsupported" in {item["code"] for item in blockers}


@requires_publish_validator
def test_multiple_blockers_are_deterministic_regardless_of_insertion_order(m4_publish_db):
    graph = _valid_graph(m4_publish_db, "stable-order")
    graph.semantic.depends_on_rule_codes = ["MISSING-Z", "MISSING-A"]
    graph.semantic.evidence_policy = {}
    graph.deterministic.checker_key = None
    graph.compilation.blockers = [
        {"code": "Z-LAST"},
        {"code": "A-FIRST"},
    ]
    m4_publish_db.commit()

    blockers = _validate(m4_publish_db, graph)

    assert len(blockers) >= 4
    assert blockers == _validate(m4_publish_db, graph)
