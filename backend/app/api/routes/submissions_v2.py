import json

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.db.session import get_db
from backend.app.schemas.submission import DocumentSnapshotSummary
from backend.app.schemas.submission import EvaluationBatchCreate
from backend.app.schemas.submission import EvaluationBatchRead
from backend.app.schemas.submission import GenericReviewSubmit
from backend.app.schemas.submission import GenericScoreItemReview
from backend.app.schemas.submission import ScoreSubmissionRequest
from backend.app.schemas.submission import SubmissionRead
from backend.app.schemas.submission import V2ReviewLogRead
from backend.app.schemas.submission import V2ScoreItemRead
from backend.app.schemas.submission import V2ScoringRunRead
from backend.app.services.dev_user import ensure_dev_user
from backend.app.services.llm.base import LLMScoringError
from backend.app.services.report.generic_export import build_run_export_v2
from backend.app.services.report.generic_generator import generate_report_v2
from backend.app.services.spreadsheet.generic import export_run_excel_v2
from backend.app.services.submissions.lifecycle import ResourceConflictError
from backend.app.services.submissions.lifecycle import ResourceNotFoundError
from backend.app.services.submissions.lifecycle import SubmissionProcessingError
from backend.app.services.submissions.lifecycle import create_evaluation_batch
from backend.app.services.submissions.lifecycle import evaluation_batch_projection
from backend.app.services.submissions.lifecycle import get_document_snapshot
from backend.app.services.submissions.lifecycle import get_scoring_run
from backend.app.services.submissions.lifecycle import get_submission
from backend.app.services.submissions.lifecycle import ingest_submission
from backend.app.services.submissions.lifecycle import score_generic_submission
from backend.app.services.submissions.lifecycle import score_item_projection
from backend.app.services.submissions.lifecycle import scoring_run_projection
from backend.app.services.submissions.lifecycle import snapshot_summary
from backend.app.services.submissions.lifecycle import submission_projection
from backend.app.services.submissions.review import review_log_projection
from backend.app.services.submissions.review import review_logs
from backend.app.services.submissions.review import submit_generic_review
from backend.app.services.submissions.review import update_generic_score_item


router = APIRouter(prefix="/v2", tags=["v2-submissions"])


def _raise_http(exc):
    if isinstance(exc, ResourceNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ResourceConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, SubmissionProcessingError):
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "submission_id": exc.submission_id,
            },
        ) from exc
    if isinstance(exc, LLMScoringError):
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/evaluation-batches",
    response_model=EvaluationBatchRead,
    status_code=201,
)
def post_evaluation_batch(
    payload: EvaluationBatchCreate,
    db: Session = Depends(get_db),
):
    user = ensure_dev_user(db)
    try:
        batch = create_evaluation_batch(db, payload, creator_id=user.id)
    except (LookupError, ValueError) as exc:
        _raise_http(exc)
    return evaluation_batch_projection(batch)


