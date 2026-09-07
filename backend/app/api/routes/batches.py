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
from backend.app.db.models import ExportEvent
from backend.app.db.models import GradingBatch
from backend.app.db.models import Paper
from backend.app.db.models import Rubric
from backend.app.db.models import RubricCompilation
from backend.app.db.models import RubricVersion
from backend.app.db.session import get_db
from backend.app.schemas.batch import BatchCreate
from backend.app.schemas.batch import BatchOverviewRead
from backend.app.schemas.batch import BatchProgressRead
from backend.app.schemas.batch import BatchRead
from backend.app.schemas.batch import BatchScoreResult
from backend.app.schemas.batch import BatchSummary
from backend.app.schemas.batch import BatchUpdate
from backend.app.schemas.batch import CompleteReviewRequest
from backend.app.schemas.batch import ExportEventCreate
from backend.app.schemas.batch import ReviewAcceptRequest
from backend.app.schemas.batch import UploadPrecheckRequest
from backend.app.schemas.batch import ReviewAcceptResult
from backend.app.services.dev_user import ensure_dev_user
from backend.app.services.auth import auth_active
from backend.app.services.ai_connections import connection_snapshot_for_owner
from backend.app.services.batches import get_batch_summary
from backend.app.services.batches import review_accept
from backend.app.services.batches import exports
from backend.app.services.batches import review_queue
from backend.app.services.papers import precheck
from backend.app.services.batches import review_stats
from backend.app.services.batches import state
from backend.app.services.batches import state as batch_state_module
from backend.app.services.batches.results import current_job
from backend.app.services.batches.results import select_current_results
from backend.app.services.calibration.analytics import batch_ranking
from backend.app.services.calibration.analytics import drift_monitor
from backend.app.services.calibration.analytics import review_sample
from backend.app.services.calibration.analytics import score_drift
from backend.app.services.scoring.engine import score_batch

router = APIRouter(prefix="/batches", tags=["batches"])

#: 仍需要注意力的阶段。已复核与已归档属于「做完了」，不占用待办计数。
_TODO_STAGES = ("draft", "parsing", "scoring", "scored", "scored_with_errors")


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


