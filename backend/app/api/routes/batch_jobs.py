import os
import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Response
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from backend.app.api import actions
from backend.app.api import guards
from backend.app.api.deps import CurrentPrincipal
from backend.app.api.deps import current_principal
from backend.app.api.deps import current_user_id
from backend.app.api.deps import require_organization_role
from backend.app.db.models import GradingBatch
from backend.app.db.session import get_db
from backend.app.schemas.batch_job import BatchScoringJobCreate
from backend.app.schemas.batch_job import BatchScoreEstimateRead
from backend.app.schemas.batch_job import BatchScoringJobRead
from backend.app.services.batch_scoring.jobs import ACTIVE_JOB_STATUSES
from backend.app.services.batch_scoring.jobs import RETRYABLE_ITEM_STATUSES
from backend.app.services.batch_scoring.jobs import cancel_batch_scoring_job
from backend.app.services.batch_scoring.jobs import create_batch_scoring_job
from backend.app.services.batch_scoring.jobs import get_batch_scoring_job
from backend.app.services.batch_scoring.jobs import get_latest_batch_scoring_job
from backend.app.services.batch_scoring.jobs import list_attention_batch_scoring_jobs
from backend.app.services.batch_scoring.jobs import retry_batch_scoring_job
from backend.app.services.batch_scoring.jobs import run_batch_scoring_job
from backend.app.services.batch_scoring.vercel_queue import dispatch_batch_scoring_job
from backend.app.services.dev_user import ensure_dev_user
from backend.app.services.scoring.usage_estimate import TokenCapExceededError
from backend.app.services.scoring.usage_estimate import assert_within_token_caps
from backend.app.services.scoring.usage_estimate import estimate_batch


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


_batch_or_404 = guards.visible_batch


_job_or_404 = guards.visible_job


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


@router.get(
    "/batches/{batch_id}/score-estimate",
    response_model=BatchScoreEstimateRead,
)
def read_score_estimate(
    batch_id: str,
    rescore: bool = Query(default=False),
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """Estimate input tokens locally before starting; no provider is called."""

    _batch_or_404(db, batch_id, principal)
    return estimate_batch(db, batch_id, rescore=rescore)


def _has_active_job(db, batch_id) -> bool:
    latest = get_latest_batch_scoring_job(db, batch_id)
    return latest is not None and latest.status in ACTIVE_JOB_STATUSES


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
    _batch_or_404(db, batch_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    # 与评分助手共用同一个动作（对话评分助手方案 T4）；派发在这里异步完成。
    job, created = actions.start_scoring_job(db, principal, user_id, batch_id, payload)
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
    _job_or_404(db, job_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    return actions.cancel_scoring_job(db, principal, job_id)


@router.post(
    "/batch-scoring-jobs/{job_id}/retry",
    response_model=BatchScoringJobRead,
)
async def retry_job(
    job_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _job_or_404(db, job_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    job = actions.retry_scoring_job(db, principal, job_id)
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