@router.post("/submissions", response_model=SubmissionRead, status_code=201)
async def post_submission(
    evaluation_batch_id: str = Form(...),
    metadata_json: str = Form("{}"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = ensure_dev_user(db)
    try:
        metadata = json.loads(metadata_json)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="metadata_json must be a JSON object",
        ) from exc
    if not isinstance(metadata, dict):
        raise HTTPException(
            status_code=422,
            detail="metadata_json must be a JSON object",
        )
    try:
        submission, _snapshot = ingest_submission(
            db,
            evaluation_batch_id=evaluation_batch_id,
            file_name=file.filename or "submission",
            uploaded_media_type=file.content_type,
            raw_bytes=await file.read(),
            metadata=metadata,
            creator_id=user.id,
        )
    except (LookupError, ValueError) as exc:
        _raise_http(exc)
    return submission_projection(submission)


@router.get("/submissions/{submission_id}", response_model=SubmissionRead)
def read_submission(submission_id: str, db: Session = Depends(get_db)):
    try:
        return submission_projection(get_submission(db, submission_id))
    except (LookupError, ValueError) as exc:
        _raise_http(exc)


@router.get(
    "/submissions/{submission_id}/document-snapshot",
    response_model=DocumentSnapshotSummary,
)
def read_document_snapshot(
    submission_id: str,
    document_snapshot_id: str | None = None,
    db: Session = Depends(get_db),
):
    try:
        snapshot = get_document_snapshot(
            db,
            submission_id,
            snapshot_id=document_snapshot_id,
        )
    except (LookupError, ValueError) as exc:
        _raise_http(exc)
    return snapshot_summary(snapshot)


@router.post(
    "/submissions/{submission_id}/score",
    response_model=V2ScoringRunRead,
)
def post_submission_score(
    submission_id: str,
    payload: ScoreSubmissionRequest,
    db: Session = Depends(get_db),
):
    ensure_dev_user(db)
    try:
        run = score_generic_submission(
            db,
            submission_id,
            rescore_generation=payload.rescore_generation,
            document_snapshot_id=payload.document_snapshot_id,
        )
        run = get_scoring_run(db, run.id)
    except (LLMScoringError, LookupError, ValueError) as exc:
        _raise_http(exc)
    return scoring_run_projection(run)


@router.get(
    "/scoring-runs/{run_id}",
    response_model=V2ScoringRunRead,
)
def read_scoring_run(run_id: str, db: Session = Depends(get_db)):
    try:
        return scoring_run_projection(get_scoring_run(db, run_id))
    except (LookupError, ValueError) as exc:
        _raise_http(exc)


@router.patch(
    "/score-items/{item_id}",
    response_model=V2ScoreItemRead,
)
def patch_score_item(
    item_id: str,
    payload: GenericScoreItemReview,
    db: Session = Depends(get_db),
):
    ensure_dev_user(db)
    try:
        item = update_generic_score_item(
            db,
            item_id=item_id,
            final_score=payload.final_score,
            reason=payload.reason,
            resolution_type=payload.resolution_type,
            reviewer_id=settings.DEFAULT_DEV_USER_ID,
        )
    except (LookupError, ValueError) as exc:
        _raise_http(exc)
    return score_item_projection(item)


@router.post(
    "/scoring-runs/{run_id}/review",
    response_model=V2ScoringRunRead,
)
def post_scoring_review(
    run_id: str,
    payload: GenericReviewSubmit,
    db: Session = Depends(get_db),
):
    ensure_dev_user(db)
    try:
        run = submit_generic_review(
            db,
            run_id=run_id,
            reason=payload.reason,
            reviewer_id=settings.DEFAULT_DEV_USER_ID,
        )
        run = get_scoring_run(db, run.id)
    except (LookupError, ValueError) as exc:
        _raise_http(exc)
    return scoring_run_projection(run)


@router.get(
    "/scoring-runs/{run_id}/review-logs",
    response_model=list[V2ReviewLogRead],
)
def read_review_logs(run_id: str, db: Session = Depends(get_db)):
    try:
        return [
            review_log_projection(log) for log in review_logs(db, run_id)
        ]
    except (LookupError, ValueError) as exc:
        _raise_http(exc)


@router.get("/scoring-runs/{run_id}/export.json")
def export_scoring_run_json(run_id: str, db: Session = Depends(get_db)):
    try:
        return build_run_export_v2(db, run_id)
    except (LookupError, TypeError, ValueError) as exc:
        _raise_http(exc)


@router.get("/scoring-runs/{run_id}/report")
def export_scoring_run_report(run_id: str, db: Session = Depends(get_db)):
    try:
        path = generate_report_v2(db, run_id)
    except (LookupError, TypeError, ValueError) as exc:
        _raise_http(exc)
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        filename=path.name,
    )


@router.get("/scoring-runs/{run_id}/export.xlsx")
def export_scoring_run_excel(run_id: str, db: Session = Depends(get_db)):
    try:
        path = export_run_excel_v2(db, run_id)
    except (LookupError, TypeError, ValueError) as exc:
        _raise_http(exc)
    return FileResponse(
        path,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        filename=path.name,
    )


__all__ = ["router"]
