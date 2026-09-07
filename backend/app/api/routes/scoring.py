from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from backend.app.api.deps import CurrentPrincipal
from backend.app.api.deps import current_principal
from backend.app.api.deps import current_user_id
from backend.app.api.deps import require_organization_role
from backend.app.db.models import Paper
from backend.app.db.models import ReviewLog
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun
from backend.app.db.session import get_db
from backend.app.services.scoring import document_view
from backend.app.schemas.scoring import ReviewLogRead
from backend.app.schemas.scoring import ReviewSubmit
from backend.app.schemas.scoring import ScoreItemRead
from backend.app.schemas.scoring import ScoreItemUpdate
from backend.app.schemas.scoring import ScoringRunRead
from backend.app.services.dev_user import ensure_dev_user
from backend.app.services.llm.base import LLMScoringError
from backend.app.services.scoring.engine import score_paper
from backend.app.services.scoring.engine import retry_score_paper
from backend.app.services.scoring.engine import submit_review
from backend.app.services.scoring.engine import update_score_item

router = APIRouter(tags=["scoring"])


def _visible_paper(db: Session, paper_id: str, principal: CurrentPrincipal) -> Paper:
    paper = db.get(Paper, paper_id)
    if paper is None or (
        principal.organization_id is not None
        and paper.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="paper not found")
    return paper


def _visible_run(
    db: Session,
    run_id: str,
    principal: CurrentPrincipal,
) -> ScoringRun:
    run = db.get(ScoringRun, run_id)
    if run is None or (
        principal.organization_id is not None
        and run.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="scoring run not found")
    return run


def _visible_score_item(
    db: Session,
    item_id: str,
    principal: CurrentPrincipal,
) -> ScoreItem:
    item = db.get(ScoreItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="score item not found")
    _visible_run(db, item.scoring_run_id, principal)
    return item


@router.get("/scoring-runs", response_model=list[ScoringRunRead])
def list_scoring_runs(
    paper_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    query = select(ScoringRun).order_by(ScoringRun.created_at.desc())
    if principal.organization_id is not None:
        query = query.where(ScoringRun.organization_id == principal.organization_id)
    if paper_id:
        query = query.where(ScoringRun.paper_id == paper_id)
    if batch_id:
        query = query.join(Paper).where(Paper.batch_id == batch_id)
    return db.scalars(query).all()


@router.post("/papers/{paper_id}/score", response_model=ScoringRunRead)
def create_scoring_run(
    paper_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    try:
        _visible_paper(db, paper_id, principal)
        require_organization_role(principal, "org_admin", "teacher")
        return score_paper(db, paper_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LLMScoringError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/scoring-runs/{run_id}")
def get_scoring_run(
    run_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    run = _visible_run(db, run_id, principal)
    if run.submission_id is not None:
        raise HTTPException(
            status_code=400,
            detail="submission scoring runs must be queried through /api/v2",
        )
    payload = ScoringRunRead.model_validate(run).model_dump(mode="json")
    # 单条审计查询必须明确展示历史空身份；批量/旧导出仍可省略新增 null 字段，
    # 以维持 M0 的公开 JSON 兼容合同。
    payload.update(
        {
            "policy_snapshot": run.policy_snapshot,
            "policy_hash": run.policy_hash,
            "policy_schema_version": run.policy_schema_version,
        }
    )
    return payload


@router.get("/scoring-runs/{run_id}/document-view")
def get_document_view(
    run_id: str,
    limit: int = Query(default=document_view.DEFAULT_LIMIT, ge=1, le=document_view.MAX_LIMIT),
    cursor: str | None = None,
    anchor_id: str | None = None,
    response: Response = None,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """评分工作区中间栏的只读正文投影（计划 §5-A）。

    与评分项展示投影共用同一份快照身份。正文可能包含学生论文原文，因此
    禁止任何共享缓存。
    """
    run = _visible_run(db, run_id, principal)
    payload = document_view.build_document_view(
        db, run, limit=limit, cursor=cursor, anchor_id=anchor_id
    )
    if response is not None:
        response.headers["Cache-Control"] = "private, no-store"
    return payload


@router.post("/scoring-runs/{run_id}/retry", response_model=ScoringRunRead)
def retry_scoring_run(
    run_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    run = _visible_run(db, run_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    if run.submission_id is not None:
        raise HTTPException(
            status_code=400,
            detail="submission scoring runs must be retried through /api/v2",
        )
    try:
        return retry_score_paper(db, run.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LLMScoringError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/scoring-runs/{run_id}/items", response_model=list[ScoreItemRead])
def list_score_items(
    run_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _visible_run(db, run_id, principal)
    return db.scalars(
        select(ScoreItem)
        .where(ScoreItem.scoring_run_id == run_id)
        .options(selectinload(ScoreItem.criterion))
        .order_by(ScoreItem.created_at)
    ).all()


@router.patch("/score-items/{item_id}", response_model=ScoreItemRead)
def patch_score_item(
    item_id: str,
    payload: ScoreItemUpdate,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
    user_id: str = Depends(current_user_id),
):
    ensure_dev_user(db)
    try:
        _visible_score_item(db, item_id, principal)
        require_organization_role(principal, "org_admin", "teacher")
        return update_score_item(db, item_id, payload.final_score, payload.reason, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/scoring-runs/{run_id}/review", response_model=ScoringRunRead)
def review_scoring_run(
    run_id: str,
    payload: ReviewSubmit,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
    user_id: str = Depends(current_user_id),
):
    ensure_dev_user(db)
    try:
        _visible_run(db, run_id, principal)
        require_organization_role(principal, "org_admin", "teacher")
        return submit_review(db, run_id, payload.reason, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/scoring-runs/{run_id}/review-logs", response_model=list[ReviewLogRead])
def list_review_logs(
    run_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _visible_run(db, run_id, principal)
    return db.scalars(select(ReviewLog).where(ReviewLog.scoring_run_id == run_id).order_by(ReviewLog.created_at)).all()
