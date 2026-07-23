"""Safe read model for rubric review/recovery clients.

The projection intentionally excludes source text, model output, prompts and
rule bodies.  It exposes only identifiers and lifecycle state required to call
the explicit review, recompile and publish operations.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db import models


def read_execution_draft(*, session: Session, rubric_id: str) -> dict:
    rubric = session.get(models.Rubric, rubric_id)
    if rubric is None:
        raise ValueError("rubric does not exist")

    compilations = session.scalars(
        select(models.RubricCompilation)
        .where(models.RubricCompilation.rubric_id == rubric_id)
        .order_by(
            models.RubricCompilation.created_at.desc(),
            models.RubricCompilation.id.desc(),
        )
    ).all()
    if rubric.status == "published":
        active_candidates = [
            item
            for item in compilations
            if item.published_at is not None
            and item.published_at == rubric.published_at
            and item.status == "validated"
        ]
    else:
        active_candidates = [
            item
            for item in compilations
            if item.published_at is None and item.status != "superseded"
        ]

    active = active_candidates[0] if len(active_candidates) == 1 else None
    compilation_summaries = [
        {
            "id": item.id,
            "status": item.status,
            "is_active": active is not None and item.id == active.id,
            "blocker_count": len(item.blockers or []),
            "created_at": item.created_at,
            "published_at": item.published_at,
        }
        for item in compilations
    ]
    if active is None:
        return {
            "rubric_id": rubric.id,
            "rubric_status": rubric.status,
            "active_compilation": None,
            "compilations": compilation_summaries,
            "ambiguity": (
                None
                if not active_candidates
                else "multiple active compilations require explicit recovery"
            ),
        }

    versions = session.scalars(
        select(models.RubricVersion).where(
            models.RubricVersion.compilation_id == active.id
        )
    ).all()
    version = versions[0] if len(versions) == 1 else None
    rules = (
        session.scalars(
            select(models.AtomicRule)
            .where(models.AtomicRule.rubric_version_id == version.id)
            .order_by(models.AtomicRule.rule_code)
        ).all()
        if version is not None
        else []
    )
    rule_ids = [item.id for item in rules]
    links = (
        session.scalars(
            select(models.RuleTemplateLink)
            .where(models.RuleTemplateLink.rule_id.in_(rule_ids))
            .order_by(models.RuleTemplateLink.id)
        ).all()
        if rule_ids
        else []
    )
    rule_code_by_id = {item.id: item.rule_code for item in rules}
    return {
        "rubric_id": rubric.id,
        "rubric_status": rubric.status,
        "active_compilation": {
            "id": active.id,
            "status": active.status,
            "blockers": list(active.blockers or []),
            "warnings": list(active.warnings or []),
            "version": (
                {
                    "id": version.id,
                    "version": version.version,
                    "workflow_profile": version.workflow_profile,
                    "business_profile_key": version.business_profile_key,
                }
                if version is not None
                else None
            ),
            "rules": [
                {
                    "id": item.id,
                    "rule_code": item.rule_code,
                    "name": item.name,
                    "direction": item.direction,
                    "effect_type": item.effect_type,
                    "judge_type": item.judge_type,
                    "status": item.status,
                    "reviewed_by": item.reviewed_by,
                    "reviewed_at": item.reviewed_at,
                }
                for item in rules
            ],
            "template_links": [
                {
                    "id": item.id,
                    "rule_code": rule_code_by_id[item.rule_id],
                    "review_status": item.review_status,
                    "reviewed_by": item.reviewed_by,
                    "reviewed_at": item.reviewed_at,
                }
                for item in links
            ],
        },
        "compilations": compilation_summaries,
        "ambiguity": None,
    }


__all__ = ["read_execution_draft"]
