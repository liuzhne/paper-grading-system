"""Stable, profile-neutral ``grading-core/run-export@2`` projection."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
import json

from sqlalchemy import select

from backend.app.db import models
from backend.app.services.scoring.profiles.registry import get_profile
from backend.app.services.submissions.lifecycle import get_scoring_run


def _number(value):
    return None if value is None else float(value)


def _time(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return None if value is None else str(value)


def _stable(values):
    return sorted(
        [deepcopy(value) for value in values or []],
        key=lambda value: json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
    )


def _rule_projection(value, criterion_code: str):
    return {
        "version_hash": value.get("version_hash"),
        "rule_code": value["rule_code"],
        "criterion_code": value.get("criterion_code") or criterion_code,
        "direction": value.get("direction"),
        "effect_type": value.get("effect_type"),
        "status": value.get("status"),
        "level_code": value.get("selected_level_code"),
        "calculated_effect": deepcopy(value.get("calculated_effect")),
        "evidence": _stable(value.get("evidence_refs")),
        "observations": _stable(value.get("occurrences")),
    }


def _criterion_projection(item):
    code = item.criterion_code or item.criterion_id
    rules = sorted(
        [
            _rule_projection(value, code)
            for value in (item.rule_results or [])
        ],
        key=lambda value: value["rule_code"],
    )
    return {
        "criterion_id": item.criterion_id,
        "criterion_code": code,
        "criterion_name": item.criterion_name or "",
        "max_score": _number(item.max_score),
        "ai_score": _number(item.ai_score),
        "final_score": _number(item.final_score),
        "automatic_status": item.auto_score_status or "calculated",
        "evidence_sufficient": bool(item.evidence_sufficient),
        "need_manual_review": bool(item.need_manual_review),
        "reason": item.reason or "",
        "aggregation": deepcopy(item.aggregation),
        "evidence": _stable(item.evidence),
        "rules": rules,
    }


def _review_projection(log):
    return {
        "score_item_id": log.score_item_id,
        "reviewer_id": log.reviewer_id,
        "before_score": _number(log.before_score),
        "after_score": _number(log.after_score),
        "reason": log.reason,
        "policy_hash": log.policy_hash,
        "resolution_type": log.resolution_type,
        "created_at": _time(log.created_at),
    }


def build_run_export_v2(db, run_id: str) -> dict:
    run = get_scoring_run(db, run_id)
    submission = run.submission
    document_snapshot = run.document_snapshot
    batch = submission.evaluation_batch
    profile = get_profile(
        profile_key=run.business_profile_key,
        profile_version=run.business_profile_version,
    )
    extension_builder = getattr(profile, "build_export_extensions", None)
    if not callable(extension_builder):
        raise ValueError("business profile has no export extension contract")
    extensions = extension_builder(
        submission=submission,
        document_snapshot=document_snapshot,
        run=run,
    )
    if not isinstance(extensions, Mapping):
        raise TypeError("profile export extensions must be a mapping")
    extensions = deepcopy(dict(extensions))
    # Extensions cross the API/report boundary and therefore must remain plain
    # JSON, but unlike Core hash inputs they may legitimately contain floats.
    json.dumps(extensions, ensure_ascii=False, sort_keys=True, allow_nan=False)

    review_logs = db.scalars(
        select(models.ReviewLog)
        .where(models.ReviewLog.scoring_run_id == run.id)
        .order_by(models.ReviewLog.created_at, models.ReviewLog.id)
    ).all()
    criteria = sorted(
        [_criterion_projection(item) for item in run.items],
        key=lambda value: value["criterion_code"],
    )
    return {
        "schema": "grading-core/run-export@2",
        "run": {
            "id": run.id,
            "status": run.status,
            "ai_total_score": _number(run.ai_total_score),
            "final_total_score": _number(run.final_total_score),
            "grade": run.grade,
            "need_manual_review": bool(run.need_manual_review),
            "created_at": _time(run.created_at),
        },
        "submission": {
            "id": submission.id,
            "evaluation_batch_id": submission.evaluation_batch_id,
            "document_snapshot_id": document_snapshot.id,
            "artifact": {
                "hash": submission.source_artifact_hash,
                "ref": submission.source_artifact_ref,
            },
            "file": {
                "name": submission.file_name,
                "media_type": submission.media_type,
                "byte_length": submission.byte_length,
            },
        },
        "identity": {
            "rubric_version_id": run.rubric_version_id,
            "rubric_version_hash": run.rubric_version_hash,
            "rubric_hash_scheme": run.rubric_hash_scheme,
            "rubric_snapshot_hash": run.rubric_snapshot_hash,
            "execution_plan_hash": run.execution_plan_hash,
            "plan_schema_version": run.plan_schema_version,
            "policy_hash": run.policy_hash,
            "policy_schema_version": run.policy_schema_version,
            "business_profile_key": run.business_profile_key,
            "business_profile_version": run.business_profile_version,
            "workflow_profile": run.workflow_profile,
            "source_artifact_hash": run.source_artifact_hash,
            "normalized_content_hash": run.normalized_content_hash,
            "document_snapshot_hash": run.document_snapshot_hash,
            "prompt_version": run.prompt_version,
            "runtime_identity": deepcopy(run.runtime_identity),
            "engine_version": run.engine_version,
            "model": {
                "provider": run.model_provider,
                "name": run.model_name,
                "version": run.model_version,
            },
            "rescore_generation": run.rescore_generation,
            "idempotency_key": run.idempotency_key,
        },
        "criteria": criteria,
        "review_logs": [_review_projection(log) for log in review_logs],
        "profile_extensions": extensions,
    }


__all__ = ["build_run_export_v2"]
