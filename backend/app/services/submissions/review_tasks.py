"""Tenant-safe rule checkpoint and manual-review workflow services."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.db import models
from backend.app.services.submissions.lifecycle import ResourceConflictError
from backend.app.services.submissions.lifecycle import ResourceNotFoundError
from backend.app.services.submissions.review import recalculate_generic_run
from backend.app.services.submissions.review import update_generic_score_item


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def rule_task_projection(task) -> dict:
    return {
        "id": task.id,
        "organization_id": task.organization_id,
        "scoring_run_id": task.scoring_run_id,
        "score_item_id": task.score_item_id,
        "criterion_code": task.criterion_code,
        "rule_code": task.rule_code,
        "judge_type": task.judge_type,
        "dependency_rule_codes": list(task.dependency_rule_codes or []),
        "status": task.status,
        "blocking_final_total": bool(task.blocking_final_total),
        "attempt_count": task.attempt_count,
        "max_attempts": task.max_attempts,
        "provider_error": task.provider_error,
        "result_snapshot": task.result_snapshot,
        "next_attempt_at": task.next_attempt_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def manual_task_projection(task) -> dict:
    return {
        "id": task.id,
        "organization_id": task.organization_id,
        "scoring_run_id": task.scoring_run_id,
        "rule_scoring_task_id": task.rule_scoring_task_id,
        "score_item_id": task.score_item_id,
        "criterion_code": task.criterion_code,
        "rule_code": task.rule_code,
        "trigger_code": task.trigger_code,
        "trigger_message": task.trigger_message,
        "blocking_final_total": bool(task.blocking_final_total),
        "status": task.status,
        "priority": task.priority,
        "assigned_reviewer_id": task.assigned_reviewer_id,
        "claimed_at": task.claimed_at,
        "due_at": task.due_at,
        "resolved_at": task.resolved_at,
        "resolution_type": task.resolution_type,
        "resolution_reason": task.resolution_reason,
        "resolution_evidence": task.resolution_evidence,
        "version": task.version,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def list_rule_tasks(db, *, run_id: str, organization_id: str | None):
    query = (
        select(models.RuleScoringTask)
        .where(models.RuleScoringTask.scoring_run_id == run_id)
        .order_by(
            models.RuleScoringTask.criterion_code,
            models.RuleScoringTask.rule_code,
        )
    )
    if organization_id is not None:
        query = query.where(
            models.RuleScoringTask.organization_id == organization_id
        )
    return db.scalars(query).all()


def list_manual_tasks(
    db,
    *,
    organization_id: str | None,
    status: str | None = None,
):
    query = select(models.ManualReviewTask).order_by(
        models.ManualReviewTask.priority.desc(),
        models.ManualReviewTask.created_at,
        models.ManualReviewTask.id,
    )
    if organization_id is not None:
        query = query.where(
            models.ManualReviewTask.organization_id == organization_id
        )
    if status is not None:
        query = query.where(models.ManualReviewTask.status == status)
    return db.scalars(query).all()


def get_manual_task(db, *, task_id: str, organization_id: str | None):
    task = db.scalar(
        select(models.ManualReviewTask)
        .where(models.ManualReviewTask.id == task_id)
        .options(
            selectinload(models.ManualReviewTask.scoring_run).selectinload(
                models.ScoringRun.document_snapshot
            )
        )
    )
    if task is None or (
        organization_id is not None and task.organization_id != organization_id
    ):
        raise ResourceNotFoundError("manual review task not found")
    return task


def claim_manual_task(
    db,
    *,
    task_id: str,
    organization_id: str | None,
    reviewer_id: str,
    version: int,
):
    task = get_manual_task(
        db, task_id=task_id, organization_id=organization_id
    )
    if task.version != version or task.status != "open":
        raise ResourceConflictError("manual review task was already changed")
    task.status = "claimed"
    task.assigned_reviewer_id = reviewer_id
    task.claimed_at = _now()
    task.version += 1
    db.commit()
    db.refresh(task)
    return task


def release_manual_task(
    db,
    *,
    task_id: str,
    organization_id: str | None,
    reviewer_id: str,
    version: int,
):
    task = get_manual_task(
        db, task_id=task_id, organization_id=organization_id
    )
    if (
        task.version != version
        or task.status != "claimed"
        or task.assigned_reviewer_id != reviewer_id
    ):
        raise ResourceConflictError("manual review task cannot be released")
    task.status = "open"
    task.assigned_reviewer_id = None
    task.claimed_at = None
    task.version += 1
    db.commit()
    db.refresh(task)
    return task


def _validated_evidence(task, evidence) -> list[dict]:
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("manual resolution requires source evidence")
    snapshot = task.scoring_run.document_snapshot
    payload = None if snapshot is None else snapshot.snapshot_payload
    units = {
        item.get("evidence_unit_id"): item
        for item in ((payload or {}).get("evidence_units") or [])
        if isinstance(item, dict)
    }
    accepted = []
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {
            "evidence_unit_id",
            "quote",
        }:
            raise ValueError("manual evidence has invalid fields")
        unit = units.get(item["evidence_unit_id"])
        quote = item["quote"]
        if (
            unit is None
            or not isinstance(quote, str)
            or not quote.strip()
            or quote.strip() not in unit["normalized_text"]
        ):
            raise ValueError("manual evidence is not authorized by the document snapshot")
        accepted.append(
            {
                "evidence_unit_id": item["evidence_unit_id"],
                "quote": quote.strip(),
                "locator": unit["locator"],
            }
        )
    return accepted


def resolve_manual_task(
    db,
    *,
    task_id: str,
    organization_id: str | None,
    reviewer_id: str,
    version: int,
    final_score: float,
    reason: str,
    evidence: list[dict],
):
    task = get_manual_task(
        db, task_id=task_id, organization_id=organization_id
    )
    if (
        task.version != version
        or task.status != "claimed"
        or task.assigned_reviewer_id != reviewer_id
    ):
        raise ResourceConflictError("manual review task cannot be resolved")
    if task.score_item_id is None:
        raise ResourceConflictError("manual review task has no score item")
    accepted_evidence = _validated_evidence(task, evidence)
    item = db.get(models.ScoreItem, task.score_item_id)
    if item is None:
        raise ResourceNotFoundError("manual review score item not found")
    resolution = {
        "invalid": "resolve_validation",
        "blocked": "resolve_block",
    }.get(item.auto_score_status)
    if resolution is None:
        raise ResourceConflictError("score item is not an automatic blocking result")
    update_generic_score_item(
        db,
        item_id=item.id,
        final_score=final_score,
        reason=reason,
        resolution_type=resolution,
        reviewer_id=reviewer_id,
    )
    task = get_manual_task(
        db, task_id=task_id, organization_id=organization_id
    )
    task.status = "resolved"
    task.resolution_type = "human_score"
    task.resolution_reason = reason.strip()
    task.resolution_evidence = accepted_evidence
    task.resolved_at = _now()
    task.version += 1
    db.flush()
    recalculate_generic_run(db, task.scoring_run)
    db.commit()
    db.refresh(task)
    return task


__all__ = [
    "claim_manual_task",
    "get_manual_task",
    "list_manual_tasks",
    "list_rule_tasks",
    "manual_task_projection",
    "release_manual_task",
    "resolve_manual_task",
    "rule_task_projection",
]
