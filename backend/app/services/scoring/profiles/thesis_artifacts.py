"""Thesis-only artifact, evaluation and review projections.

The generic Core never imports this module.  ORM/API/report adapters may pass
legacy thesis objects in and receive detached mappings that preserve existing
public artifacts while keeping thesis fields out of Core contracts.
"""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping

from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.rules import as_float


ARTIFACT_SCHEMA_VERSION = "thesis-artifact-projection@1"
EVAL_IDENTITY_SCHEMA_VERSION = "paper-grading/thesis-eval-run-identity@1"


def _criterion_name(item):
    criterion = getattr(item, "criterion", None)
    return str(getattr(criterion, "name", None) or "")


def _criterion_code(item):
    value = getattr(item, "criterion_code", None)
    if value:
        return str(value)
    criterion = getattr(item, "criterion", None)
    return str(getattr(criterion, "code", None) or "")


def _item_projection(item):
    return {
        "id": item.id,
        "criterion_code": _criterion_code(item),
        "criterion_name": _criterion_name(item),
        "max_score": as_float(item.max_score),
        "ai_score": as_float(item.ai_score),
        "final_score": as_float(item.final_score),
        "evidence_sufficient": bool(item.evidence_sufficient),
        "reason": item.reason or "",
        "deductions": deepcopy(item.deductions or []),
        "deduction_items": deepcopy(getattr(item, "deduction_items", None) or []),
        "evidence": deepcopy(item.evidence or []),
        "band_selection": deepcopy(getattr(item, "band_selection", None)),
        "sub_results": deepcopy(getattr(item, "sub_results", None)),
        "suggestion": item.suggestion or "",
        "confidence": as_float(item.confidence),
        "need_manual_review": bool(item.need_manual_review),
        "auto_score_status": getattr(item, "auto_score_status", None),
    }


def _optional_float(value):
    return None if value is None else as_float(value)


def _review_projection(log, item_names):
    created = getattr(log, "created_at", None)
    item_id = getattr(log, "score_item_id", None)
    return {
        "score_item_id": item_id,
        "item_name": item_names.get(item_id, "") if item_id else "",
        "reviewer_id": getattr(log, "reviewer_id", None) or "",
        "before_score": _optional_float(getattr(log, "before_score", None)),
        "after_score": _optional_float(getattr(log, "after_score", None)),
        "reason": getattr(log, "reason", None) or "",
        "policy_hash": getattr(log, "policy_hash", None),
        "resolution_type": getattr(log, "resolution_type", None),
        "created_at": created.isoformat(sep=" ") if created else "",
    }


def _reviewer(review_logs):
    for log in reversed(list(review_logs)):
        reviewer = getattr(log, "reviewer_id", None)
        if reviewer:
            return reviewer
    return ""


def _review_notes(review_logs):
    logs = list(review_logs)
    overall = [
        log.reason
        for log in logs
        if getattr(log, "score_item_id", None) is None
        and getattr(log, "reason", None)
    ]
    if overall:
        return "；".join(overall)
    return "；".join(
        log.reason for log in logs if getattr(log, "reason", None)
    )


