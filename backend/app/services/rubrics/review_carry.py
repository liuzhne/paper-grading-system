"""Keep confirmed generated rows only when their content and scoring context match."""
import json
from decimal import Decimal

from sqlalchemy import select

from backend.app.db import models
from backend.app.services.rubrics import lifecycle
from backend.app.services.rubrics.review_workspace import rule_content, number_text


def signature(rule):
    content = rule_content(rule)
    origin = content.get("origin") or {}
    if not origin.get("generation_fingerprint") or not origin.get("draft_row_key"):
        return None
    # New graph IDs and the whole source row necessarily change when a sibling
    # suggestion is appended. The row's own origin and all executable fields do not.
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
