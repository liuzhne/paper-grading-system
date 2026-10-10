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


def _session_factory(db):
    """与请求同库的独立会话工厂：巡检与叫醒各自提交，不和请求的事务混在一起。"""

    bind = db.get_bind()
    db.rollback()
    return sessionmaker(bind=bind, autocommit=False, autoflush=False)


def _wake_job(db, job, *, reason):
    """按空闲名额叫醒（Vercel）；没发出去也不影响任务，巡检会补发。"""

    source_key = next((item.source_key for item in job.items), None)
    # 同一次提交的重放（双击、重试请求）得到同一个幂等键。
    token = "%s-%s" % (job.id, job.updated_at.isoformat() if job.updated_at else "")
    factory = _session_factory(db)
    try:
        if source_key:
            with factory() as session:
                wake_for_capacity(session, source_key, reason=reason, token=token)
        ensure_sweep_chain(factory)
    except Exception:
        logger.exception("batch_scoring_wake_failed job_id=%s", job.id)


def _sweep_on_read(db, job_id):
    """进度读取对本任务做一次限频巡检，让卡住的条目在用户刷新后几秒内恢复。"""

    try:
        sweep_parent_on_read(_session_factory(db), BATCH_SCORING_KIND, job_id)
    except Exception:
        logger.exception("batch_scoring_read_sweep_failed job_id=%s", job_id)
    db.expire_all()


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
    try:
        _batch_or_404(db, batch_id, principal)
        require_organization_role(principal, "org_admin", "teacher")
        # An active job is returned idempotently below; only new work is
        # checked against the configured input-token caps.
        if not _has_active_job(db, batch_id):
            assert_within_token_caps(db, batch_id, rescore=payload.rescore)
        job, created = create_batch_scoring_job(
            db,
            batch_id=batch_id,
            rescore=payload.rescore,
            max_workers=payload.max_workers,
            observation_policy=payload.observation_policy,
            actor_id=user_id,
        )
    except TokenCapExceededError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        detail = str(exc)
        status = 404 if detail == "batch not found" else 409 if "active" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc
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
        existing = _job_or_404(db, job_id, principal)
        require_organization_role(principal, "org_admin", "teacher")
        retry_paper_ids = [
            item.paper_id
            for item in existing.items
            if item.status in RETRYABLE_ITEM_STATUSES
        ]
        if retry_paper_ids:
            # Retried papers reuse the decision ledger, so this usually only
            # counts the rules that failed.
            assert_within_token_caps(
                db,
                existing.grading_batch_id,
                rescore=existing.rescore,
                paper_ids=retry_paper_ids,
            )
        job = retry_batch_scoring_job(db, job_id)
    except TokenCapExceededError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        status = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
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
