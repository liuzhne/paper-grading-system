"""共享的写动作：页面接口与评分助手的图调用同一组函数（对话评分助手方案 T4）。

每个函数都自己执行可见性守卫与角色校验，因此图直接调用它们时拿到的权限
边界与页面接口完全一致。路由函数里仍保留一行守卫与角色校验：静态门禁测试
（`test_every_batch_write_route_is_role_gated` 等）按源码检查每个写路由，
那一行是给这道门禁看的，重复执行一次只多一次主键读取。

这里抛 `HTTPException`，与路由原有的状态码和文案保持一致；图在调用处把它
转成对话里的说明。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from backend.app.api import guards
from backend.app.api.deps import CurrentPrincipal
from backend.app.api.deps import require_organization_role
from backend.app.core.config import settings
from backend.app.db.models import BatchScoringItem
from backend.app.db.models import GradingBatch
from backend.app.db.models import Paper
from backend.app.db.models import PaperChunk
from backend.app.db.models import Rubric
from backend.app.db.models import RubricCompilation
from backend.app.db.models import RubricVersion
from backend.app.db.models import ScoringRun
from backend.app.services.ai_connections import active_connection_id
from backend.app.services.ai_connections import connection_snapshot_for_owner
from backend.app.services.auth import audit
from backend.app.services.batch_scoring.jobs import ACTIVE_JOB_STATUSES
from backend.app.services.batch_scoring.jobs import RETRYABLE_ITEM_STATUSES
from backend.app.services.batch_scoring.jobs import cancel_batch_scoring_job
from backend.app.services.batch_scoring.jobs import create_batch_scoring_job
from backend.app.services.batch_scoring.jobs import get_latest_batch_scoring_job
from backend.app.services.batch_scoring.jobs import retry_batch_scoring_job
from backend.app.services.batches import duration
from backend.app.services.papers import precheck
from backend.app.services.scoring.usage_estimate import TokenCapExceededError
from backend.app.services.scoring.usage_estimate import assert_within_token_caps
from backend.app.services.storage.local import artifact_key_invalid
from backend.app.services.storage.local import artifact_not_found
from backend.app.services.storage.local import delete_private_object
from backend.app.services.work_queue.sweep import ensure_sweep_chain
from backend.app.services.work_queue.wake import wake_for_capacity

WRITE_ROLES = ("org_admin", "teacher")

logger = logging.getLogger("batch-scoring-jobs")


# --- 评分标准 --------------------------------------------------------------------

def list_visible_rubrics(db: Session, principal: CurrentPrincipal) -> list[Rubric]:
    query = select(Rubric)
    visibility = guards.visible_rubrics_filter(principal)
    if visibility is not None:
        query = query.where(visibility)
    return list(db.scalars(query.order_by(Rubric.created_at.desc())).all())


def is_frozen_version(db: Session, rubric: Rubric, version: RubricVersion) -> bool:
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


def resolve_batch_version(
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
        if not is_frozen_version(db, rubric, version):
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
        if is_frozen_version(db, rubric, version)
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


# --- 批次 --------------------------------------------------------------------------

def create_batch(db: Session, principal: CurrentPrincipal, user_id: str, payload) -> GradingBatch:
    """新建评分任务；`payload` 为 `BatchCreate`。"""

    require_organization_role(principal, *WRITE_ROLES)
    rubric = guards.visible_rubric(db, payload.rubric_id, principal)
    rubric_version = resolve_batch_version(db, rubric, payload.rubric_version_id)
    connection_id = payload.ai_connection_id or active_connection_id(
        db, owner_id=user_id, organization_id=principal.organization_id or "",
    )
    connection_snapshot = None
    if connection_id is not None:
        try:
            connection_snapshot = connection_snapshot_for_owner(
                db,
                connection_id=connection_id,
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
        ai_connection_id=connection_id,
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


def upload_precheck(db: Session, principal: CurrentPrincipal, batch_id: str, paper_ids: list[str]) -> dict:
    """解析预检：只汇总既有解析诊断，不重传、不重解析。"""

    guards.visible_batch(db, batch_id, principal)
    require_organization_role(principal, *WRITE_ROLES)
    requested = list(dict.fromkeys(paper_ids))
    papers = db.scalars(
        select(Paper).where(Paper.id.in_(requested)).order_by(Paper.created_at, Paper.id)
    ).all()
    found = {paper.id for paper in papers}
    if set(requested) - found or any(paper.batch_id != batch_id for paper in papers):
        raise HTTPException(status_code=404, detail="paper not found in batch")

    result = precheck.build_precheck(papers)
    # 预计耗时只进展示，不能用于评分租约或超时判定。
    result["duration_estimate"] = duration.estimate_scoring_duration(
        db,
        organization_id=principal.organization_id,
        item_count=len(papers),
    )
    return result


# --- 论文 --------------------------------------------------------------------------

def _paper_problem(status_code, code, message, user_action, *, retryable):
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "user_action": user_action, "retryable": retryable},
    )


def delete_paper(db: Session, principal: CurrentPrincipal, paper_id: str) -> None:
    """删除上传未完成的记录，或解析失败且从未评分（无评分记录、无批任务条目、无分块）的材料。"""

    paper = guards.visible_paper(db, paper_id, principal)
    require_organization_role(principal, *WRITE_ROLES)
    removable_failed_parse = paper.status == "failed" and not any((
        db.scalar(select(ScoringRun.id).where(ScoringRun.paper_id == paper.id).limit(1)),
        db.scalar(select(BatchScoringItem.id).where(BatchScoringItem.paper_id == paper.id).limit(1)),
        db.scalar(select(PaperChunk.id).where(PaperChunk.paper_id == paper.id).limit(1)),
    ))
    if paper.status != "uploading" and not removable_failed_parse:
        _paper_problem(
            409,
            "PAPER_DELETE_STATE_INVALID",
            "仅能删除失败的上传记录，或解析失败且从未评分的材料。",
            "已归档或已解析的材料请保留；如需移除，请使用对应的数据管理流程。",
            retryable=False,
        )

    if removable_failed_parse and paper.file_path and not paper.file_path.startswith("supabase://"):
        # 本地存储：只删存储根目录下的文件，路径由服务端生成，这里再兜一次底。
        local = Path(paper.file_path).resolve()
        root = Path(settings.STORAGE_ROOT).resolve()
        if root in local.parents:
            local.unlink(missing_ok=True)
    if paper.file_path.startswith("supabase://"):
        try:
            delete_private_object(paper.file_path)
        except Exception as exc:
            # 历史上的 Unicode 键在建对象前就被拒绝；它与“对象已不存在”都按清理成功处理，
            # 真正的存储故障保留记录。
            if not (artifact_not_found(exc) or artifact_key_invalid(exc)):
                _paper_problem(
                    502,
                    "FAILED_UPLOAD_CLEANUP_FAILED",
                    "暂时无法清理私有存储中的失败上传。",
                    "请稍后重试删除；当前记录和文件引用均已保留。",
                    retryable=True,
                )

    audit(
        db,
        "paper.failed_upload_deleted",
        actor_id=principal.user_id,
        organization_id=paper.organization_id,
        metadata={"paper_id": paper.id, "batch_id": paper.batch_id, "status": paper.status},
    )
    db.delete(paper)
    db.commit()


# --- 批量评分任务 ------------------------------------------------------------------

def has_active_job(db: Session, batch_id: str) -> bool:
    latest = get_latest_batch_scoring_job(db, batch_id)
    return latest is not None and latest.status in ACTIVE_JOB_STATUSES


def start_scoring_job(db: Session, principal: CurrentPrincipal, user_id: str, batch_id: str, payload):
    """建（或幂等返回进行中的）批量评分任务；`payload` 为 `BatchScoringJobCreate`。

    只落库，不叫醒：调用方提交自己的改动后再调用 `wake_job`（路由在线程池里调用）。
    """

    try:
        guards.visible_batch(db, batch_id, principal)
        require_organization_role(principal, *WRITE_ROLES)
        # 进行中的任务按幂等返回；只有新建的工作才检查输入 token 上限。
        if not has_active_job(db, batch_id):
            assert_within_token_caps(db, batch_id, rescore=payload.rescore)
        return create_batch_scoring_job(
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


def retry_scoring_job(db: Session, principal: CurrentPrincipal, job_id: str):
    """只重排失败、取消或中断的条目；只落库，不叫醒（同上）。"""

    try:
        existing = guards.visible_job(db, job_id, principal)
        require_organization_role(principal, *WRITE_ROLES)
        retry_paper_ids = [
            item.paper_id
            for item in existing.items
            if item.status in RETRYABLE_ITEM_STATUSES
        ]
        if retry_paper_ids:
            # 重试的论文复用决策账本，通常只计入失败的那几条规则。
            assert_within_token_caps(
                db,
                existing.grading_batch_id,
                rescore=existing.rescore,
                paper_ids=retry_paper_ids,
            )
        return retry_batch_scoring_job(db, job_id)
    except TokenCapExceededError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        status = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc


def cancel_scoring_job(db: Session, principal: CurrentPrincipal, job_id: str):
    try:
        guards.visible_job(db, job_id, principal)
        require_organization_role(principal, *WRITE_ROLES)
        return cancel_batch_scoring_job(db, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def session_factory(db: Session):
    """与请求同库的独立会话工厂：巡检与叫醒各自提交，不和请求的事务混在一起。

    会回滚调用方会话里未提交的改动：调用方必须先提交自己的改动再叫醒。
    """

    bind = db.get_bind()
    db.rollback()
    return sessionmaker(bind=bind, autocommit=False, autoflush=False)


def wake_job(db: Session, job, *, reason: str) -> None:
    """按空闲名额叫醒（Vercel）；没发出去也不影响任务，巡检会补发。"""

    source_key = next((item.source_key for item in job.items), None)
    # 同一次提交的重放（双击、重试请求）得到同一个幂等键。
    token = "%s-%s" % (job.id, job.updated_at.isoformat() if job.updated_at else "")
    factory = session_factory(db)
    try:
        if source_key:
            with factory() as session:
                wake_for_capacity(session, source_key, reason=reason, token=token)
        ensure_sweep_chain(factory)
    except Exception:
        logger.exception("batch_scoring_wake_failed job_id=%s", job.id)
