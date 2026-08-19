"""Publication-time executability validation for versioned rubrics.

The validator is deliberately read-only.  It reports every independently
actionable problem as a stable structured blocker and leaves transaction and
state-transition decisions to :mod:`backend.app.services.rubrics.lifecycle`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import timezone
from decimal import Decimal, InvalidOperation
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db import models
from backend.app.services.scoring.core.policy import compile_scoring_policy
from backend.app.services.scoring.core.policy import validate_weight_configuration
from backend.app.services.scoring.profiles.technical_proposal import (
    TECHNICAL_PROPOSAL_CHECKER_KEYS,
)
from backend.app.services.scoring.profiles.technical_proposal import (
    TechnicalProposalProfile,
)
from backend.app.services.scoring.profiles.thesis import THESIS_CHECKER_KEYS
from backend.app.services.scoring.profiles.thesis import ThesisProfile


_DOCUMENT_SCHEMA = "document-snapshot@1"
_EVENT_FIELDS = {
    "change_id",
    "rule_code",
    "field_path",
    "action",
    "before",
    "after",
    "actor_id",
    "occurred_at",
    "reason",
}


class DefaultPublicationCheckerRegistry:
    """Small deployment registry used by the built-in M4 profiles.

    Runtime scoring still resolves the concrete immutable checker package.  At
    publication time this registry validates the checker identities emitted by
    the bundled import pipeline without importing code from database strings.
    """

    _KNOWN = {
        "technical_proposal.required_fields.v1": {
            "checker_version": "1.0.0",
            "supported_profiles": {"technical_proposal"},
        },
        "thesis.legacy_required_fields.v1": {
            "checker_version": "1.0.0",
            "supported_profiles": {"thesis"},
        },
        **{
            key: {
                "checker_version": "1.0.0",
                "supported_profiles": {"technical_proposal"},
            }
            for key in TECHNICAL_PROPOSAL_CHECKER_KEYS.values()
        },
        **{
            key: {
                "checker_version": "1.0.0",
                "supported_profiles": {"thesis"},
            }
            for key in THESIS_CHECKER_KEYS.values()
        },
        # Pre-M4 provenance fixtures use this key.  It is accepted only by the
        # compatibility publication path in lifecycle, never put in a Core plan.
        "required_structure_presence": {
            "checker_version": "legacy",
            "supported_profiles": {"thesis"},
        },
    }

    def publication_registration(self, checker_key: str):
        return self._KNOWN.get(checker_key)

    def validate_publication_params(self, checker_key: str, params) -> None:
        if not isinstance(params, Mapping):
            raise ValueError("checker params must be an object")
        if checker_key in TECHNICAL_PROPOSAL_CHECKER_KEYS.values():
            TechnicalProposalProfile().build_checker_registry().resolve(
                checker_key=checker_key,
                checker_version="1.0.0",
                checker_params=dict(params),
                profile_key="technical_proposal",
                document_schema_version=_DOCUMENT_SCHEMA,
            )
        elif checker_key == "thesis.legacy_required_fields.v1":
            if set(params) != {"criterion_code", "applies_to"} or any(
                not isinstance(value, str) or not value.strip()
                for value in params.values()
            ):
                raise ValueError(
                    "criterion_code and applies_to must be non-empty strings"
                )
        elif checker_key in THESIS_CHECKER_KEYS.values():
            ThesisProfile().build_checker_registry().resolve(
                checker_key=checker_key,
                checker_version="1.0.0",
                checker_params=dict(params),
                profile_key="thesis",
                document_schema_version=_DOCUMENT_SCHEMA,
            )


DEFAULT_CHECKER_REGISTRY = DefaultPublicationCheckerRegistry()
DEFAULT_BUSINESS_PROFILE_REGISTRY = {
    "technical_proposal": {
        "profile_key": "technical_proposal",
        "profile_version": "technical-proposal-profile@1",
    },
    "thesis": {"profile_key": "thesis", "profile_version": "thesis-v1"},
}
DEFAULT_WORKFLOW_PROFILE_REGISTRY = {
    "template_driven": {
        "profile_key": "template_driven",
        "profile_version": "template-driven@1",
        "require_template_link_confirmation": True,
    },
    "manual_json": {
        "profile_key": "manual_json",
        "profile_version": "manual-json@1",
        "require_template_link_confirmation": False,
    },
}


def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _naive_utc_iso(value) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def _registry_get(registry, key):
    if isinstance(registry, Mapping):
        return registry.get(key)
    getter = getattr(registry, "get", None)
    if callable(getter):
        return getter(key)
    resolver = getattr(registry, "resolve", None)
    if callable(resolver):
        try:
            return resolver(key)
        except (KeyError, ValueError):
            return None
    return None


def _workflow_requires_links(workflow) -> bool:
    if isinstance(workflow, Mapping):
        return bool(workflow.get("require_template_link_confirmation"))
    return bool(getattr(workflow, "require_template_link_confirmation", False))


def _checker_registration(registry, checker_key: str):
    method = getattr(registry, "publication_registration", None)
    if callable(method):
        return method(checker_key)
    entries = getattr(registry, "_entries", None)
    if isinstance(entries, dict) and checker_key in entries:
        return entries[checker_key][0]
    return None


def _validate_checker_params(registry, registration, rule, profile_key: str):
    supported = set(registration.get("supported_profiles") or ())
    if profile_key not in supported:
        return "profile"
    method = getattr(registry, "validate_publication_params", None)
    try:
        if callable(method):
            method(rule.checker_key, rule.checker_params)
        else:
            entries = getattr(registry, "_entries", None)
            if not isinstance(entries, dict) or rule.checker_key not in entries:
                return "unknown"
            _registration, _checker, validate_params = entries[rule.checker_key]
            validate_params(dict(rule.checker_params or {}))
    except (TypeError, ValueError, AssertionError):
        return "params"
    return None


def validate_publishable_rubric(
    session: Session,
    rubric_id: str,
    compilation_id: str,
    *,
    checker_registry=None,
    business_profile_registry=None,
    workflow_profile_registry=None,
    submission_profile_key: str | None = None,
    allow_pre_m4_provenance: bool = False,
):
    """Return a stable tuple of structured publication blockers."""

    if checker_registry is None:
        checker_registry = DEFAULT_CHECKER_REGISTRY
    if business_profile_registry is None:
        business_profile_registry = DEFAULT_BUSINESS_PROFILE_REGISTRY
    if workflow_profile_registry is None:
        workflow_profile_registry = DEFAULT_WORKFLOW_PROFILE_REGISTRY
    rubric = session.get(models.Rubric, rubric_id)
    compilation = session.get(models.RubricCompilation, compilation_id)
    if rubric is None or compilation is None or compilation.rubric_id != rubric_id:
        identity = {"rubric_id": rubric_id, "compilation_id": compilation_id}
        return (
            {
                "code": "compilation_not_validated",
                "field_path": "/compilation",
                "identity": identity,
                "message": "rubric or compilation does not exist or has mismatched lineage",
            },
        )

    versions = session.scalars(
        select(models.RubricVersion).where(
            models.RubricVersion.compilation_id == compilation_id
        )
    ).all()
    version = versions[0] if len(versions) == 1 else None
    criteria = session.scalars(
        select(models.RubricCriterion)
        .where(models.RubricCriterion.rubric_id == rubric_id)
        .order_by(models.RubricCriterion.code)
    ).all()
    rules = []
    if version is not None:
        rules = session.scalars(
            select(models.AtomicRule)
            .where(models.AtomicRule.rubric_version_id == version.id)
            .order_by(models.AtomicRule.rule_code)
        ).all()
    rule_ids = [item.id for item in rules]
    links = []
    if rule_ids:
        links = session.scalars(
            select(models.RuleTemplateLink)
            .where(models.RuleTemplateLink.rule_id.in_(rule_ids))
            .order_by(models.RuleTemplateLink.rule_id, models.RuleTemplateLink.id)
        ).all()
    template_ids = [item.template_item_id for item in links]
    templates = {
        item.id: item
        for item in (
            session.scalars(
                select(models.TemplateItem).where(models.TemplateItem.id.in_(template_ids))
            ).all()
            if template_ids
            else []
        )
    }
    criterion_by_id = {item.id: item for item in criteria}
    rule_by_code = {item.rule_code: item for item in rules}
    rules_by_criterion = defaultdict(list)
    for rule in rules:
        rules_by_criterion[rule.criterion_id].append(rule)

    blockers: list[dict] = []

    def add(code, field_path, message, **entity):
        identity = {
            "rubric_id": rubric_id,
            "compilation_id": compilation_id,
            **{key: value for key, value in entity.items() if value is not None},
        }
        blockers.append(
            {
                "code": code,
                "field_path": field_path,
                "identity": identity,
                "message": message,
            }
        )

    # Pre-M4 graphs predate the validation_result/profile/audit contract.  The
    # lifecycle has already checked rubric/compilation lineage and validated
    # status before requesting this narrow compatibility mode.
    if allow_pre_m4_provenance and version is not None:
        return ()

    validation_result = compilation.validation_result or {}
    if compilation.status != "validated" or validation_result.get("valid") is not True:
        add(
            "compilation_not_validated",
            "/compilation/status",
            "compilation must be validated with a successful validation result",
        )
    if compilation.blockers:
        add(
            "compilation_has_blockers",
            "/compilation/blockers",
            "compilation contains unresolved blockers",
        )
    if version is None:
        add(
            "compilation_not_validated",
            "/rubric_version",
            "compilation must have exactly one rubric version",
        )
        return _sorted_blockers(blockers)

    profile_key = version.business_profile_key
    business_profile = _registry_get(business_profile_registry, profile_key)
    if business_profile is None:
        add(
            "business_profile_unregistered",
            "/rubric_version/business_profile_key",
            "business profile is not registered",
            rubric_version_id=version.id,
        )
    workflow = _registry_get(workflow_profile_registry, version.workflow_profile)
    if workflow is None:
        add(
            "workflow_profile_unregistered",
            "/rubric_version/workflow_profile",
            "workflow profile is not registered",
            rubric_version_id=version.id,
        )
    if submission_profile_key is not None and submission_profile_key != profile_key:
        add(
            "submission_profile_mismatch",
            "/submission/profile_key",
            "submission profile does not match rubric business profile",
            rubric_version_id=version.id,
        )

    try:
        compile_scoring_policy(version.global_policy, total_score=rubric.total_score)
    except (TypeError, ValueError):
        add(
            "global_policy_unsupported",
            "/rubric_version/global_policy",
            "global policy schema or values are unsupported",
            rubric_version_id=version.id,
        )
    try:
        validate_weight_configuration(criteria, total_score=rubric.total_score)
    except (TypeError, ValueError):
        criterion_code = criteria[0].code if criteria else "__missing__"
        add(
            "weight_policy_invalid",
            "/criteria",
            "criteria weights/max scores do not reconcile with total_score",
            criterion_code=criterion_code,
        )

    events = list(compilation.human_changes or [])
    for rule in rules:
        criterion = criterion_by_id.get(rule.criterion_id)
        criterion_code = criterion.code if criterion is not None else "__unknown__"
        rule_entity = {
            "criterion_code": criterion_code,
            "rule_code": rule.rule_code,
        }
        path = f"/atomic_rules/{rule.rule_code}"
        if rule.status != "approved":
            add(
                "rule_not_approved",
                path + "/status",
                "every rule must be approved before publication",
                **rule_entity,
            )
        elif rule.reviewed_by is None or rule.reviewed_at is None:
            add(
                "rule_review_incomplete",
                path + "/review",
                "approved rule must retain reviewer and review time",
                **rule_entity,
            )
        else:
            matches = [
                event
                for event in events
                if isinstance(event, Mapping)
                and event.get("rule_code") == rule.rule_code
                and event.get("action") == "approve"
            ]
            if not matches:
                add(
                    "rule_approval_audit_missing",
                    path + "/audit",
                    "approved rule is missing its immutable approval event",
                    **rule_entity,
                )
            elif not any(
                set(event) == _EVENT_FIELDS
                and event.get("before") == "review"
                and event.get("after") == "approved"
                and event.get("actor_id") == rule.reviewed_by
                and event.get("occurred_at") == _naive_utc_iso(rule.reviewed_at)
                and isinstance(event.get("reason"), str)
                and event.get("reason").strip()
                for event in matches
            ):
                add(
                    "rule_approval_audit_mismatch",
                    path + "/audit",
                    "approval event actor/time/schema does not match the rule review",
                    **rule_entity,
                )

        if rule.judge_type == "deterministic":
            if not rule.checker_key:
                add(
                    "deterministic_checker_missing",
                    path + "/checker_key",
                    "deterministic rule requires a checker key",
                    **rule_entity,
                )
            else:
                registration = _checker_registration(checker_registry, rule.checker_key)
                if registration is None:
                    add(
                        "checker_not_registered",
                        path + "/checker_key",
                        "checker key is not registered",
                        **rule_entity,
                    )
                else:
                    checker_error = _validate_checker_params(
                        checker_registry, registration, rule, profile_key
                    )
                    if checker_error == "profile":
                        add(
                            "checker_profile_unsupported",
                            path + "/checker_key",
                            "checker does not support the rubric business profile",
                            **rule_entity,
                        )
                    elif checker_error in {"params", "unknown"}:
                        add(
                            "checker_params_invalid",
                            path + "/checker_params",
                            "checker parameters do not satisfy the registered schema",
                            **rule_entity,
                        )
        elif rule.judge_type == "semantic":
            evidence = rule.evidence_policy or {}
            if not (
                isinstance(evidence, Mapping)
                and evidence.get("mode")
                and evidence.get("requirement")
                and evidence.get("minimum_coverage") is not None
            ):
                add(
                    "semantic_evidence_policy_missing",
                    path + "/evidence_policy",
                    "semantic rule requires an explicit evidence policy",
                    **rule_entity,
                )
            else:
                coverage = _decimal(evidence.get("minimum_coverage"))
                if (
                    evidence.get("mode")
                    not in {"source_quote", "scoped_absence", "review_only"}
                    or evidence.get("requirement") not in {"required", "optional"}
                    or coverage is None
                    or coverage < 0
                    or (
                        evidence.get("requirement") == "required"
                        and coverage == 0
                    )
                ):
                    add(
                        "semantic_evidence_policy_invalid",
                        path + "/evidence_policy",
                        (
                            "semantic evidence mode, requirement and minimum "
                            "coverage must use the supported closed contract"
                        ),
                        **rule_entity,
                    )

        direction = rule.direction
        effect = rule.effect_type
        levels = list(rule.levels or [])
        criterion_max = _decimal(criterion.max_score) if criterion is not None else None
        max_points = _decimal(rule.max_points)
        cap_points = _decimal(rule.cap_points)
        if direction == "bonus":
            add(
                "bonus_not_enabled",
                path + "/direction",
                "bonus rules are reserved and cannot be published in v1",
                **rule_entity,
            )
        if (direction in {"band", "deduct"} and effect != "score") or (
            direction == "none" and effect not in {"review", "block_submission", "report_only"}
        ):
            add(
                "direction_effect_invalid",
                path + "/effect_type",
                "direction/effect_type pair is not executable",
                **rule_entity,
            )
        if direction == "band":
            if any(
                value is not None
                for value in (
                    rule.max_points,
                    rule.repeat_policy,
                    rule.cap_points,
                    rule.mutex_group,
                )
            ):
                add(
                    "band_rule_invalid",
                    path,
                    "band score fields and mutex_group must be empty",
                    **rule_entity,
                )
            level_codes = [item.level_code for item in levels]
            if (
                len(levels) < 2
                or len(level_codes) != len(set(level_codes))
                or criterion_max is None
                or any(
                    _decimal(item.points) is None
                    or _decimal(item.points) < 0
                    or _decimal(item.points) > criterion_max
                    for item in levels
                )
            ):
                add(
                    "band_levels_invalid",
                    path + "/levels",
                    "band rule requires at least two unique in-range levels",
                    **rule_entity,
                )
        elif direction == "deduct":
            repeat = rule.repeat_policy
            invalid = (
                criterion_max is None
                or max_points is None
                or max_points <= 0
                or max_points > criterion_max
                or repeat not in {"once", "per_occurrence", "capped"}
                or bool(levels)
            )
            if repeat in {"once", "per_occurrence"} and rule.cap_points is not None:
                invalid = True
            if repeat == "capped" and (
                cap_points is None or cap_points <= 0 or cap_points > criterion_max
            ):
                invalid = True
            if invalid:
                add(
                    "deduct_rule_invalid",
                    path,
                    "deduct max/repeat/cap/levels contract is invalid",
                    **rule_entity,
                )
        elif direction == "none":
            if any(
                value is not None
                for value in (rule.max_points, rule.repeat_policy, rule.cap_points)
            ) or levels:
                add(
                    "none_rule_invalid",
                    path,
                    "none-effect rule cannot carry score fields or levels",
                    **rule_entity,
                )

        for dependency in rule.depends_on_rule_codes or []:
            if dependency not in rule_by_code:
                add(
                    "dependency_unknown",
                    path + "/depends_on_rule_codes",
                    f"dependency {dependency!r} does not exist in this rubric version",
                    **rule_entity,
                )

    # Dependency cycles are publication errors; report every member so the UI
    # can locate all edits necessary to break the cycle.
    for code in _cycle_members(rule_by_code):
        rule = rule_by_code[code]
        criterion = criterion_by_id.get(rule.criterion_id)
        add(
            "dependency_cycle",
            f"/atomic_rules/{code}/depends_on_rule_codes",
            "rule dependency graph contains a cycle",
            criterion_code=criterion.code if criterion else "__unknown__",
            rule_code=code,
        )

    mutex_groups = defaultdict(list)
    for rule in rules:
        if rule.mutex_group:
            mutex_groups[rule.mutex_group].append(rule)
    for group, members in mutex_groups.items():
        criterion_ids = {item.criterion_id for item in members}
        if (
            len(members) < 2
            or len(criterion_ids) != 1
            or any(item.direction == "band" for item in members)
        ):
            for rule in members:
                criterion = criterion_by_id.get(rule.criterion_id)
                add(
                    "mutex_definition_invalid",
                    f"/atomic_rules/{rule.rule_code}/mutex_group",
                    f"mutex group {group!r} must have 2+ non-band rules in one criterion",
                    criterion_code=criterion.code if criterion else "__unknown__",
                    rule_code=rule.rule_code,
                )

    for criterion in criteria:
        criterion_rules = rules_by_criterion.get(criterion.id, [])
        score_mode = criterion.scoring_mode
        if not criterion_rules:
            add(
                "criterion_rules_missing",
                f"/criteria/{criterion.code}",
                "every criterion requires at least one executable AtomicRule",
                criterion_code=criterion.code,
            )
        band_rules = [
            item
            for item in criterion_rules
            if item.direction == "band" and item.effect_type == "score"
        ]
        deduct_rules = [item for item in criterion_rules if item.direction == "deduct"]
        if score_mode in {"band", "banded"} or band_rules:
            if len(band_rules) != 1 or deduct_rules:
                add(
                    "band_criterion_invalid",
                    f"/criteria/{criterion.code}",
                    "band criterion requires exactly one band score rule and no deduct rule",
                    criterion_code=criterion.code,
                )
        if score_mode == "review_only":
            review_rules = [
                item
                for item in criterion_rules
                if item.direction == "none" and item.effect_type == "review"
            ]
            if any(item.effect_type == "score" for item in criterion_rules) or not review_rules:
                add(
                    "review_only_criterion_invalid",
                    f"/criteria/{criterion.code}/scoring_mode",
                    "review_only criterion requires a none/review rule and no score effect",
                    criterion_code=criterion.code,
                )
        if score_mode == "llm_direct":
            add(
                "llm_direct_not_publishable",
                f"/criteria/{criterion.code}/scoring_mode",
                "versioned rubrics cannot publish llm_direct criteria",
                criterion_code=criterion.code,
            )
        if criterion.criterion_type == "hybrid" and any(
            isinstance(item, Mapping) and item.get("scoring_mode") == "llm_direct"
            for item in (criterion.sub_checks or [])
        ):
            add(
                "hybrid_llm_direct_not_publishable",
                f"/criteria/{criterion.code}/sub_checks",
                "hybrid leaves must be migrated away from llm_direct",
                criterion_code=criterion.code,
            )

    if workflow is not None and _workflow_requires_links(workflow):
        for link in links:
            if link.review_status == "pending":
                rule = next((item for item in rules if item.id == link.rule_id), None)
                template = templates.get(link.template_item_id)
                add(
                    "template_link_pending",
                    f"/rule_template_links/{link.id}/review_status",
                    "workflow requires every existing template link to be reviewed",
                    rule_code=rule.rule_code if rule else "__unknown__",
                    template_item_code=(
                        template.item_code if template else link.template_item_id
                    ),
                )

    return _sorted_blockers(blockers)


def _cycle_members(rule_by_code):
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []
    cycles: set[str] = set()

    def visit(code: str):
        if code in visiting:
            try:
                index = stack.index(code)
            except ValueError:
                index = 0
            cycles.update(stack[index:])
            return
        if code in visited:
            return
        visiting.add(code)
        stack.append(code)
        for dependency in rule_by_code[code].depends_on_rule_codes or []:
            if dependency in rule_by_code:
                visit(dependency)
        stack.pop()
        visiting.remove(code)
        visited.add(code)

    for rule_code in sorted(rule_by_code):
        visit(rule_code)
    return cycles


def _sorted_blockers(blockers):
    return tuple(
        sorted(
            blockers,
            key=lambda item: (
                item["code"],
                item["field_path"],
                json.dumps(item["identity"], ensure_ascii=False, sort_keys=True),
            ),
        )
    )


__all__ = [
    "DEFAULT_BUSINESS_PROFILE_REGISTRY",
    "DEFAULT_CHECKER_REGISTRY",
    "DEFAULT_WORKFLOW_PROFILE_REGISTRY",
    "validate_publishable_rubric",
]
