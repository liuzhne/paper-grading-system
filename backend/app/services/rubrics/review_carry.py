"""Keep confirmed rules only when their content and scoring context match."""
import json
from decimal import Decimal

from sqlalchemy import select

from backend.app.db import models
from backend.app.services.rubrics import lifecycle
from backend.app.services.rubrics.review_workspace import rule_content, number_text


def signature(rule):
    content = rule_content(rule)
    # New graph IDs and source row IDs necessarily change between compilations.
    # The executable content, stable AI origin (when present) and criterion context
    # are sufficient to decide whether the prior human confirmation is still valid.
    for field in ("id", "criterion_id", "sources"):
        content.pop(field)
    criterion = rule.criterion
    context = {key: getattr(criterion, key) for key in (
        "code", "name", "description", "max_score", "weight", "criterion_type",
        "scoring_mode", "applies_to", "dimension", "evidence_hints", "sub_checks",
    )}
    version = rule.rubric_version
    return json.dumps({"rule": content, "criterion": context,
                       "policy": version.global_policy, "profile": version.business_profile_key,
                       "workflow": version.workflow_profile}, sort_keys=True,
                      default=lambda value: number_text(value) if isinstance(value, Decimal) else str(value))


def snapshot_confirmed_rows(session, predecessor):
    if predecessor is None:
        return {}
    rules = session.scalars(select(models.AtomicRule).join(models.RubricVersion).where(
        models.RubricVersion.compilation_id == predecessor.id,
        models.AtomicRule.status == "approved",
    )).all()
    return {rule.rule_code: (signature(rule), rule.id) for rule in rules if signature(rule)}


def carry_confirmed_rows(session, rubric_id, rules, previous, predecessor_id, actor_id):
    for rule in rules:
        accepted = previous.get(rule.rule_code)
        if not accepted or signature(rule) != accepted[0]:
            continue
        reason = f"复用内容与评分上下文均未改变的既有确认；前序编译 {predecessor_id}，规则 {accepted[1]}"
        lifecycle.submit_atomic_rule_for_review(session, rubric_id, rule.rule_code, actor_id, reason)
        lifecycle.approve_atomic_rule(session, rubric_id, rule.rule_code, actor_id, reason)


def calculate_carry_diff(
    session,
    predecessor,
    next_criteria: list,
    next_profile: str | None = None,
) -> tuple[int, int]:
    """Calculate how many approved rules will be retained vs invalidated.
    Returns (retained_count, invalidated_count)."""
    if predecessor is None:
        return 0, 0
    confirmed = snapshot_confirmed_rows(session, predecessor)
    if not confirmed:
        return 0, 0
    total_approved = len(confirmed)

    next_crit_map: dict[str, tuple[str, float | None]] = {}
    for c in next_criteria:
        code = getattr(c, "code", None) if not isinstance(c, dict) else c.get("code")
        name = getattr(c, "name", None) if not isinstance(c, dict) else c.get("name")
        max_score = getattr(c, "max_score", None) if not isinstance(c, dict) else c.get("max_score")
        if code:
            next_crit_map[code] = (name, float(max_score) if max_score is not None else None)

    prev_version = session.scalar(
        select(models.RubricVersion).where(models.RubricVersion.compilation_id == predecessor.id)
    )
    if prev_version is None:
        return 0, total_approved

    if next_profile and next_profile != prev_version.business_profile_key:
        return 0, total_approved

    rules = session.scalars(select(models.AtomicRule).join(models.RubricVersion).where(
        models.RubricVersion.compilation_id == predecessor.id,
        models.AtomicRule.status == "approved",
    )).all()

    retained = 0
    for rule in rules:
        crit = rule.criterion
        if crit and crit.code in next_crit_map:
            target_name, target_score = next_crit_map[crit.code]
            if crit.name == target_name and (target_score is None or float(crit.max_score) == target_score):
                retained += 1
    invalidated = total_approved - retained
    return retained, invalidated
