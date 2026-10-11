import os
import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Response
from fastapi.concurrency import run_in_threadpool
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
from backend.app.services.batch_scoring.jobs import KIND as BATCH_SCORING_KIND
from backend.app.services.batch_scoring.jobs import RETRYABLE_ITEM_STATUSES
from backend.app.services.batch_scoring.jobs import cancel_batch_scoring_job
from backend.app.services.batch_scoring.jobs import create_batch_scoring_job
from backend.app.services.batch_scoring.jobs import get_batch_scoring_job
from backend.app.services.batch_scoring.jobs import get_latest_batch_scoring_job
from backend.app.services.batch_scoring.jobs import list_attention_batch_scoring_jobs
from backend.app.services.batch_scoring.jobs import retry_batch_scoring_job
from backend.app.services.batch_scoring.jobs import run_batch_scoring_job
from backend.app.services.dev_user import ensure_dev_user
from backend.app.services.scoring.usage_estimate import TokenCapExceededError
from backend.app.services.scoring.usage_estimate import assert_within_token_caps
from backend.app.services.scoring.usage_estimate import estimate_batch
from backend.app.services.work_queue.sweep import ensure_sweep_chain
from backend.app.services.work_queue.sweep import sweep_parent_on_read
from backend.app.services.work_queue.wake import wake_for_capacity


router = APIRouter(tags=["batch-scoring-jobs"])
logger = logging.getLogger("batch-scoring-jobs")


# 叫醒与它用的会话工厂在共享动作里（对话评分助手方案 T4）：评分助手的图开评、重试后同样要叫醒。
_session_factory = actions.session_factory


_wake_job = actions.wake_job


def _sweep_on_read(db, job_id):
    """进度读取对本任务做一次限频巡检，让卡住的条目在用户刷新后几秒内恢复。"""

    try:
        sweep_parent_on_read(_session_factory(db), BATCH_SCORING_KIND, job_id)
    except Exception:
        logger.exception("batch_scoring_read_sweep_failed job_id=%s", job_id)
    db.expire_all()


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


_has_active_job = actions.has_active_job


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
    # 与评分助手共用同一个动作（对话评分助手方案 T4）。
    job, created = actions.start_scoring_job(db, principal, user_id, batch_id, payload)
    await run_in_threadpool(_wake_job, db, job, reason="create")
    response.status_code = 201 if created else 200
    return get_batch_scoring_job(db, job.id)


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
    if job.status in ACTIVE_JOB_STATUSES:
        job_id = job.id
        _sweep_on_read(db, job_id)
        job = get_batch_scoring_job(db, job_id)
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
    job = _job_or_404(db, job_id, principal)
    if job.status in ACTIVE_JOB_STATUSES:
        _sweep_on_read(db, job_id)
        job = _job_or_404(db, job_id, principal)
    return job


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
    await run_in_threadpool(_wake_job, db, job, reason="retry")
    return get_batch_scoring_job(db, job.id)


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
            detail="生产评分由 Vercel 后台执行器逐份领取；请查看任务进度，无需在请求中启动。",
        )
    _job_or_404(db, job_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    factory = _session_factory(db)
    try:
        return run_batch_scoring_job(factory, job_id=job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
