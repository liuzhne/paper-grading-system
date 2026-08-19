"""Auditable human review for generic Core runs."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.db import models
from backend.app.services.scoring.core.policy import aggregate_scores
from backend.app.services.scoring.core.policy import compile_scoring_policy
from backend.app.services.submissions.lifecycle import ResourceConflictError
from backend.app.services.submissions.lifecycle import ResourceNotFoundError
from backend.app.services.submissions.lifecycle import get_scoring_run


_EXPECTED_RESOLUTION = {
    "invalid": "resolve_validation",
    "blocked": "resolve_block",
}


def _policy(run):
    snapshot = run.policy_snapshot
    if not isinstance(snapshot, dict):
        raise ResourceConflictError("v2 run has no frozen scoring policy")
    total_score = (snapshot.get("aggregation") or {}).get("total_score")
    compiled = compile_scoring_policy(snapshot, total_score=total_score)
    if compiled.policy_hash != run.policy_hash:
        raise ResourceConflictError("frozen policy identity mismatch")
    if compiled.schema_version != run.policy_schema_version:
        raise ResourceConflictError("frozen policy schema identity mismatch")
    if compiled.usage != "authoritative_new_runs":
        raise ResourceConflictError("non-authoritative policy cannot be reviewed")
    return compiled


def _item_row(item, *, use_final: bool, resolved: bool) -> dict:
    aggregation = item.aggregation or {}
    row = {
        "criterion_code": (
            aggregation.get("criterion_code")
            or item.criterion_code
            or item.criterion_id
        ),
        "raw_score": item.ai_score,
        "max_score": item.max_score,
        "weight": aggregation.get("weight"),
        "auto_score_status": (
            "calculated"
            if resolved and item.auto_score_status in _EXPECTED_RESOLUTION
            else item.auto_score_status or "calculated"
        ),
    }
    if use_final:
        row["final_score"] = item.final_score
    return row


def _resolution_logs(db, run_id: str):
    logs = db.scalars(
        select(models.ReviewLog)
        .where(
            models.ReviewLog.scoring_run_id == run_id,
            models.ReviewLog.score_item_id.is_not(None),
        )
        .order_by(models.ReviewLog.created_at, models.ReviewLog.id)
    ).all()
    return {log.score_item_id: log for log in logs}


def _resolved(item, logs) -> bool:
    expected = _EXPECTED_RESOLUTION.get(item.auto_score_status)
    if expected is None:
        return False
    log = logs.get(item.id)
    return bool(
        log is not None
        and log.resolution_type == expected
        and log.after_score is not None
    )


def unresolved_automatic_failures(db, run) -> list[str]:
    logs = _resolution_logs(db, run.id)
    return [
        item.id
        for item in run.items
        if item.auto_score_status in _EXPECTED_RESOLUTION
        and not _resolved(item, logs)
    ]


def recalculate_generic_run(db, run) -> None:
    policy = _policy(run)
    logs = _resolution_logs(db, run.id)
    ai_result = aggregate_scores(
        policy,
        [
            _item_row(item, use_final=False, resolved=False)
            for item in run.items
        ],
    )
    final_result = aggregate_scores(
        policy,
        [
            _item_row(
                item,
                use_final=True,
                resolved=_resolved(item, logs),
            )
            for item in run.items
        ],
    )
    run.ai_total_score = ai_result.rounded_total
    run.final_total_score = final_result.rounded_total
    run.grade = final_result.grade
    run.need_manual_review = bool(
        final_result.need_manual_review
        or unresolved_automatic_failures(db, run)
    )


def update_generic_score_item(
    db,
    *,
    item_id: str,
    final_score: float,
    reason: str,
    resolution_type: str,
    reviewer_id: str,
):
    item = db.scalar(
        select(models.ScoreItem)
        .where(models.ScoreItem.id == item_id)
        .options(
            selectinload(models.ScoreItem.criterion),
            selectinload(models.ScoreItem.scoring_run).selectinload(
                models.ScoringRun.items
            ).selectinload(models.ScoreItem.criterion),
            selectinload(models.ScoreItem.scoring_run).selectinload(
                models.ScoringRun.submission
            ),
        )
    )
    if item is None or item.scoring_run.submission_id is None:
        raise ResourceNotFoundError("v2 score item not found")
    if final_score < 0 or final_score > float(item.max_score):
        raise ValueError("final_score out of range")
    status = item.auto_score_status or "calculated"
    expected = _EXPECTED_RESOLUTION.get(status, "ordinary_override")
    if resolution_type != expected:
        raise ResourceConflictError(
            "resolution capability does not match automatic score status"
        )
    before = item.final_score if item.final_score is not None else item.ai_score
    item.final_score = Decimal(str(final_score))
    run = item.scoring_run
    run.status = "reviewing"
    run.need_manual_review = True
    run.submission.status = "pending_review"
    db.add(
        models.ReviewLog(
            scoring_run_id=run.id,
            score_item_id=item.id,
            reviewer_id=reviewer_id,
            before_score=before,
            after_score=item.final_score,
            reason=reason.strip(),
            policy_hash=run.policy_hash,
            resolution_type=resolution_type,
        )
    )
    db.flush()
    recalculate_generic_run(db, run)
    db.commit()
    db.refresh(item)
    return item


def submit_generic_review(db, *, run_id: str, reason: str, reviewer_id: str):
    run = get_scoring_run(db, run_id)
    recalculate_generic_run(db, run)
    unresolved = unresolved_automatic_failures(db, run)
    if unresolved or run.final_total_score is None:
        raise ResourceConflictError(
            "review has unresolved invalid or blocked automatic scores"
        )
    run.status = "reviewed"
    run.need_manual_review = False
    run.submission.status = "reviewed"
    db.add(
        models.ReviewLog(
            scoring_run_id=run.id,
            score_item_id=None,
            reviewer_id=reviewer_id,
            before_score=run.ai_total_score,
            after_score=run.final_total_score,
            reason=reason.strip(),
            policy_hash=run.policy_hash,
            resolution_type="ordinary_override",
        )
    )
    db.commit()
    db.refresh(run)
    return run


def review_logs(db, run_id: str):
    get_scoring_run(db, run_id)
    return db.scalars(
        select(models.ReviewLog)
        .where(models.ReviewLog.scoring_run_id == run_id)
        .order_by(models.ReviewLog.created_at, models.ReviewLog.id)
    ).all()


def review_log_projection(log) -> dict:
    def number(value):
        return None if value is None else float(value)

    return {
        "id": log.id,
        "scoring_run_id": log.scoring_run_id,
        "score_item_id": log.score_item_id,
        "reviewer_id": log.reviewer_id,
        "before_score": number(log.before_score),
        "after_score": number(log.after_score),
        "reason": log.reason,
        "policy_hash": log.policy_hash,
        "resolution_type": log.resolution_type,
        "created_at": log.created_at,
    }


__all__ = [
    "recalculate_generic_run",
    "review_log_projection",
    "review_logs",
    "submit_generic_review",
    "unresolved_automatic_failures",
    "update_generic_score_item",
]
