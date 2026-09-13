"""Authorized review projection, separate from the body-free recovery graph."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.db import models
from backend.app.services.rubrics.draft_graph import read_execution_draft
from backend.app.services.rubrics import lifecycle
from backend.app.services.rubrics.rule_origin import rule_origin
from backend.app.services.rubrics.executable_validator import validate_publishable_rubric


def number_text(value):
    return format(Decimal(str(value)).normalize(), "f") if value is not None else None


def rule_content(rule):
    """Only rubric content; never model output, prompts or submitted documents."""
    return {
        "id": rule.id,
        "criterion_id": rule.criterion_id,
        "rule_code": rule.rule_code,
        "name": rule.name,
        "rule_text": rule.rule_text,
        "direction": rule.direction,
        "effect_type": rule.effect_type,
        "max_points": number_text(rule.max_points),
        "cap_points": number_text(rule.cap_points),
        "mutex_group": rule.mutex_group,
        "repeat_policy": rule.repeat_policy,
        "checker_key": rule.checker_key,
        "checker_params": rule.checker_params,
        "evidence_policy": rule.evidence_policy,
        "applies_to": rule.applies_to,
        "strictness": rule.strictness,
        "judge_type": rule.judge_type,
        "positive_example": rule.positive_example,
        "negative_example": rule.negative_example,
        "boundary_example": rule.boundary_example,
        "depends_on_rule_codes": rule.depends_on_rule_codes,
        "creation_method": rule.creation_method,
        "origin": rule_origin(rule, rule.criterion),
        "levels": [{"code": level.level_code, "points": number_text(level.points),
                    "descriptor": level.descriptor, "positive_example": level.positive_example,
                    "negative_example": level.negative_example, "display_order": level.display_order} for level in rule.levels],
        "sources": [{"sheet": source.sheet_name, "row": source.row_number,
                     "locator": source.cell_locator, "text": source.raw_text}
                    for source in sorted(rule.source_rules, key=lambda item: item.id)],
    }


def content_token(rule):
    return hashlib.sha256(json.dumps(rule_content(rule), ensure_ascii=False,
                                     sort_keys=True).encode()).hexdigest()


def read_review_workspace(session, rubric_id):
    execution = read_execution_draft(session=session, rubric_id=rubric_id)
    active = execution["active_compilation"]
    version = (active or {}).get("version")
    rules = session.scalars(select(models.AtomicRule).where(
        models.AtomicRule.rubric_version_id == version["id"]
    ).options(selectinload(models.AtomicRule.levels), selectinload(models.AtomicRule.source_rules))
      .order_by(models.AtomicRule.rule_code)).all() if version else []
    return {
        "rubric_id": rubric_id,
        "compilation_id": active["id"] if active else None,
        "structural_blockers": [issue for issue in validate_publishable_rubric(session, rubric_id, active["id"])
                                if issue["code"] not in {"rule_not_approved", "template_link_pending", "compilation_not_validated", "compilation_has_blockers"}] if active else [],
        "atomic_editing": any(rule.creation_method != "manual" for rule in rules),
        "rules": [{**rule_content(rule), "content_token": content_token(rule),
                   "status": rule.status, "reviewed_by": rule.reviewed_by,
                   "reviewed_at": rule.reviewed_at} for rule in rules],
        "template_links": [{"id": link.id, "rule_code": rule.rule_code,
                            "review_status": link.review_status,
                            "text": link.template_item.raw_text,
                            "section_path": link.template_item.section_path,
                            "rationale": link.rationale}
                           for rule in rules for link in rule.template_links],
    }


def confirm_rule(session, rubric_id, rule_code, actor_id, payload):
    # The same parent lock serializes recompilation, editing and confirmation.
    rubric = session.scalar(select(models.Rubric).where(models.Rubric.id == rubric_id)
                            .with_for_update().execution_options(populate_existing=True))
    if rubric is None:
        raise lifecycle.RubricLifecycleError("评分标准不存在")
    lifecycle._require_state(rubric, "draft", "confirm_rule")
    rule, _version, compilation, _criterion = lifecycle._require_rule_context(
        session, rubric_id, rule_code, for_update=True)
    if (compilation.id != payload.compilation_id or rule.id != payload.rule_id
            or content_token(rule) != payload.content_token):
        raise lifecycle.RubricLifecycleError("条款或执行草稿已变化，请重新加载并核对后确认")
    # A retry after a lost response is a no-op; it must not duplicate audit events.
    if rule.status == "approved":
        return rule
    if rule.status == "draft":
        lifecycle.submit_atomic_rule_for_review(session, rubric_id, rule_code,
                                               actor_id, payload.reason)
    return lifecycle.approve_atomic_rule(session, rubric_id, rule_code,
                                         actor_id, payload.reason)
