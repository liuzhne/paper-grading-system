"""共享的可见性守卫：页面接口与评分助手的图调用同一组函数。

这些函数原先分散在各路由模块里（`_visible_batch`、`_visible_paper`、
`_job_or_404`……），助手的图要执行同样的读写，复制一份就会出现“改了一边漏了
另一边”。现在路由模块里的旧名字只是这里的别名，同一个函数对象
（对话评分助手方案 T4）。

只查归属与可见性，不查角色；角色由调用方 `require_organization_role` 负责。
"""

from datetime import datetime
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.deps import CurrentPrincipal
from backend.app.db.models import GradingBatch
from backend.app.db.models import Paper
from backend.app.db.models import Rubric
from backend.app.db.models import RubricImportSession
from backend.app.db.models import ScoringRun
from backend.app.services.auth import auth_active


def visible_batch(db: Session, batch_id: str, principal: CurrentPrincipal) -> GradingBatch:
    batch = db.get(GradingBatch, batch_id)
    if batch is None or (
        principal.organization_id is not None and batch.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="batch not found")
    return batch


def visible_paper(db: Session, paper_id: str, principal: CurrentPrincipal) -> Paper:
    paper = db.get(Paper, paper_id)
    if paper is None or (
        principal.organization_id is not None and paper.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="paper not found")
    return paper


def visible_run(db: Session, run_id: str, principal: CurrentPrincipal) -> ScoringRun:
    run = db.get(ScoringRun, run_id)
    if run is None or (
        principal.organization_id is not None
        and run.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="scoring run not found")
    return run


def visible_job(db: Session, job_id: str, principal: CurrentPrincipal):
    from backend.app.services.batch_scoring.jobs import get_batch_scoring_job

    job = get_batch_scoring_job(db, job_id)
    if job is None or (
        principal.organization_id is not None
        and job.batch.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="batch scoring job not found")
    return job


def rubric_visible_to(rubric: Rubric, principal: CurrentPrincipal) -> bool:
    """单份评分标准的可见性判定；列表过滤见 `visible_rubrics_filter`，两者同一口径。"""

    if not auth_active() or principal.platform_role == "platform_admin":
        return True
    return bool(
        rubric.visibility == "system"
        or rubric.visibility == "organization" and rubric.organization_id == principal.organization_id
        or rubric.visibility == "private" and rubric.owner_id == principal.user_id
    )


def visible_rubrics_filter(principal: CurrentPrincipal):
    """列表查询用的过滤条件；不需要过滤时返回 None。"""

    if not auth_active() or principal.platform_role == "platform_admin":
        return None
    return or_(
        Rubric.visibility == "system",
        and_(
            Rubric.visibility == "organization",
            Rubric.organization_id == principal.organization_id,
        ),
        and_(
            Rubric.visibility == "private",
            Rubric.owner_id == principal.user_id,
        ),
    )


def visible_rubric(db: Session, rubric_id: str, principal: CurrentPrincipal, *, loader=None) -> Rubric:
    rubric = loader(db, rubric_id) if loader is not None else db.get(Rubric, rubric_id)
    if rubric is None or not rubric_visible_to(rubric, principal):
        raise HTTPException(status_code=404, detail="rubric not found")
    return rubric


def visible_import_session(
    db: Session,
    session_id: str,
    principal: CurrentPrincipal,
    *,
    for_update: bool = False,
) -> RubricImportSession:
    query = select(RubricImportSession).where(RubricImportSession.id == session_id)
    if for_update:
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None:
        raise HTTPException(status_code=404, detail="rubric import session not found")
    allowed = (
        not auth_active()
        or principal.platform_role == "platform_admin"
        or row.owner_id == principal.user_id
        or row.visibility == "organization"
        and row.organization_id == principal.organization_id
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="rubric import session not found")
    if row.status == "draft" and row.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
        row.status = "expired"
        row.state_version += 1
        db.commit()
        raise HTTPException(status_code=410, detail="rubric import session has expired")
    return row
