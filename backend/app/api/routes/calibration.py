from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session

from backend.app.api.deps import CurrentPrincipal
from backend.app.api.deps import current_principal
from backend.app.api.deps import require_organization_role
from backend.app.db.models import Rubric
from backend.app.db.session import get_db
from backend.app.schemas.calibration import CalibrationAnchorCreate
from backend.app.schemas.calibration import CalibrationAnchorRead
from backend.app.services.calibration import create_anchor
from backend.app.services.calibration import list_anchors

router = APIRouter(prefix="/calibration", tags=["calibration"])


def _visible_rubric(
    db: Session,
    rubric_id: str,
    principal: CurrentPrincipal,
) -> Rubric:
    rubric = db.get(Rubric, rubric_id)
    if rubric is None or (
        principal.organization_id is not None
        and rubric.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="rubric not found")
    return rubric


@router.post("/anchors", response_model=CalibrationAnchorRead)
def create_calibration_anchor(
    payload: CalibrationAnchorCreate,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _visible_rubric(db, payload.rubric_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    if payload.score > payload.max_score:
        raise HTTPException(status_code=400, detail="score cannot exceed max_score")
    return create_anchor(db, payload)


@router.get("/anchors", response_model=list[CalibrationAnchorRead])
def list_calibration_anchors(
    rubric_id: str = Query(...),
    criterion_code: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _visible_rubric(db, rubric_id, principal)
    return list_anchors(db, rubric_id, criterion_code)