@router.get("/overview", response_model=BatchOverviewRead)
def batches_overview(
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """工作台 KPI（计划 §6）。

    材料计数经结果选择器，与评分任务页、复核页同源——否则各页面会给出互相
    矛盾的数字。已归档批次不计入待办口径，否则待评数永远降不下去。
    """
    query = select(GradingBatch)
    if principal.organization_id is not None:
        query = query.where(GradingBatch.organization_id == principal.organization_id)
    batches = db.scalars(query).all()

    stage_counts = {stage: 0 for stage in state.BATCH_STAGES}
    materials = {"total": 0, "scored": 0, "reviewed": 0, "failed": 0, "pending": 0}
    active = 0
    for batch in batches:
        stage_counts[batch.status] = stage_counts.get(batch.status, 0) + 1
        if batch.status in _TODO_STAGES:
            active += 1
        if batch.status == "archived":
            # 归档批次只读，其材料不再是待办。
            continue
        selection = select_current_results(db, batch)
        materials["total"] += selection.total_count
        materials["scored"] += selection.scored_count
        materials["reviewed"] += selection.reviewed_count
        materials["failed"] += selection.failed_count
        materials["pending"] += selection.pending_count

    return {
        "total_batches": len(batches),
        "active_batches": active,
        "stage_counts": stage_counts,
        "material_counts": materials,
    }


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


@router.get("/{batch_id}/progress", response_model=BatchProgressRead)
def batch_progress_endpoint(
    batch_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """阶段、结果计数、执行状态与可执行动作。

    阶段与 job 状态**并行返回**：一次 job 失败不代表批次阶段就是失败，只看
    阶段字段会漏掉「任务已中断、等待恢复」这类情况（计划 §5-C、§5-D）。
    """
    batch = _visible_batch(db, batch_id, principal)
    selection = select_current_results(db, batch)
    job = current_job(db, batch.id)
    return {
        "batch_id": batch.id,
        "stage": batch.status,
        "state_version": batch.state_version,
        "counts": {
            "total": selection.total_count,
            "scored": selection.scored_count,
            "reviewed": selection.reviewed_count,
            "failed": selection.failed_count,
            "pending": selection.pending_count,
        },
        "completion_ratio": selection.completion_ratio,
        "result_revision": selection.revision,
        "job": (
            None
            if job is None
            else {
                "id": job.id,
                "generation": job.generation,
                "status": job.status,
                "total_items": job.total_items,
                "succeeded_count": job.succeeded_count,
                "failed_count": job.failed_count,
                "pending_count": job.pending_count,
            }
        ),
        "available_actions": state.available_events(batch),
    }


@router.get("/{batch_id}/review-queue")
def batch_review_queue(
    batch_id: str,
    limit: int = Query(default=review_queue.DEFAULT_LIMIT, ge=1, le=review_queue.MAX_LIMIT),
    cursor: str | None = None,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """批次级复核队列（计划 §5-B）。

    阻塞任务排在普通确认之前：先解决「算不算数」，再讨论「给几分」。
    """
    batch = _visible_batch(db, batch_id, principal)
    return review_queue.build_review_queue(db, batch, limit=limit, cursor=cursor)


@router.post("/{batch_id}/review-queue/accept", response_model=ReviewAcceptResult)
def accept_review_queue_items(
    batch_id: str,
    payload: ReviewAcceptRequest,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
    user_id: str = Depends(current_user_id),
):
    """采纳本页可采纳项（计划 §5-B）。

    整次要么全写、要么全不写；同键同载荷幂等重放。
    """
    ensure_dev_user(db)
    batch = _visible_batch(db, batch_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        result = review_accept.accept_items(
            db,
            batch,
            items=[item.model_dump() for item in payload.items],
            result_revision=payload.result_revision,
            idempotency_key=payload.idempotency_key,
            reason=payload.reason,
            actor_id=user_id,
            organization_id=principal.organization_id,
        )
    except review_accept.AcceptConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except batch_state_module.BatchArchived as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except review_accept.AcceptError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return result


@router.get("/{batch_id}/review-stats")
def batch_review_stats(
    batch_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """复核统计（计划 §5-B）。已确认数按当前评分项算，不按日志条数累加。"""
    batch = _visible_batch(db, batch_id, principal)
    return review_stats.build_review_stats(db, batch)


@router.get("/{batch_id}/review-timeline")
def batch_review_timeline(
    batch_id: str,
    limit: int = Query(default=review_stats.DEFAULT_LIMIT, ge=1, le=review_stats.MAX_LIMIT),
    cursor: str | None = None,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """复核审计流，按时间倒序。"""
    batch = _visible_batch(db, batch_id, principal)
    return review_stats.build_review_timeline(db, batch, limit=limit, cursor=cursor)


@router.post("/{batch_id}/complete-review", response_model=BatchRead)
def complete_batch_review(
    batch_id: str,
    payload: CompleteReviewRequest,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """完成复核并转 reviewed（计划 §5-C）。

    前置条件由服务端重算：普通待确认为 0、阻塞为 0、结果集合未变。客户端
    不能只凭「界面上看着清空了」就完成复核。
    """
    ensure_dev_user(db)
    batch = _visible_batch(db, batch_id, principal)
    require_organization_role(principal, "org_admin", "teacher")

    stats = review_stats.build_review_stats(db, batch)
    if stats["result_revision"] != payload.result_revision:
        raise HTTPException(status_code=409, detail="批次结果已更新，请刷新后重试。")
    if stats["ordinary_pending"]:
        raise HTTPException(
            status_code=409,
            detail="仍有 %d 项待确认，无法完成复核。" % stats["ordinary_pending"],
        )
    if stats["blocking_open"]:
        raise HTTPException(
            status_code=409,
            detail="仍有 %d 个阻塞任务未解决，无法完成复核。" % stats["blocking_open"],
        )

    try:
        state.apply_event(db, batch, "complete_review")
    except state.BatchStateError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    db.refresh(batch)
    return batch


@router.post("/{batch_id}/upload-precheck")
def batch_upload_precheck(
    batch_id: str,
    payload: UploadPrecheckRequest,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """解析预检（计划 §5-E）。

    只汇总既有解析诊断——不重传文件、不额外跑一次全文解析，否则一次预检会
    把整批材料重解析一遍，还可能得出与后续评分实际使用的解析结果不一致的
    结论。
    """
    _visible_batch(db, batch_id, principal)
    requested = list(dict.fromkeys(payload.paper_ids))
    papers = db.scalars(
        select(Paper).where(Paper.id.in_(requested)).order_by(Paper.created_at, Paper.id)
    ).all()
    found = {paper.id for paper in papers}
    if set(requested) - found or any(paper.batch_id != batch_id for paper in papers):
        raise HTTPException(status_code=404, detail="paper not found in batch")
    return precheck.build_precheck(papers)


@router.get("/{batch_id}/export-precheck")
def batch_export_precheck(
    batch_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """导出前检查（计划 §5-F）。

    正式成绩要求复核清零；报告与结构化审计导出允许在中间状态进行，只是会
    标注未完成——不用新界面放宽旧服务已有的边界。
    """
    batch = _visible_batch(db, batch_id, principal)
    return exports.build_export_precheck(db, batch)


@router.get("/{batch_id}/export-history")
def batch_export_history(
    batch_id: str,
    limit: int = Query(default=exports.DEFAULT_LIMIT, ge=1, le=exports.MAX_LIMIT),
    cursor: str | None = None,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """全通道导出历史。新事件与旧 run 日志并存展示，不合并成假的批次事件。"""
    batch = _visible_batch(db, batch_id, principal)
    return exports.build_export_history(db, batch, limit=limit, cursor=cursor)


@router.post("/{batch_id}/export-events", status_code=201)
def create_export_event(
    batch_id: str,
    payload: ExportEventCreate,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
    user_id: str = Depends(current_user_id),
):
    ensure_dev_user(db)
    batch = _visible_batch(db, batch_id, principal)
    selection = select_current_results(db, batch)
    if selection.revision != payload.result_revision:
        raise HTTPException(status_code=409, detail="批次结果已更新，请刷新后重试。")

    event = ExportEvent(
        organization_id=principal.organization_id,
        grading_batch_id=batch.id,
        channel=payload.channel,
        scope=payload.scope,
        result_revision=selection.revision,
        # 只记录到「已生成」。
        status="generated",
        actor_id=user_id,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return {
        "id": event.id,
        "channel": event.channel,
        "scope": event.scope,
        "status": event.status,
        "actor_id": event.actor_id,
        "result_revision": event.result_revision,
        "created_at": event.created_at,
    }


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
