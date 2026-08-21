"""Profile-neutral v2 submission ingestion and Core scoring lifecycle."""

from __future__ import annotations

from copy import deepcopy
from io import BytesIO
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.core.config import settings
from backend.app.db import models
from backend.app.services.document_parser.extractor import extract_document
from backend.app.services.llm.factory import get_llm_scorer
from backend.app.services.scoring.adapters.persistence import (
    CoreRunPersistence,
    LocalDocumentSnapshotStore,
)
from backend.app.services.scoring.adapters.rubric_snapshot import (
    CompiledRubricSnapshotLoader,
)
from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.contracts import (
    DocumentSnapshot as CoreDocumentSnapshot,
    ScoringRequest,
    SubmissionSnapshot,
)
from backend.app.services.scoring.core.engine import score_submission
from backend.app.services.scoring.core.execution_plan import RuleExecutionPlanBuilder
from backend.app.services.scoring.core.identity import hash_source_artifact
from backend.app.services.scoring.core.identity import (
    scoring_request_idempotency_projection,
)
from backend.app.services.scoring.profiles.registry import get_profile
from backend.app.services.storage.local import ensure_storage_dirs
from backend.app.services.storage.local import materialize
from backend.app.services.storage.local import safe_filename
from backend.app.services.storage.local import store_binary


class ResourceNotFoundError(ValueError):
    pass


class ResourceConflictError(ValueError):
    pass


class SubmissionProcessingError(ValueError):
    def __init__(self, submission_id: str, message: str):
        super().__init__(message)
        self.submission_id = submission_id


def _published_version(db, rubric_version_id: str):
    version = db.get(models.RubricVersion, rubric_version_id)
    if version is None:
        raise ResourceNotFoundError("rubric version not found")
    rubric = db.get(models.Rubric, version.rubric_id)
    compilation = db.get(models.RubricCompilation, version.compilation_id)
    if not (
        rubric is not None
        and rubric.status == "published"
        and rubric.published_at is not None
        and compilation is not None
        and compilation.status == "validated"
        and compilation.published_at == rubric.published_at
        and compilation.final_version_hash == version.version_hash
    ):
        raise ResourceConflictError(
            "rubric version is not a consistently published immutable version"
        )
    return rubric, version


