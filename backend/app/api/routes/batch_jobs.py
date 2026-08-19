from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Response
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from backend.app.api.deps import current_user_id
from backend.app.db.session import get_db
from backend.app.schemas.batch_job import BatchScoringJobCreate
from backend.app.schemas.batch_job import BatchScoringJobRead
from backend.app.services.batch_scoring.jobs import cancel_batch_scoring_job
from backend.app.services.batch_scoring.jobs import create_batch_scoring_job
from backend.app.services.batch_scoring.jobs import get_batch_scoring_job
from backend.app.services.batch_scoring.jobs import get_latest_batch_scoring_job
from backend.app.services.batch_scoring.jobs import retry_batch_scoring_job
from backend.app.services.batch_scoring.jobs import run_batch_scoring_job
from backend.app.services.dev_user import ensure_dev_user


router = APIRouter(tags=["batch-scoring-jobs"])


def _job_or_404(db, job_id):
    job = get_batch_scoring_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="batch scoring job not found")
    return job


@router.post(
    "/batches/{batch_id}/score-jobs",
    response_model=BatchScoringJobRead,
    status_code=201,
)
def create_job(
    batch_id: str,
    payload: BatchScoringJobCreate,
    response: Response,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
):
    ensure_dev_user(db)
    try:
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
    response.status_code = 201 if created else 200
    return job


@router.get(
    "/batches/{batch_id}/score-jobs/latest",
    response_model=BatchScoringJobRead,
)
def latest_job(batch_id: str, db: Session = Depends(get_db)):
    job = get_latest_batch_scoring_job(db, batch_id)
    if job is None:
        raise HTTPException(status_code=404, detail="batch scoring job not found")
    return job


@router.get(
    "/batch-scoring-jobs/{job_id}",
    response_model=BatchScoringJobRead,
)
def read_job(job_id: str, db: Session = Depends(get_db)):
    return _job_or_404(db, job_id)


@router.post(
    "/batch-scoring-jobs/{job_id}/cancel",
    response_model=BatchScoringJobRead,
)
def cancel_job(job_id: str, db: Session = Depends(get_db)):
    try:
        return cancel_batch_scoring_job(db, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/batch-scoring-jobs/{job_id}/retry",
    response_model=BatchScoringJobRead,
)
def retry_job(job_id: str, db: Session = Depends(get_db)):
    try:
        return retry_batch_scoring_job(db, job_id)
    except ValueError as exc:
        status = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post(
    "/batch-scoring-jobs/{job_id}/run",
    response_model=BatchScoringJobRead,
)
def run_job(job_id: str, db: Session = Depends(get_db)):
    _job_or_404(db, job_id)
    bind = db.get_bind()
    db.rollback()
    factory = sessionmaker(bind=bind, autocommit=False, autoflush=False)
    try:
        return run_batch_scoring_job(factory, job_id=job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

