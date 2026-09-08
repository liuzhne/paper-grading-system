import json

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.app.api.deps import CurrentPrincipal
from backend.app.api.deps import current_principal
from backend.app.api.deps import current_user_id
from backend.app.api.deps import require_organization_role
from backend.app.db import models
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
from backend.app.schemas.submission import ManualReviewResolve
from backend.app.schemas.submission import ManualReviewTaskRead
from backend.app.schemas.submission import ManualReviewVersion
from backend.app.schemas.submission import RuleScoringTaskRead
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
from backend.app.services.submissions.review_tasks import claim_manual_task
from backend.app.services.submissions.review_tasks import get_manual_task
from backend.app.services.submissions.review_tasks import list_manual_tasks
from backend.app.services.submissions.review_tasks import list_rule_tasks
from backend.app.services.submissions.review_tasks import manual_task_projection
from backend.app.services.submissions.review_tasks import release_manual_task
from backend.app.services.submissions.review_tasks import resolve_manual_task
from backend.app.services.submissions.review_tasks import rule_task_projection


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


def _visible_evaluation_batch(
    db: Session,
    batch_id: str,
    principal: CurrentPrincipal,
) -> models.EvaluationBatch:
    batch = db.get(models.EvaluationBatch, batch_id)
    if batch is None or (
        principal.organization_id is not None
        and batch.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="evaluation batch not found")
    return batch


def _visible_submission(
    db: Session,
    submission_id: str,
    principal: CurrentPrincipal,
):
    try:
        submission = get_submission(db, submission_id)
    except (LookupError, ValueError) as exc:
        _raise_http(exc)
    if (
        principal.organization_id is not None
        and submission.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="submission not found")
    return submission


def _visible_scoring_run(
    db: Session,
    run_id: str,
    principal: CurrentPrincipal,
):
    try:
        run = get_scoring_run(db, run_id)
    except (LookupError, ValueError) as exc:
        _raise_http(exc)
    if (
        principal.organization_id is not None
        and run.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="v2 scoring run not found")
    return run


def _visible_score_item(
    db: Session,
    item_id: str,
    principal: CurrentPrincipal,
):
    item = db.get(models.ScoreItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="score item not found")
    _visible_scoring_run(db, item.scoring_run_id, principal)
    return item


