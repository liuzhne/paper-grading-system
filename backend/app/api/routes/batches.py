from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.deps import CurrentPrincipal
from backend.app.api.deps import current_principal
from backend.app.api.deps import current_user_id
from backend.app.api.deps import require_organization_role
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
from backend.app.services.auth import auth_active
from backend.app.services.ai_connections import connection_snapshot_for_owner
from backend.app.services.batches import get_batch_summary
from backend.app.services.batches import state
from backend.app.services.calibration.analytics import batch_ranking
from backend.app.services.calibration.analytics import drift_monitor
from backend.app.services.calibration.analytics import review_sample
from backend.app.services.calibration.analytics import score_drift
from backend.app.services.scoring.engine import score_batch

router = APIRouter(prefix="/batches", tags=["batches"])


def _visible_batch(db: Session, batch_id: str, principal: CurrentPrincipal) -> GradingBatch:
    batch = db.get(GradingBatch, batch_id)
    if batch is None or (
        principal.organization_id is not None and batch.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="batch not found")
    return batch


def _visible_rubric(db: Session, rubric_id: str, principal: CurrentPrincipal) -> Rubric:
    rubric = db.get(Rubric, rubric_id)
    if rubric is None:
        raise HTTPException(status_code=404, detail="rubric not found")
    if not auth_active() or principal.platform_role == "platform_admin":
        return rubric
    if rubric.visibility == "system":
        return rubric
    if rubric.visibility == "organization" and rubric.organization_id == principal.organization_id:
        return rubric
    if rubric.visibility == "private" and rubric.owner_id == principal.user_id:
        return rubric
    raise HTTPException(status_code=404, detail="rubric not found")


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
def create_batch(payload: BatchCreate, db: Session = Depends(get_db), user_id: str = Depends(current_user_id), principal: CurrentPrincipal = Depends(current_principal)):
    ensure_dev_user(db)
    require_organization_role(principal, "org_admin", "teacher")
    rubric = _visible_rubric(db, payload.rubric_id, principal)
    rubric_version = _resolve_batch_version(
        db, rubric, payload.rubric_version_id
    )
    connection_snapshot = None
    if payload.ai_connection_id is not None:
        try:
            connection_snapshot = connection_snapshot_for_owner(
                db,
                connection_id=payload.ai_connection_id,
                owner_id=user_id,
                organization_id=principal.organization_id or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="AI connection not found") from exc
    batch = GradingBatch(
        name=payload.name,
        department=payload.department,
        major=payload.major,
        academic_year=payload.academic_year,
        paper_type=payload.paper_type,
        rubric_id=payload.rubric_id,
        rubric_version_id=(rubric_version.id if rubric_version else None),
        ai_connection_id=payload.ai_connection_id,
        ai_connection_key_version=(
            connection_snapshot["key_version"] if connection_snapshot else None
        ),
        ai_connection_snapshot=connection_snapshot,
        status="draft",
        created_by=user_id,
        owner_id=user_id,
        organization_id=principal.organization_id,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


@router.get("", response_model=list[BatchRead])
def list_batches(db: Session = Depends(get_db), principal: CurrentPrincipal = Depends(current_principal)):
    query = select(GradingBatch).order_by(GradingBatch.created_at.desc())
    if principal.organization_id is not None:
        query = query.where(GradingBatch.organization_id == principal.organization_id)
    return db.scalars(query).all()


@router.post("/{batch_id}/score", response_model=BatchScoreResult)
def score_batch_endpoint(
    batch_id: str,
    rescore: bool = False,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    try:
        _visible_batch(db, batch_id, principal)
        require_organization_role(principal, "org_admin", "teacher")
        return score_batch(db, batch_id, rescore=rescore)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{batch_id}/start", response_model=BatchScoreResult)
def start_batch_endpoint(
    batch_id: str,
    rescore: bool = False,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    return score_batch_endpoint(batch_id, rescore=rescore, db=db, principal=principal)


@router.get("/{batch_id}/summary", response_model=BatchSummary)
def get_batch_summary_endpoint(batch_id: str, db: Session = Depends(get_db), principal: CurrentPrincipal = Depends(current_principal)):
    try:
        _visible_batch(db, batch_id, principal)
        return get_batch_summary(db, batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{batch_id}/ranking")
def batch_ranking_endpoint(
    batch_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    try:
        _visible_batch(db, batch_id, principal)
        return batch_ranking(db, batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{batch_id}/drift")
def batch_drift_endpoint(
    batch_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    try:
        _visible_batch(db, batch_id, principal)
        return score_drift(db, batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{batch_id}/review-sample")
def batch_review_sample_endpoint(
    batch_id: str,
    ratio: float | None = Query(default=None, ge=0.0, le=1.0),
    seed: str = "",
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    try:
        _visible_batch(db, batch_id, principal)
        return review_sample(db, batch_id, ratio=ratio, seed=seed)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{batch_id}/drift-monitor")
def batch_drift_monitor_endpoint(
    batch_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    try:
        _visible_batch(db, batch_id, principal)
        return drift_monitor(db, batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.patch("/{batch_id}", response_model=BatchRead)
def update_batch(
    batch_id: str,
    payload: BatchUpdate,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    batch = _visible_batch(db, batch_id, principal)
    require_organization_role(principal, "org_admin", "teacher")

    updates = payload.model_dump(exclude_unset=True)

    # 阶段不能由客户端直接写（前端 v2 计划 §5-C）。兼容期允许旧客户端回传
    # **未变化**的 status 作为 no-op；任何真正的阶段变更必须走动作服务，
    # 否则会绕过转移合法性、归档守卫与 state_version 并发检查。
    requested_status = updates.pop("status", None)
    if requested_status is not None and requested_status != batch.status:
        raise HTTPException(
            status_code=409,
            detail=(
                "批次阶段不能通过 PATCH 修改（当前 %s，请求 %s）；"
                "请使用对应的阶段动作端点。" % (batch.status, requested_status)
            ),
        )
    state.guard_writable(batch)

    rubric_id_present = "rubric_id" in updates
    version_id_present = "rubric_version_id" in updates
    requested_rubric_id = updates.pop("rubric_id", batch.rubric_id)
    requested_version_id = updates.pop(
        "rubric_version_id",
        None if rubric_id_present else batch.rubric_version_id,
    )
    requested_connection_id = updates.pop("ai_connection_id", batch.ai_connection_id)
    connection_changed = requested_connection_id != batch.ai_connection_id
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
        rubric = _visible_rubric(db, requested_rubric_id, principal)
        resolved_version = _resolve_batch_version(
            db, rubric, requested_version_id
        )
        # Assign the composite identity only after every check succeeds so a
        # cross-rubric or unfrozen request cannot leave a partial pin.
        batch.rubric_id = rubric.id
        batch.rubric_version_id = (
            resolved_version.id if resolved_version is not None else None
        )

    if connection_changed:
        if batch.papers:
            raise HTTPException(
                status_code=400,
                detail="cannot change AI connection after papers have been uploaded",
            )
        if requested_connection_id is None:
            batch.ai_connection_id = None
            batch.ai_connection_key_version = None
            batch.ai_connection_snapshot = None
        else:
            try:
                snapshot = connection_snapshot_for_owner(
                    db,
                    connection_id=requested_connection_id,
                    owner_id=principal.user_id,
                    organization_id=principal.organization_id or "",
                )
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="AI connection not found") from exc
            batch.ai_connection_id = requested_connection_id
            batch.ai_connection_key_version = snapshot["key_version"]
            batch.ai_connection_snapshot = snapshot

    for field, value in updates.items():
        setattr(batch, field, value)

    db.commit()
    db.refresh(batch)
    return batch


@router.get("/{batch_id}", response_model=BatchRead)
def get_batch(
    batch_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    return _visible_batch(db, batch_id, principal)
