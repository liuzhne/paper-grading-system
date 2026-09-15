import os
import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Response
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from backend.app.api.deps import CurrentPrincipal
from backend.app.api.deps import current_principal
from backend.app.api.deps import current_user_id
from backend.app.api.deps import require_organization_role
from backend.app.db.models import GradingBatch
from backend.app.db.session import get_db
from backend.app.schemas.batch_job import BatchScoringJobCreate
from backend.app.schemas.batch_job import BatchScoringJobRead
from backend.app.services.batch_scoring.jobs import cancel_batch_scoring_job
from backend.app.services.batch_scoring.jobs import create_batch_scoring_job
from backend.app.services.batch_scoring.jobs import get_batch_scoring_job
from backend.app.services.batch_scoring.jobs import get_latest_batch_scoring_job
from backend.app.services.batch_scoring.jobs import list_attention_batch_scoring_jobs
from backend.app.services.batch_scoring.jobs import retry_batch_scoring_job
from backend.app.services.batch_scoring.jobs import run_batch_scoring_job
from backend.app.services.batch_scoring.vercel_queue import dispatch_batch_scoring_job
from backend.app.services.dev_user import ensure_dev_user


router = APIRouter(tags=["batch-scoring-jobs"])
logger = logging.getLogger("batch-scoring-jobs")


async def _dispatch_or_503(job):
    try:
        await dispatch_batch_scoring_job(job)
    except Exception as exc:
        logger.exception("batch_scoring_dispatch_failed job_id=%s", job.id)
        raise HTTPException(
            status_code=503,
            detail="后台评分任务暂未进入执行队列，请稍后重新开始。",
        ) from exc


def _batch_or_404(
    db: Session,
    batch_id: str,
    principal: CurrentPrincipal,
) -> GradingBatch:
    batch = db.get(GradingBatch, batch_id)
    if batch is None or (
        principal.organization_id is not None
        and batch.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="batch not found")
    return batch


def _job_or_404(db, job_id, principal: CurrentPrincipal):
    job = get_batch_scoring_job(db, job_id)
    if job is None or (
        principal.organization_id is not None
        and job.batch.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="batch scoring job not found")
    return job


@router.get(
    "/batch-scoring-jobs",
    response_model=list[BatchScoringJobRead],
)
def list_attention_jobs(
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    return list_attention_batch_scoring_jobs(
        db, organization_id=principal.organization_id
    )


@router.post(
    "/batches/{batch_id}/score-jobs",
    response_model=BatchScoringJobRead,
    status_code=201,
)
async def create_job(
    batch_id: str,
    payload: BatchScoringJobCreate,
    response: Response,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    try:
        _batch_or_404(db, batch_id, principal)
        require_organization_role(principal, "org_admin", "teacher")
        job, created = create_batch_scoring_job(
            db,
            batch_id=batch_id,
            rescore=payload.rescore,
            max_workers=payload.max_workers,
            observation_policy=payload.observation_policy,
            actor_id=user_id,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 404 if detail == "batch not found" else 409 if "active" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    await _dispatch_or_503(job)
    response.status_code = 201 if created else 200
    return job


@router.get(
    "/batches/{batch_id}/score-jobs/latest",
    response_model=BatchScoringJobRead,
)
def latest_job(
    batch_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _batch_or_404(db, batch_id, principal)
    job = get_latest_batch_scoring_job(db, batch_id)
    if job is None:
        raise HTTPException(status_code=404, detail="batch scoring job not found")
    return job


@router.get(
    "/batch-scoring-jobs/{job_id}",
    response_model=BatchScoringJobRead,
)
def read_job(
    job_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    return _job_or_404(db, job_id, principal)


@router.post(
    "/batch-scoring-jobs/{job_id}/cancel",
    response_model=BatchScoringJobRead,
)
def cancel_job(
    job_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    try:
        _job_or_404(db, job_id, principal)
        require_organization_role(principal, "org_admin", "teacher")
        return cancel_batch_scoring_job(db, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/batch-scoring-jobs/{job_id}/retry",
    response_model=BatchScoringJobRead,
)
async def retry_job(
    job_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    try:
        _job_or_404(db, job_id, principal)
        require_organization_role(principal, "org_admin", "teacher")
        job = retry_batch_scoring_job(db, job_id)
    except ValueError as exc:
        status = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    await _dispatch_or_503(job)
    return job


@router.post(
    "/batch-scoring-jobs/{job_id}/run",
    response_model=BatchScoringJobRead,
)
def run_job(
    job_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    if os.getenv("VERCEL"):
        raise HTTPException(
            status_code=409,
            detail="生产评分由 Vercel 后台执行器通过队列逐份领取；请查看任务进度，无需在请求中启动。",
        )
    _job_or_404(db, job_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    bind = db.get_bind()
    db.rollback()
    factory = sessionmaker(bind=bind, autocommit=False, autoflush=False)
    try:
        return run_batch_scoring_job(factory, job_id=job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