class ThesisArtifactFacade:
    """Pure projections used by thesis adapters outside generic Core."""

    profile_key = "thesis"

    def build_artifact_projection(
        self,
        *,
        run,
        review_logs,
        section_summaries=(),
        coherence_findings=None,
        format_findings=None,
    ):
        paper = run.paper
        rubric = run.rubric
        items = [_item_projection(item) for item in run.items]
        item_names = {item["id"]: item["criterion_name"] for item in items}
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "profile_key": self.profile_key,
            "paper": {
                "id": paper.id,
                "title": paper.title,
                "student_id": paper.student_id,
                "student_name": paper.student_name,
                "department": getattr(paper, "department", None),
                "major": getattr(paper, "major", None),
            },
            "rubric": {
                "id": rubric.id,
                "name": rubric.name,
                "version": rubric.version,
            },
            "scores": {
                "ai_total": as_float(run.ai_total_score),
                "final_total": as_float(run.final_total_score),
                "grade": run.grade or "",
                "need_manual_review": bool(run.need_manual_review),
                "status": run.status,
            },
            "items": items,
            "coherence_findings": deepcopy(
                run.coherence_findings or []
                if coherence_findings is None
                else coherence_findings
            ),
            "format_findings": deepcopy(
                run.format_findings or []
                if format_findings is None
                else format_findings
            ),
            "section_summaries": deepcopy(list(section_summaries or [])),
            "review_logs": [
                _review_projection(log, item_names) for log in review_logs
            ],
        }

    def build_spreadsheet_projection(self, *, batch, run, review_logs):
        paper = run.paper
        items = [_item_projection(item) for item in run.items]
        deductions = []
        for item in items:
            deductions.extend(item["deductions"])
        changed = any(item["final_score"] != item["ai_score"] for item in items)
        finished = getattr(run, "finished_at", None)
        return {
            "schema_version": "thesis-spreadsheet-projection@1",
            "profile_key": self.profile_key,
            "summary": {
                "batch_name": batch.name,
                "student_id": paper.student_id,
                "student_name": paper.student_name,
                "department": paper.department or batch.department,
                "major": paper.major or batch.major,
                "title": paper.title,
                "rubric_version": run.rubric.version,
                "ai_total": as_float(run.ai_total_score),
                "final_total": as_float(run.final_total_score),
                "grade": run.grade,
                "main_deductions": "；".join(deductions[:3]),
                "changed": "是" if changed else "否",
                "need_manual_review": "是" if run.need_manual_review else "否",
                "finished_at": finished,
                "reviewer": _reviewer(review_logs) if run.status == "reviewed" else "",
                "review_notes": _review_notes(review_logs),
                "report_link": "/api/scoring-runs/%s/report" % run.id,
            },
            "details": [
                {
                    "student_id": paper.student_id,
                    "student_name": paper.student_name,
                    "title": paper.title,
                    "criterion_name": item["criterion_name"],
                    "max_score": item["max_score"],
                    "ai_score": item["ai_score"],
                    "final_score": item["final_score"],
                    "deductions": "；".join(item["deductions"]),
                    "evidence_quotes": "；".join(
                        evidence.get("quote", "") for evidence in item["evidence"]
                    ),
                    "evidence_locations": "；".join(
                        evidence.get("location", "") for evidence in item["evidence"]
                    ),
                    "suggestion": item["suggestion"],
                    "confidence": item["confidence"],
                }
                for item in items
            ],
        }

    def build_eval_run_identity(self, *, run, require_frozen=True):
        if run.business_profile_key not in (None, self.profile_key):
            raise ValueError("evaluation run business profile does not match thesis")
        if require_frozen and run.business_profile_key != self.profile_key:
            raise ValueError("evaluation run business profile is not frozen")
        frozen = (
            run.business_profile_version is not None
            and run.prompt_version is not None
            and isinstance(run.runtime_identity, Mapping)
        )
        if not frozen and not require_frozen:
            checker_manifest = run.checker_manifest
            policy = run.policy_snapshot
            grade_scale = (
                policy.get("grade_scale") if isinstance(policy, Mapping) else None
            )
            return {
                "schema_version": "paper-grading/historical-unfrozen-eval-run-identity@1",
                "gating_eligible": False,
                "rubric_version_id": run.rubric_version_id,
                "rubric_version_hash": run.rubric_version_hash,
                "rubric_hash_scheme": run.rubric_hash_scheme,
                "rubric_snapshot_hash": run.rubric_snapshot_hash,
                "policy_hash": run.policy_hash,
                "policy_snapshot_sha256": (
                    canonical_sha256(policy) if isinstance(policy, Mapping) else None
                ),
                "grade_scale_sha256": (
                    canonical_sha256(grade_scale)
                    if isinstance(grade_scale, Mapping)
                    else None
                ),
                "execution_plan_hash": run.execution_plan_hash,
                "plan_schema_version": run.plan_schema_version,
                "checker_manifest_sha256": (
                    canonical_sha256(checker_manifest)
                    if checker_manifest is not None
                    else None
                ),
                "business_profile_key": run.business_profile_key,
                "business_profile_version": run.business_profile_version,
                "workflow_profile": run.workflow_profile,
                "prompt_version": run.prompt_version,
                "runtime_identity_sha256": None,
                "model_provider": run.model_provider,
                "model_name": run.model_name,
                "model_version": run.model_version,
                "engine_version": run.engine_version,
                "source_artifact_hash": run.source_artifact_hash,
                "normalized_content_hash": run.normalized_content_hash,
                "document_snapshot_hash": run.document_snapshot_hash,
            }
        if run.business_profile_version is None:
            raise ValueError("evaluation run profile version is not frozen")
        if run.prompt_version is None:
            raise ValueError("evaluation run prompt version is not frozen")
        runtime = run.runtime_identity
        if not isinstance(runtime, Mapping):
            raise ValueError("evaluation run runtime identity is not frozen")
        if (
            runtime.get("profile_key") != run.business_profile_key
            or runtime.get("profile_version") != run.business_profile_version
        ):
            raise ValueError("runtime profile identity does not match evaluation run")
        if runtime.get("prompt_version") != run.prompt_version:
            raise ValueError("runtime prompt identity does not match evaluation run")
        policy = run.policy_snapshot
        if not isinstance(policy, Mapping) or policy.get("policy_hash") != run.policy_hash:
            raise ValueError("evaluation run policy identity is not frozen")
        grade_scale = policy.get("grade_scale")
        if not isinstance(grade_scale, Mapping):
            raise ValueError("evaluation run grade identity is not frozen")
        checker_manifest = run.checker_manifest
        return {
            "schema_version": EVAL_IDENTITY_SCHEMA_VERSION,
            "rubric_version_id": run.rubric_version_id,
            "rubric_version_hash": run.rubric_version_hash,
            "rubric_hash_scheme": run.rubric_hash_scheme,
            "rubric_snapshot_hash": run.rubric_snapshot_hash,
            "policy_hash": run.policy_hash,
            "policy_snapshot_sha256": canonical_sha256(policy),
            "grade_scale_sha256": canonical_sha256(grade_scale),
            "execution_plan_hash": run.execution_plan_hash,
            "plan_schema_version": run.plan_schema_version,
            "checker_manifest_sha256": (
                canonical_sha256(checker_manifest)
                if checker_manifest is not None
                else None
            ),
            "business_profile_key": run.business_profile_key,
            "business_profile_version": run.business_profile_version,
            "workflow_profile": run.workflow_profile,
            "prompt_version": run.prompt_version,
            "runtime_identity_sha256": canonical_sha256(runtime),
            "model_provider": run.model_provider,
            "model_name": run.model_name,
            "model_version": run.model_version,
            "engine_version": run.engine_version,
            "source_artifact_hash": run.source_artifact_hash,
            "normalized_content_hash": run.normalized_content_hash,
            "document_snapshot_hash": run.document_snapshot_hash,
        }

    @staticmethod
    def assert_ordinary_item_override_allowed(*, item):
        if item.auto_score_status in {"invalid", "blocked"}:
            raise ValueError(
                "ordinary override cannot resolve an invalid or blocked auto score; "
                "use an authorized resolution or retry"
            )

    @staticmethod
    def assert_review_submission_allowed(*, run):
        if run.final_total_score is None:
            raise ValueError(
                "review cannot be submitted while invalid or blocked score items remain"
            )


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "EVAL_IDENTITY_SCHEMA_VERSION",
    "ThesisArtifactFacade",
]