@router.post(
    "/evaluation-batches",
    response_model=EvaluationBatchRead,
    status_code=201,
)
def post_evaluation_batch(
    payload: EvaluationBatchCreate,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    version = db.get(models.RubricVersion, payload.rubric_version_id)
    rubric = None if version is None else db.get(models.Rubric, version.rubric_id)
    if rubric is None or (
        principal.organization_id is not None
        and rubric.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="rubric version not found")
    require_organization_role(principal, "org_admin", "teacher")
    try:
        batch = create_evaluation_batch(
            db,
            payload,
            creator_id=principal.user_id,
            organization_id=principal.organization_id,
        )
    except (LookupError, ValueError) as exc:
        _raise_http(exc)
    return evaluation_batch_projection(batch)


@router.post("/submissions", response_model=SubmissionRead, status_code=201)
async def post_submission(
    evaluation_batch_id: str = Form(...),
    metadata_json: str = Form("{}"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
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
        _visible_evaluation_batch(db, evaluation_batch_id, principal)
        require_organization_role(principal, "org_admin", "teacher")
        submission, _snapshot = ingest_submission(
            db,
            evaluation_batch_id=evaluation_batch_id,
            file_name=file.filename or "submission",
            uploaded_media_type=file.content_type,
            raw_bytes=await file.read(),
            metadata=metadata,
            creator_id=principal.user_id,
        )
    except (LookupError, ValueError) as exc:
        _raise_http(exc)
    return submission_projection(submission)


@router.get("/submissions/{submission_id}", response_model=SubmissionRead)
def read_submission(
    submission_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    return submission_projection(_visible_submission(db, submission_id, principal))


@router.get(
    "/submissions/{submission_id}/document-snapshot",
    response_model=DocumentSnapshotSummary,
)
def read_document_snapshot(
    submission_id: str,
    document_snapshot_id: str | None = None,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    try:
        _visible_submission(db, submission_id, principal)
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
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    try:
        _visible_submission(db, submission_id, principal)
        require_organization_role(principal, "org_admin", "teacher")
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
def read_scoring_run(
    run_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    return scoring_run_projection(_visible_scoring_run(db, run_id, principal))


@router.get(
    "/scoring-runs/{run_id}/rule-tasks",
    response_model=list[RuleScoringTaskRead],
)
def read_rule_tasks(
    run_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _visible_scoring_run(db, run_id, principal)
    return [
        rule_task_projection(task)
        for task in list_rule_tasks(
            db,
            run_id=run_id,
            organization_id=principal.organization_id,
        )
    ]


@router.get(
    "/manual-review-tasks",
    response_model=list[ManualReviewTaskRead],
)
def read_manual_review_tasks(
    status: str | None = None,
    batch_id: str | None = None,
    scoring_run_id: str | None = None,
    limit: int | None = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """按批次 / run 范围过滤并分页（计划 §6）。

    复核队列按批次工作；只能按 status 过滤时，取一个批次的阻塞任务要把全组织的
    任务都拉回来再在客户端筛——加上分页之后，那种做法会把「这一页里没有该批次」
    显示成「该批次没有阻塞任务」。
    """
    require_organization_role(principal, "org_admin", "teacher")
    return [
        manual_task_projection(task)
        for task in list_manual_tasks(
            db,
            organization_id=principal.organization_id,
            status=status,
            batch_id=batch_id,
            scoring_run_id=scoring_run_id,
            limit=limit,
            offset=offset,
        )
    ]


@router.get(
    "/manual-review-tasks/{task_id}",
    response_model=ManualReviewTaskRead,
)
def read_manual_review_task(
    task_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    require_organization_role(principal, "org_admin", "teacher")
    try:
        task = get_manual_task(
            db,
            task_id=task_id,
            organization_id=principal.organization_id,
        )
    except (LookupError, ValueError) as exc:
        _raise_http(exc)
    return manual_task_projection(task)


@router.post(
    "/manual-review-tasks/{task_id}/claim",
    response_model=ManualReviewTaskRead,
)
def post_claim_manual_review_task(
    task_id: str,
    payload: ManualReviewVersion,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
    user_id: str = Depends(current_user_id),
):
    require_organization_role(principal, "org_admin", "teacher")
    try:
        task = claim_manual_task(
            db,
            task_id=task_id,
            organization_id=principal.organization_id,
            reviewer_id=user_id,
            version=payload.version,
        )
    except (LookupError, ValueError) as exc:
        _raise_http(exc)
    return manual_task_projection(task)


@router.post(
    "/manual-review-tasks/{task_id}/release",
    response_model=ManualReviewTaskRead,
)
def post_release_manual_review_task(
    task_id: str,
    payload: ManualReviewVersion,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
    user_id: str = Depends(current_user_id),
):
    require_organization_role(principal, "org_admin", "teacher")
    try:
        task = release_manual_task(
            db,
            task_id=task_id,
            organization_id=principal.organization_id,
            reviewer_id=user_id,
            version=payload.version,
        )
    except (LookupError, ValueError) as exc:
        _raise_http(exc)
    return manual_task_projection(task)


@router.post(
    "/manual-review-tasks/{task_id}/resolve",
    response_model=ManualReviewTaskRead,
)
def post_resolve_manual_review_task(
    task_id: str,
    payload: ManualReviewResolve,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
    user_id: str = Depends(current_user_id),
):
    require_organization_role(principal, "org_admin", "teacher")
    try:
        task = resolve_manual_task(
            db,
            task_id=task_id,
            organization_id=principal.organization_id,
            reviewer_id=user_id,
            version=payload.version,
            final_score=payload.final_score,
            reason=payload.reason,
            evidence=payload.evidence,
        )
    except (LookupError, ValueError) as exc:
        _raise_http(exc)
    return manual_task_projection(task)


@router.patch(
    "/score-items/{item_id}",
    response_model=V2ScoreItemRead,
)
def patch_score_item(
    item_id: str,
    payload: GenericScoreItemReview,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
    user_id: str = Depends(current_user_id),
):
    ensure_dev_user(db)
    try:
        _visible_score_item(db, item_id, principal)
        require_organization_role(principal, "org_admin", "teacher")
        item = update_generic_score_item(
            db,
            item_id=item_id,
            final_score=payload.final_score,
            reason=payload.reason,
            resolution_type=payload.resolution_type,
            reviewer_id=user_id,
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
    principal: CurrentPrincipal = Depends(current_principal),
    user_id: str = Depends(current_user_id),
):
    ensure_dev_user(db)
    try:
        _visible_scoring_run(db, run_id, principal)
        require_organization_role(principal, "org_admin", "teacher")
        run = submit_generic_review(
            db,
            run_id=run_id,
            reason=payload.reason,
            reviewer_id=user_id,
        )
        run = get_scoring_run(db, run.id)
    except (LookupError, ValueError) as exc:
        _raise_http(exc)
    return scoring_run_projection(run)


@router.get(
    "/scoring-runs/{run_id}/review-logs",
    response_model=list[V2ReviewLogRead],
)
def read_review_logs(
    run_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    try:
        _visible_scoring_run(db, run_id, principal)
        return [
            review_log_projection(log) for log in review_logs(db, run_id)
        ]
    except (LookupError, ValueError) as exc:
        _raise_http(exc)


@router.get("/scoring-runs/{run_id}/export.json")
def export_scoring_run_json(
    run_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    try:
        _visible_scoring_run(db, run_id, principal)
        return build_run_export_v2(db, run_id)
    except (LookupError, TypeError, ValueError) as exc:
        _raise_http(exc)


@router.get("/scoring-runs/{run_id}/report")
def export_scoring_run_report(
    run_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    try:
        _visible_scoring_run(db, run_id, principal)
        path = generate_report_v2(db, run_id)
    except (LookupError, TypeError, ValueError) as exc:
        _raise_http(exc)
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        filename=path.name,
    )


@router.get("/scoring-runs/{run_id}/export.xlsx")
def export_scoring_run_excel(
    run_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    try:
        _visible_scoring_run(db, run_id, principal)
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
