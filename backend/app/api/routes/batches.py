from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.deps import current_user_id
from backend.app.core.config import settings
from backend.app.db.models import GradingBatch
from backend.app.db.models import Rubric
from backend.app.db.models import RubricCompilation
from backend.app.db.models import RubricVersion
from backend.app.db.session import get_db
from backend.app.schemas.batch import BatchCreate
from backend.app.schemas.batch import BatchRead
from backend.app.schemas.batch import BatchScoreResult
from backend.app.schemas.batch import BatchSummary
from backend.app.schemas.batch import BatchUpdate
from backend.app.services.dev_user import ensure_dev_user
from backend.app.services.batches import get_batch_summary
from backend.app.services.calibration.analytics import batch_ranking
from backend.app.services.calibration.analytics import drift_monitor
from backend.app.services.calibration.analytics import review_sample
from backend.app.services.calibration.analytics import score_drift
from backend.app.services.scoring.engine import score_batch

router = APIRouter(prefix="/batches", tags=["batches"])


def _is_frozen_version(db: Session, rubric: Rubric, version: RubricVersion) -> bool:
    compilation = db.get(RubricCompilation, version.compilation_id)
    return bool(
        rubric.status == "published"
        and rubric.published_at is not None
        and version.rubric_id == rubric.id
        and compilation is not None
        and compilation.rubric_id == rubric.id
        and compilation.status == "validated"
        and compilation.reviewed_by is not None
        and compilation.reviewed_at is not None
        and compilation.published_at is not None
        and compilation.reviewed_at == compilation.published_at
        and compilation.published_at == rubric.published_at
        and compilation.final_version_hash == version.version_hash
    )


def _resolve_batch_version(
    db: Session,
    rubric: Rubric,
    requested_version_id: str | None,
) -> RubricVersion | None:
    if requested_version_id is not None:
        version = db.get(RubricVersion, requested_version_id)
        if version is None:
            raise HTTPException(status_code=400, detail="rubric version not found")
        if version.rubric_id != rubric.id:
            raise HTTPException(
                status_code=400,
                detail="rubric version does not belong to selected rubric",
            )
        if not _is_frozen_version(db, rubric, version):
            raise HTTPException(
                status_code=400,
                detail="rubric version is not a consistently frozen published version",
            )
        return version

    formal_versions = db.scalars(
        select(RubricVersion).where(RubricVersion.rubric_id == rubric.id)
    ).all()
    if not formal_versions:
        return None
    eligible = [
        version
        for version in formal_versions
        if _is_frozen_version(db, rubric, version)
    ]
    if len(eligible) == 1:
        return eligible[0]
    if not eligible:
        raise HTTPException(
            status_code=400,
            detail="formal rubric has no consistently frozen published version",
        )
    raise HTTPException(
        status_code=400,
        detail="multiple frozen versions exist; rubric_version_id is required",
    )


@router.post("", response_model=BatchRead)
def create_batch(payload: BatchCreate, db: Session = Depends(get_db), user_id: str = Depends(current_user_id)):
    ensure_dev_user(db)
    rubric = db.get(Rubric, payload.rubric_id)
    if rubric is None:
        raise HTTPException(status_code=404, detail="rubric not found")
    rubric_version = _resolve_batch_version(
        db, rubric, payload.rubric_version_id
    )
    batch = GradingBatch(
        name=payload.name,
        department=payload.department,
        major=payload.major,
        academic_year=payload.academic_year,
        paper_type=payload.paper_type,
        rubric_id=payload.rubric_id,
        rubric_version_id=(rubric_version.id if rubric_version else None),
        status=payload.status,
        created_by=user_id,
        owner_id=user_id,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


@router.get("", response_model=list[BatchRead])
def list_batches(db: Session = Depends(get_db)):
    return db.scalars(select(GradingBatch).order_by(GradingBatch.created_at.desc())).all()


@router.post("/{batch_id}/score", response_model=BatchScoreResult)
def score_batch_endpoint(batch_id: str, rescore: bool = False, db: Session = Depends(get_db)):
    ensure_dev_user(db)
    try:
        return score_batch(db, batch_id, rescore=rescore)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{batch_id}/start", response_model=BatchScoreResult)
def start_batch_endpoint(batch_id: str, rescore: bool = False, db: Session = Depends(get_db)):
    return score_batch_endpoint(batch_id, rescore=rescore, db=db)


@router.get("/{batch_id}/summary", response_model=BatchSummary)
def get_batch_summary_endpoint(batch_id: str, db: Session = Depends(get_db)):
    try:
        return get_batch_summary(db, batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{batch_id}/ranking")
def batch_ranking_endpoint(batch_id: str, db: Session = Depends(get_db)):
    try:
        return batch_ranking(db, batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{batch_id}/drift")
def batch_drift_endpoint(batch_id: str, db: Session = Depends(get_db)):
    try:
        return score_drift(db, batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{batch_id}/review-sample")
def batch_review_sample_endpoint(
    batch_id: str,
    ratio: float | None = Query(default=None, ge=0.0, le=1.0),
    seed: str = "",
    db: Session = Depends(get_db),
):
    try:
        return review_sample(db, batch_id, ratio=ratio, seed=seed)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{batch_id}/drift-monitor")
def batch_drift_monitor_endpoint(batch_id: str, db: Session = Depends(get_db)):
    try:
        return drift_monitor(db, batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.patch("/{batch_id}", response_model=BatchRead)
def update_batch(batch_id: str, payload: BatchUpdate, db: Session = Depends(get_db)):
    ensure_dev_user(db)
    batch = db.get(GradingBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")

    updates = payload.model_dump(exclude_unset=True)
    rubric_id_present = "rubric_id" in updates
    version_id_present = "rubric_version_id" in updates
    requested_rubric_id = updates.pop("rubric_id", batch.rubric_id)
    requested_version_id = updates.pop(
        "rubric_version_id",
        None if rubric_id_present else batch.rubric_version_id,
    )
    identity_changed = (
        requested_rubric_id != batch.rubric_id
        or requested_version_id != batch.rubric_version_id
        or version_id_present
    )
    if identity_changed:
        if batch.papers:
            raise HTTPException(
                status_code=400,
                detail="cannot change rubric/version after papers have been uploaded",
            )
        rubric = db.get(Rubric, requested_rubric_id)
        if rubric is None:
            raise HTTPException(status_code=404, detail="rubric not found")
        resolved_version = _resolve_batch_version(
            db, rubric, requested_version_id
        )
        # Assign the composite identity only after every check succeeds so a
        # cross-rubric or unfrozen request cannot leave a partial pin.
        batch.rubric_id = rubric.id
        batch.rubric_version_id = (
            resolved_version.id if resolved_version is not None else None
        )

    for field, value in updates.items():
        setattr(batch, field, value)

    db.commit()
    db.refresh(batch)
    return batch


@router.get("/{batch_id}", response_model=BatchRead)
def get_batch(batch_id: str, db: Session = Depends(get_db)):
    batch = db.get(GradingBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return batch
