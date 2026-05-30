from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session

from backend.app.db.models import Rubric
from backend.app.db.session import get_db
from backend.app.schemas.calibration import CalibrationAnchorCreate
from backend.app.schemas.calibration import CalibrationAnchorRead
from backend.app.services.calibration import create_anchor
from backend.app.services.calibration import list_anchors

router = APIRouter(prefix="/calibration", tags=["calibration"])


@router.post("/anchors", response_model=CalibrationAnchorRead)
def create_calibration_anchor(payload: CalibrationAnchorCreate, db: Session = Depends(get_db)):
    if db.get(Rubric, payload.rubric_id) is None:
        raise HTTPException(status_code=404, detail="rubric not found")
    if payload.score > payload.max_score:
        raise HTTPException(status_code=400, detail="score cannot exceed max_score")
    return create_anchor(db, payload)


@router.get("/anchors", response_model=list[CalibrationAnchorRead])
def list_calibration_anchors(
    rubric_id: str = Query(...),
    criterion_code: str = Query(default=None),
    db: Session = Depends(get_db),
):
    return list_anchors(db, rubric_id, criterion_code)