def create_evaluation_batch(db, payload, *, creator_id: str):
    rubric, version = _published_version(db, payload.rubric_version_id)
    if version.business_profile_key != payload.business_profile_key:
        raise ResourceConflictError("rubric version business profile key mismatch")
    try:
        get_profile(
            profile_key=payload.business_profile_key,
            profile_version=payload.business_profile_version,
        )
    except LookupError as exc:
        raise ResourceConflictError(str(exc)) from exc
    except ValueError as exc:
        raise ResourceConflictError(str(exc)) from exc
    batch = models.EvaluationBatch(
        owner_id=creator_id,
        name=payload.name.strip(),
        rubric_id=rubric.id,
        rubric_version_id=version.id,
        business_profile_key=payload.business_profile_key,
        business_profile_version=payload.business_profile_version,
        status="active",
        created_by=creator_id,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def evaluation_batch_projection(batch) -> dict:
    return {
        "id": batch.id,
        "name": batch.name,
        "rubric_id": batch.rubric_id,
        "rubric_version_id": batch.rubric_version_id,
        "business_profile_key": batch.business_profile_key,
        "business_profile_version": batch.business_profile_version,
        "status": batch.status,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
    }


def _media_type(suffix: str) -> str:
    if suffix == ".docx":
        return (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        )
    if suffix == ".pdf":
        return "application/pdf"
    raise ValueError("only .docx and text PDF files are supported")


def ingest_submission(
    db,
    *,
    evaluation_batch_id: str,
    file_name: str,
    uploaded_media_type: str | None,
    raw_bytes: bytes,
    metadata: dict,
    creator_id: str,
):
    del uploaded_media_type
    batch = db.get(models.EvaluationBatch, evaluation_batch_id)
    if batch is None:
        raise ResourceNotFoundError("evaluation batch not found")
    if batch.status != "active":
        raise ResourceConflictError("evaluation batch is not active")
    try:
        profile = get_profile(
            profile_key=batch.business_profile_key,
            profile_version=batch.business_profile_version,
        )
    except (LookupError, ValueError) as exc:
        raise ResourceConflictError(str(exc)) from exc
    suffix = Path(file_name or "").suffix.lower()
    media_type = _media_type(suffix)
    if not isinstance(raw_bytes, bytes) or not raw_bytes:
        raise ValueError("uploaded document is empty")
    maximum = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if len(raw_bytes) > maximum:
        raise ValueError("uploaded document exceeds the configured size limit")
    if not isinstance(metadata, dict):
        raise ValueError("submission metadata must be an object")

    artifact_hash = hash_source_artifact(raw_bytes)
    submission = models.Submission(
        evaluation_batch_id=batch.id,
        source_artifact_hash=artifact_hash,
        source_artifact_ref="blob:sha256:" + artifact_hash,
        file_name=safe_filename(file_name or ("submission" + suffix)),
        media_type=media_type,
        byte_length=len(raw_bytes),
        submission_metadata=deepcopy(metadata),
        status="parsing",
        created_by=creator_id,
    )
    db.add(submission)
    db.flush()

    ensure_storage_dirs()
    try:
        artifact_ref = store_binary(
            BytesIO(raw_bytes),
            "uploads/sha256",
            artifact_hash + suffix,
            content_type=media_type,
        )
        extracted = extract_document(materialize(artifact_ref))
        interpreter = getattr(profile, "interpret_document", None)
        if not callable(interpreter):
            raise ValueError("business profile has no document interpreter")
        interpreted = interpreter(
            extracted_document=extracted,
            submission=submission,
        )
        document = CoreDocumentSnapshot.from_mapping(
            interpreted.to_mapping()
            if callable(getattr(interpreted, "to_mapping", None))
            else interpreted
        ).to_mapping()
        if (
            document["profile_key"] != batch.business_profile_key
            or document["profile_version"] != batch.business_profile_version
        ):
            raise ValueError(
                "interpreted document business profile identity mismatch"
            )
        snapshot_store = LocalDocumentSnapshotStore()
        snapshot_ref = snapshot_store.put(document)
        snapshot = models.DocumentSnapshot(
            submission_id=submission.id,
            schema_version=document["schema_version"],
            business_profile_key=document["profile_key"],
            business_profile_version=document["profile_version"],
            parser_version=document["parser_version"],
            normalizer_version=document["normalizer_version"],
            content_hash=document["content_hash"],
            snapshot_hash=document["document_snapshot_hash"],
            snapshot_ref=snapshot_ref,
            snapshot_payload=deepcopy(document),
        )
        db.add(snapshot)
        submission.status = "parsed"
        db.commit()
        db.refresh(submission)
        db.refresh(snapshot)
        return submission, snapshot
    except Exception as exc:
        submission.status = "failed"
        submission.error_message = " ".join(str(exc).split())[:1000]
        db.commit()
        raise SubmissionProcessingError(
            submission.id,
            submission.error_message or "submission parsing failed",
        ) from exc


def _latest_snapshot(submission):
    snapshots = sorted(
        submission.document_snapshots,
        key=lambda item: (item.created_at, item.id),
    )
    return snapshots[-1] if snapshots else None


def submission_projection(submission) -> dict:
    batch = submission.evaluation_batch
    snapshot = _latest_snapshot(submission)
    return {
        "id": submission.id,
        "evaluation_batch_id": submission.evaluation_batch_id,
        "business_profile_key": batch.business_profile_key,
        "business_profile_version": batch.business_profile_version,
        "source_artifact_hash": submission.source_artifact_hash,
        "source_artifact_ref": submission.source_artifact_ref,
        "file_name": submission.file_name,
        "media_type": submission.media_type,
        "byte_length": submission.byte_length,
        "metadata": deepcopy(submission.submission_metadata),
        "status": submission.status,
        "error_message": submission.error_message,
        "document_snapshot_id": None if snapshot is None else snapshot.id,
        "created_at": submission.created_at,
        "updated_at": submission.updated_at,
    }


def snapshot_summary(snapshot) -> dict:
    payload = snapshot.snapshot_payload
    return {
        "id": snapshot.id,
        "submission_id": snapshot.submission_id,
        "schema_version": snapshot.schema_version,
        "business_profile_key": snapshot.business_profile_key,
        "business_profile_version": snapshot.business_profile_version,
        "parser_version": snapshot.parser_version,
        "normalizer_version": snapshot.normalizer_version,
        "content_hash": snapshot.content_hash,
        "snapshot_hash": snapshot.snapshot_hash,
        "snapshot_ref": snapshot.snapshot_ref,
        "section_count": len(payload.get("sections", ()) or ()),
        "evidence_unit_count": len(payload.get("evidence_units", ()) or ()),
        "parse_quality": payload.get("parse_quality"),
        "created_at": snapshot.created_at,
    }


def get_submission(db, submission_id: str):
    submission = db.scalar(
        select(models.Submission)
        .where(models.Submission.id == submission_id)
        .options(
            selectinload(models.Submission.evaluation_batch),
            selectinload(models.Submission.document_snapshots),
        )
    )
    if submission is None:
        raise ResourceNotFoundError("submission not found")
    return submission


def get_document_snapshot(db, submission_id: str, snapshot_id: str | None = None):
    submission = get_submission(db, submission_id)
    if snapshot_id is None:
        snapshot = _latest_snapshot(submission)
    else:
        snapshot = next(
            (
                item
                for item in submission.document_snapshots
                if item.id == snapshot_id
            ),
            None,
        )
    if snapshot is None:
        raise ResourceNotFoundError("document snapshot not found")
    return snapshot


def _submission_snapshot(submission):
    return SubmissionSnapshot.from_mapping(
        {
            "schema_version": "submission-snapshot@1",
            "submission_id": submission.id,
            "profile_key": submission.evaluation_batch.business_profile_key,
            "source_artifact_hash": submission.source_artifact_hash,
            "metadata": deepcopy(submission.submission_metadata),
            "artifact_refs": [
                {
                    "kind": "source",
                    "ref": submission.source_artifact_ref,
                    "content_hash": submission.source_artifact_hash,
                }
            ],
        }
    )


def score_generic_submission(
    db,
    submission_id: str,
    *,
    rescore_generation: int,
    document_snapshot_id: str | None = None,
    scorer=None,
):
    submission = get_submission(db, submission_id)
    if submission.status == "failed":
        raise ResourceConflictError(
            "submission parse failed: "
            + (submission.error_message or "unknown error")
        )
    if submission.status not in {
        "parsed",
        "scored",
        "pending_review",
        "reviewed",
    }:
        raise ResourceConflictError("submission is not ready for scoring")
    snapshot = get_document_snapshot(
        db,
        submission.id,
        snapshot_id=document_snapshot_id,
    )
    batch = submission.evaluation_batch
    try:
        profile = get_profile(
            profile_key=batch.business_profile_key,
            profile_version=batch.business_profile_version,
        )
    except (LookupError, ValueError) as exc:
        raise ResourceConflictError(str(exc)) from exc
    registry = profile.build_checker_registry()
    rubric_snapshot = CompiledRubricSnapshotLoader().load_from_session(
        session=db,
        rubric_version_id=batch.rubric_version_id,
        expected_profile_key=batch.business_profile_key,
    )
    document = CoreDocumentSnapshot.from_mapping(
        deepcopy(snapshot.snapshot_payload)
    )
    plan = RuleExecutionPlanBuilder(
        checker_registry=registry,
        policy_compiler_version="scoring-policy-compiler@1",
        engine_contract_version="scoring-core@1",
    ).build(
        rubric=rubric_snapshot,
        profile=profile,
        document_schema_version=document.schema_version,
    )

    owns_scorer = scorer is None
    scorer = scorer or get_llm_scorer()
    try:
        request_mapping = {
            "schema_version": "scoring-request@2",
            "submission": _submission_snapshot(submission).to_mapping(),
            "document": document.to_mapping(),
            "plan": plan.to_mapping(),
            "runtime_identity": profile.build_runtime_identity(scorer),
            "rescore_generation": rescore_generation,
        }
        request_mapping["idempotency_key"] = canonical_sha256(
            scoring_request_idempotency_projection(request_mapping)
        )
        request = ScoringRequest.from_mapping(request_mapping)
        existing = db.scalar(
            select(models.ScoringRun).where(
                models.ScoringRun.idempotency_key == request.idempotency_key
            )
        )
        if existing is not None:
            if (
                existing.submission_id != submission.id
                or existing.document_snapshot_id != snapshot.id
            ):
                raise ResourceConflictError(
                    "idempotency key is occupied by a different scoring target"
                )
            return existing
        outcome = score_submission(
            request=request,
            checker_registry=registry,
            llm_runtime=profile.build_llm_runtime(scorer),
            profile=profile,
        )
        rubric = db.get(models.Rubric, batch.rubric_id)
        return CoreRunPersistence(
            db,
            document_snapshot_store=LocalDocumentSnapshotStore(),
        ).persist(
            request=request,
            outcome=outcome,
            paper_id=None,
            submission_id=submission.id,
            document_snapshot_id=snapshot.id,
            rubric_id=rubric.id,
            criterion_id_by_code={
                criterion.code: criterion.id for criterion in rubric.criteria
            },
            workflow_profile=batch.rubric_version.workflow_profile,
            document_snapshot_ref=snapshot.snapshot_ref,
        )
    finally:
        if owns_scorer:
            close = getattr(scorer, "close", None)
            if callable(close):
                close()


def _number(value):
    return None if value is None else float(value)


def scoring_run_projection(run) -> dict:
    items = sorted(
        run.items,
        key=lambda item: (item.criterion_code or "", item.id),
    )
    return {
        "schema_version": "run-read@2",
        "id": run.id,
        "paper_id": run.paper_id,
        "submission_id": run.submission_id,
        "document_snapshot_id": run.document_snapshot_id,
        "rubric_id": run.rubric_id,
        "rubric_version_id": run.rubric_version_id,
        "rubric_version_hash": run.rubric_version_hash,
        "rubric_hash_scheme": run.rubric_hash_scheme,
        "rubric_snapshot_hash": run.rubric_snapshot_hash,
        "business_profile_key": run.business_profile_key,
        "business_profile_version": run.business_profile_version,
        "workflow_profile": run.workflow_profile,
        "policy_hash": run.policy_hash,
        "policy_schema_version": run.policy_schema_version,
        "execution_plan_hash": run.execution_plan_hash,
        "plan_schema_version": run.plan_schema_version,
        "source_artifact_hash": run.source_artifact_hash,
        "normalized_content_hash": run.normalized_content_hash,
        "document_snapshot_hash": run.document_snapshot_hash,
        "prompt_version": run.prompt_version,
        "runtime_identity": deepcopy(run.runtime_identity),
        "engine_version": run.engine_version,
        "model_provider": run.model_provider,
        "model_name": run.model_name,
        "model_version": run.model_version,
        "rescore_generation": run.rescore_generation,
        "idempotency_key": run.idempotency_key,
        "status": run.status,
        "ai_total_score": _number(run.ai_total_score),
        "final_total_score": _number(run.final_total_score),
        "grade": run.grade,
        "need_manual_review": run.need_manual_review,
        "items": [score_item_projection(item) for item in items],
        "created_at": run.created_at,
    }


def score_item_projection(item) -> dict:
    return {
        "id": item.id,
        "criterion_id": item.criterion_id,
        "criterion_code": item.criterion_code,
        "criterion_name": item.criterion_name,
        "max_score": _number(item.max_score),
        "ai_score": _number(item.ai_score),
        "final_score": _number(item.final_score),
        "evidence_sufficient": item.evidence_sufficient,
        "evidence": deepcopy(item.evidence or []),
        "reason": item.reason,
        "need_manual_review": item.need_manual_review,
        "auto_score_status": item.auto_score_status,
        "aggregation": deepcopy(item.aggregation),
        "rule_results": deepcopy(item.rule_results or []),
        "rule_results_schema_version": item.rule_results_schema_version,
    }


def get_scoring_run(db, run_id: str):
    run = db.scalar(
        select(models.ScoringRun)
        .where(models.ScoringRun.id == run_id)
        .options(
            selectinload(models.ScoringRun.items).selectinload(
                models.ScoreItem.criterion
            ),
            selectinload(models.ScoringRun.submission),
            selectinload(models.ScoringRun.document_snapshot),
        )
    )
    if run is None or run.submission_id is None:
        raise ResourceNotFoundError("v2 scoring run not found")
    return run


__all__ = [
    "ResourceConflictError",
    "ResourceNotFoundError",
    "SubmissionProcessingError",
    "create_evaluation_batch",
    "evaluation_batch_projection",
    "get_document_snapshot",
    "get_scoring_run",
    "get_submission",
    "ingest_submission",
    "score_generic_submission",
    "score_item_projection",
    "scoring_run_projection",
    "snapshot_summary",
    "submission_projection",
]
