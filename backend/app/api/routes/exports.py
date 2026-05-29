from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import Paper
from backend.app.db.models import ScoringRun
from backend.app.db.models import SpreadsheetWriteLog
from backend.app.db.session import get_db
from backend.app.schemas.export import ExportLogRead
from backend.app.schemas.export import WriteSheetRequest
from backend.app.services.report.generator import generate_report
from backend.app.services.spreadsheet.excel import export_batch_excel
from backend.app.services.spreadsheet.writer import SpreadsheetWriteError
from backend.app.services.spreadsheet.writer import write_run_to_sheet

router = APIRouter(tags=["exports"])


@router.get("/export-logs", response_model=list[ExportLogRead])
def list_export_logs(
    batch_id: Optional[str] = None,
    run_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = select(SpreadsheetWriteLog).order_by(SpreadsheetWriteLog.created_at.desc())
    if run_id:
        query = query.where(SpreadsheetWriteLog.scoring_run_id == run_id)
    if batch_id:
        query = query.join(ScoringRun).join(Paper).where(Paper.batch_id == batch_id)
    return db.scalars(query).all()


@router.get("/batches/{batch_id}/export.xlsx")
def export_batch(batch_id: str, db: Session = Depends(get_db)):
    try:
        path = export_batch_excel(db, batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@router.post("/scoring-runs/{run_id}/write-sheet", response_model=ExportLogRead)
def write_sheet(run_id: str, payload: WriteSheetRequest | None = None, db: Session = Depends(get_db)):
    try:
        target_id = payload.target_id if payload else None
        return write_run_to_sheet(db, run_id, target_id=target_id)
    except SpreadsheetWriteError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/scoring-runs/{run_id}/report")
def report(run_id: str, db: Session = Depends(get_db)):
    try:
        path = generate_report(db, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return FileResponse(path, media_type="text/html; charset=utf-8", filename=path.name)
