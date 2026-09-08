from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.deps import CurrentPrincipal
from backend.app.api.deps import current_principal
from backend.app.api.deps import require_organization_role
from backend.app.db.models import GradingBatch
from backend.app.db.models import Paper
from backend.app.db.models import ScoringRun
from backend.app.db.models import SpreadsheetWriteLog
from backend.app.db.session import get_db
from backend.app.schemas.export import ExportLogRead
from backend.app.schemas.export import WriteSheetRequest
from backend.app.services.report.generator import generate_report
from backend.app.services.report.json_export import build_run_export
from backend.app.services.spreadsheet.excel import export_batch_excel
from backend.app.services.spreadsheet.writer import SpreadsheetWriteError
from backend.app.services.spreadsheet.writer import write_run_to_sheet

router = APIRouter(tags=["exports"])


def _legacy_thesis_run(
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
    if run.submission_id is not None:
        raise HTTPException(
            status_code=400,
            detail="submission scoring runs must use /api/v2 export endpoints",
        )
    return run


@router.get("/export-logs", response_model=list[ExportLogRead])
def list_export_logs(
    batch_id: Optional[str] = None,
    run_id: Optional[str] = None,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    # 导出是成绩数据离开系统的地方。组织归属只回答「是不是本组织的数据」，
    # 不回答「这个人该不该把它导出去」。
    require_organization_role(principal, "org_admin", "teacher")
    query = (
        select(SpreadsheetWriteLog)
        .join(ScoringRun)
        .order_by(SpreadsheetWriteLog.created_at.desc())
    )
    if principal.organization_id is not None:
        query = query.where(ScoringRun.organization_id == principal.organization_id)
    if run_id:
        query = query.where(SpreadsheetWriteLog.scoring_run_id == run_id)
    if batch_id:
        query = query.join(Paper).where(Paper.batch_id == batch_id)
    return db.scalars(query).all()


@router.get("/batches/{batch_id}/export.xlsx")
def export_batch(
    batch_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    batch = db.get(GradingBatch, batch_id)
    if batch is None or (
        principal.organization_id is not None
        and batch.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="batch not found")
    # 导出是成绩数据离开系统的地方。组织归属只回答「是不是本组织的数据」，
    # 不回答「这个人该不该把它导出去」。
    require_organization_role(principal, "org_admin", "teacher")
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
def write_sheet(
    run_id: str,
    payload: WriteSheetRequest | None = None,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _legacy_thesis_run(db, run_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        target_id = payload.target_id if payload else None
        return write_run_to_sheet(db, run_id, target_id=target_id)
    except SpreadsheetWriteError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/scoring-runs/{run_id}/report")
def report(
    run_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _legacy_thesis_run(db, run_id, principal)
    # 导出是成绩数据离开系统的地方。组织归属只回答「是不是本组织的数据」，
    # 不回答「这个人该不该把它导出去」。
    require_organization_role(principal, "org_admin", "teacher")
    try:
        path = generate_report(db, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return FileResponse(path, media_type="text/html; charset=utf-8", filename=path.name)


@router.get("/scoring-runs/{run_id}/export.json")
def export_run_json(
    run_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """结构化 JSON 导出（运行/论文/逐项/扣分/证据/篇章·格式发现/复核），供下游二次处理。"""
    _legacy_thesis_run(db, run_id, principal)
    # 导出是成绩数据离开系统的地方。组织归属只回答「是不是本组织的数据」，
    # 不回答「这个人该不该把它导出去」。
    require_organization_role(principal, "org_admin", "teacher")
    try:
        return build_run_export(db, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
