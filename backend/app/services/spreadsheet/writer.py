import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from backend.app.core.config import settings
from backend.app.db.models import Paper
from backend.app.db.models import ReviewLog
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun
from backend.app.db.models import SpreadsheetWriteLog
from backend.app.services.scoring.rules import as_float
from backend.app.services.spreadsheet.excel import DETAIL_HEADERS
from backend.app.services.spreadsheet.excel import SUMMARY_HEADERS
from backend.app.services.spreadsheet.excel import _main_deductions
from backend.app.services.spreadsheet.excel import _review_notes


class SpreadsheetWriteError(RuntimeError):
    pass


def write_run_to_sheet(db: Session, run_id: str, target_id: str | None = None):
    provider = (settings.SHEET_WRITER_PROVIDER or "mock").lower()
    if provider == "mock":
        return write_run_to_mock_sheet(db, run_id, target_id=target_id)
    if settings.OFFLINE_MODE and provider in {"google_sheets", "google_apps_script"}:
        # 离线模式硬禁外呼：网络型写表禁用，引导改用本地 Excel 导出。
        raise SpreadsheetWriteError("离线模式（OFFLINE_MODE）下禁用 Google Sheets 网络导出，请改用 Excel 导出（/export.xlsx 或 pgs export）。")
    if provider in {"google_sheets", "google_apps_script"}:
        try:
            return write_run_to_google_apps_script(db, run_id, target_id=target_id)
        except (ValueError, SpreadsheetWriteError):
            if settings.SHEET_FALLBACK_TO_MOCK:
                return write_run_to_mock_sheet(db, run_id, target_id=target_id)
            raise
    raise ValueError("unsupported SHEET_WRITER_PROVIDER: %s" % settings.SHEET_WRITER_PROVIDER)


def write_run_to_mock_sheet(db: Session, run_id: str, target_id: str | None = None):
    run = _load_run(db, run_id)
    payload = _sheet_payload(db, run)
    log = SpreadsheetWriteLog(
        scoring_run_id=run.id,
        target_type="mock_sheet",
        target_id=target_id or "local-preview",
        status="success",
        response=payload,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def write_run_to_google_apps_script(db: Session, run_id: str, target_id: str | None = None, client=None):
    if not settings.GOOGLE_SHEETS_WEBAPP_URL:
        raise ValueError("GOOGLE_SHEETS_WEBAPP_URL is required when SHEET_WRITER_PROVIDER=google_sheets")

    run = _load_run(db, run_id)
    payload = _sheet_payload(db, run)
    request_payload = {
        "secret": settings.GOOGLE_SHEETS_WEBAPP_SECRET,
        "target_id": target_id,
        "run_id": run.id,
        "payload": payload,
    }
    http_client = client or httpx.Client(timeout=settings.GOOGLE_SHEETS_TIMEOUT_SECONDS)
    owns_client = client is None  # 自建的 client 用完要关，避免连接池/fd 泄漏
    try:
        try:
            response = http_client.post(settings.GOOGLE_SHEETS_WEBAPP_URL, json=request_payload)
            response.raise_for_status()
            response_payload = response.json()
            if response_payload.get("ok") is False:
                raise ValueError(response_payload.get("error") or "google sheets endpoint returned ok=false")
        except Exception as exc:
            log = SpreadsheetWriteLog(
                scoring_run_id=run.id,
                target_type="google_sheets",
                target_id=target_id,
                status="failed",
                response={"request": payload},
                error_message=str(exc),
            )
            db.add(log)
            db.commit()
            db.refresh(log)
            raise SpreadsheetWriteError("google sheets write failed: %s" % exc) from exc

        log = SpreadsheetWriteLog(
            scoring_run_id=run.id,
            target_type="google_sheets",
            target_id=target_id or response_payload.get("spreadsheet_id") or response_payload.get("target_id"),
            status="success",
            response={"request": payload, "provider_response": response_payload},
        )
        db.add(log)
        db.commit()
        db.refresh(log)
        return log
    finally:
        if owns_client:
            close = getattr(http_client, "close", None)
            if callable(close):
                close()


def _load_run(db, run_id):
    run = db.scalar(
        select(ScoringRun)
        .where(ScoringRun.id == run_id)
        .options(
            selectinload(ScoringRun.paper).selectinload(Paper.batch),
            selectinload(ScoringRun.rubric),
            selectinload(ScoringRun.items),
            selectinload(ScoringRun.items).selectinload(ScoreItem.criterion),
        )
    )
    if run is None:
        raise ValueError("scoring run not found")
    return run


def _sheet_payload(db, run):
    review_logs = db.scalars(select(ReviewLog).where(ReviewLog.scoring_run_id == run.id).order_by(ReviewLog.created_at)).all()
    return {
        "summary_headers": SUMMARY_HEADERS,
        "summary_row": _summary_row(run, review_logs),
        "detail_headers": DETAIL_HEADERS,
        "detail_rows": [_detail_row(run, item) for item in run.items],
    }


def _summary_row(run, review_logs):
    paper = run.paper
    batch = paper.batch
    changed = any(as_float(item.final_score) != as_float(item.ai_score) for item in run.items)
    values = [
        batch.name,
        paper.student_id,
        paper.student_name,
        paper.department or batch.department,
        paper.major or batch.major,
        paper.title,
        run.rubric.version,
        as_float(run.ai_total_score),
        as_float(run.final_total_score),
        run.grade,
        _main_deductions(run),
        "是" if changed else "否",
        "是" if run.need_manual_review else "否",
        run.finished_at.isoformat(sep=" ") if run.finished_at else "",
        "dev-user" if run.status == "reviewed" else "",
        _review_notes(review_logs),
        "/api/scoring-runs/%s/report" % run.id,
    ]
    return dict(zip(SUMMARY_HEADERS, values))


def _detail_row(run, item):
    paper = run.paper
    values = [
        paper.student_id,
        paper.student_name,
        paper.title,
        item.criterion.name,
        as_float(item.max_score),
        as_float(item.ai_score),
        as_float(item.final_score),
        "；".join(item.deductions or []),
        "；".join(evidence.get("quote", "") for evidence in item.evidence or []),
        "；".join(evidence.get("location", "") for evidence in item.evidence or []),
        item.suggestion,
        as_float(item.confidence),
    ]
    return dict(zip(DETAIL_HEADERS, values))
