from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.app.api.deps import current_user_id
from backend.app.db.session import get_db
from backend.app.schemas.release_gate import ReleaseGateApprovalCreate
from backend.app.schemas.release_gate import ReleaseGateProfileCreate
from backend.app.schemas.release_gate import ReleaseGateProfileRead
from backend.app.schemas.release_gate import ReleaseGateRunCreate
from backend.app.schemas.release_gate import ReleaseGateRunRead
from backend.app.services.dev_user import ensure_dev_user
from backend.app.services import release_gates


router = APIRouter(prefix="/release-gates", tags=["release-gates"])


def _service_error(exc):
    status_code = (
        409 if isinstance(exc, release_gates.ReleaseGateConflict) else 400
    )
    return HTTPException(status_code=status_code, detail=str(exc))


@router.post("/profiles", response_model=ReleaseGateProfileRead)
def create_release_gate_profile(
    payload: ReleaseGateProfileCreate,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
):
    ensure_dev_user(db)
    try:
        return release_gates.create_profile(db, payload, user_id)
    except release_gates.ReleaseGateServiceError as exc:
        db.rollback()
        raise _service_error(exc) from exc


@router.get("/profiles", response_model=list[ReleaseGateProfileRead])
def list_release_gate_profiles(db: Session = Depends(get_db)):
    return release_gates.list_profiles(db)


@router.get("/profiles/{profile_id}", response_model=ReleaseGateProfileRead)
def get_release_gate_profile(profile_id: str, db: Session = Depends(get_db)):
    profile = release_gates.get_profile(db, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="release gate profile not found")
    return profile


@router.post(
    "/profiles/{profile_id}/runs",
    response_model=ReleaseGateRunRead,
)
def register_release_gate_run(
    profile_id: str,
    payload: ReleaseGateRunCreate,
    db: Session = Depends(get_db),
):
    try:
        return release_gates.register_run(
            db,
            profile_id,
            payload.candidate_record,
        )
    except release_gates.ReleaseGateServiceError as exc:
        db.rollback()
        raise _service_error(exc) from exc


@router.post(
    "/profiles/{profile_id}/rehearsals",
    response_model=ReleaseGateRunRead,
)
def register_release_gate_rehearsal(
    profile_id: str,
    payload: ReleaseGateRunCreate,
    db: Session = Depends(get_db),
):
    try:
        return release_gates.register_test_only_rehearsal(
            db,
            profile_id,
            payload.candidate_record,
        )
    except release_gates.ReleaseGateServiceError as exc:
        db.rollback()
        raise _service_error(exc) from exc


@router.get("/runs/{run_id}", response_model=ReleaseGateRunRead)
def get_release_gate_run(run_id: str, db: Session = Depends(get_db)):
    run = release_gates.get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="release gate run not found")
    return run


@router.post("/runs/{run_id}/approve", response_model=ReleaseGateRunRead)
def approve_release_gate_run(
    run_id: str,
    payload: ReleaseGateApprovalCreate,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
):
    ensure_dev_user(db)
    try:
        return release_gates.approve_run(
            db,
            run_id,
            payload.privacy_review,
            user_id,
        )
    except release_gates.ReleaseGateServiceError as exc:
        db.rollback()
        raise _service_error(exc) from exc
