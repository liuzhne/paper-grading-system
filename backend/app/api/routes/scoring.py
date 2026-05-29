from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from backend.app.core.config import settings
from backend.app.db.models import Paper
from backend.app.db.models import ReviewLog
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun
from backend.app.db.session import get_db
from backend.app.schemas.scoring import ReviewLogRead
from backend.app.schemas.scoring import ReviewSubmit
from backend.app.schemas.scoring import ScoreItemRead
from backend.app.schemas.scoring import ScoreItemUpdate
from backend.app.schemas.scoring import ScoringRunRead
from backend.app.services.dev_user import ensure_dev_user
from backend.app.services.llm.base import LLMScoringError
from backend.app.services.scoring.engine import score_paper
from backend.app.services.scoring.engine import submit_review
from backend.app.services.scoring.engine import update_score_item

router = APIRouter(tags=["scoring"])


@router.get("/scoring-runs", response_model=list[ScoringRunRead])
def list_scoring_runs(
    paper_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = select(ScoringRun).order_by(ScoringRun.created_at.desc())
    if paper_id:
        query = query.where(ScoringRun.paper_id == paper_id)
    if batch_id:
        query = query.join(Paper).where(Paper.batch_id == batch_id)
    return db.scalars(query).all()


@router.post("/papers/{paper_id}/score", response_model=ScoringRunRead)
def create_scoring_run(paper_id: str, db: Session = Depends(get_db)):
    ensure_dev_user(db)
    try:
        return score_paper(db, paper_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LLMScoringError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/scoring-runs/{run_id}", response_model=ScoringRunRead)
def get_scoring_run(run_id: str, db: Session = Depends(get_db)):
    run = db.get(ScoringRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="scoring run not found")
    return run


@router.post("/scoring-runs/{run_id}/retry", response_model=ScoringRunRead)
def retry_scoring_run(run_id: str, db: Session = Depends(get_db)):
    ensure_dev_user(db)
    run = db.get(ScoringRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="scoring run not found")
    try:
        return score_paper(db, run.paper_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LLMScoringError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/scoring-runs/{run_id}/items", response_model=list[ScoreItemRead])
def list_score_items(run_id: str, db: Session = Depends(get_db)):
    run = db.get(ScoringRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="scoring run not found")
    return db.scalars(
        select(ScoreItem)
        .where(ScoreItem.scoring_run_id == run_id)
        .options(selectinload(ScoreItem.criterion))
        .order_by(ScoreItem.created_at)
    ).all()


@router.patch("/score-items/{item_id}", response_model=ScoreItemRead)
def patch_score_item(item_id: str, payload: ScoreItemUpdate, db: Session = Depends(get_db)):
    ensure_dev_user(db)
    try:
        return update_score_item(db, item_id, payload.final_score, payload.reason, settings.DEFAULT_DEV_USER_ID)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/scoring-runs/{run_id}/review", response_model=ScoringRunRead)
def review_scoring_run(run_id: str, payload: ReviewSubmit, db: Session = Depends(get_db)):
    ensure_dev_user(db)
    try:
        return submit_review(db, run_id, payload.reason, settings.DEFAULT_DEV_USER_ID)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/scoring-runs/{run_id}/review-logs", response_model=list[ReviewLogRead])
def list_review_logs(run_id: str, db: Session = Depends(get_db)):
    run = db.get(ScoringRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="scoring run not found")
    return db.scalars(select(ReviewLog).where(ReviewLog.scoring_run_id == run_id).order_by(ReviewLog.created_at)).all()
