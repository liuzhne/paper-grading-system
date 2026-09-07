"""导出预检与历史（前端 v2 计划 §5-F）。

历史来自两个源，且**不合并**：

``export_event``
    新的事件级记录，有真实操作人与结果集合摘要。

``legacy_run_log``
    旧的 ``SpreadsheetWriteLog``。一次 v1 批次 xlsx 导出可能对应多条 run 日志；
    按文件路径或相近时间把它们并成「一次批次导出」是在编造一个从未被记录过的
    事件。宁可如实显示多条，也不合成假的一条。

旧表没有操作人字段。拿 ``run.owner`` 顶上会把**评分的所有者**写成**点导出的
人**——两者常常不是同一个人。这里显示「历史记录未记录」。
"""

from __future__ import annotations

from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.db.models import ExportEvent
from backend.app.db.models import Paper
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun
from backend.app.db.models import SpreadsheetWriteLog
from backend.app.services.batches.results import select_current_results
from backend.app.services.batches.review_stats import build_review_stats


DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: 旧 target_type -> 新展示通道。原值同时保留在 legacy_target_type 里，
#: 便于对账时回到旧记录。
LEGACY_CHANNELS = {
    "mock_sheet": "mock_sheet",
    "google_sheets": "sheets",
    "excel": "xlsx",
    "excel_v2": "xlsx",
}

CHANNELS = ("html_report", "xlsx", "json", "sheets", "mock_sheet")


def sheets_available():
    provider = (settings.SHEET_WRITER_PROVIDER or "mock").lower()
    return (
        not settings.OFFLINE_MODE
        and provider in {"google_sheets", "google_apps_script"}
        and bool(settings.GOOGLE_SHEETS_WEBAPP_URL)
    )


def build_export_precheck(session, batch):
    stats = build_review_stats(session, batch)
    selection = select_current_results(session, batch)

    blocking = []
    warnings = []

    if stats["blocking_open"]:
        blocking.append(
            {
                "code": "blocking_tasks_open",
                "message": "仍有 %d 个阻塞任务未解决，总分尚不成立。"
                % stats["blocking_open"],
            }
        )
    if stats["ordinary_pending"]:
        blocking.append(
            {
                "code": "pending_review",
                "message": "仍有 %d 项给分未经人工确认。" % stats["ordinary_pending"],
            }
        )

    total_papers = len(
        session.scalars(select(Paper).where(Paper.batch_id == batch.id)).all()
    )
    without_results = total_papers - selection.scored_count
    if without_results > 0:
        warnings.append(
            {
                "code": "materials_without_results",
                "message": "%d 份材料没有有效评分结果，将不计入成绩单。"
                % without_results,
            }
        )

    return {
        "batch_id": batch.id,
        "result_revision": selection.revision,
        # 正式成绩要求复核清零；报告与结构化审计导出允许在中间状态进行，
        # 只是会标注未完成——不能用新界面放宽旧服务已有的边界。
        "can_export_final": not blocking,
        "can_export_audit": True,
        "blocking": blocking,
        "warnings": warnings,
        "channels": {
            "html_report": True,
            "xlsx": True,
            "json": True,
            "sheets": sheets_available(),
        },
    }


def _legacy_entries(session, batch_id):
    rows = session.execute(
        select(SpreadsheetWriteLog)
        .join(ScoringRun, ScoringRun.id == SpreadsheetWriteLog.scoring_run_id)
        .join(Paper, Paper.id == ScoringRun.paper_id)
        .where(Paper.batch_id == batch_id)
    ).scalars().all()
    return [
        {
            "id": log.id,
            "source": "legacy_run_log",
            "channel": LEGACY_CHANNELS.get(log.target_type, log.target_type),
            "legacy_target_type": log.target_type,
            "scope": "run",
            "scoring_run_id": log.scoring_run_id,
            "status": log.status,
            "actor_id": None,
            # 旧表没有操作人字段；用 run.owner 顶上会把评分者写成导出者。
            "actor_display": "历史记录未记录操作人",
            "result_revision": None,
            "error_message": log.error_message,
            "created_at": log.created_at,
        }
        for log in rows
    ]


def _event_entries(session, batch_id):
    rows = session.scalars(
        select(ExportEvent).where(ExportEvent.grading_batch_id == batch_id)
    ).all()
    return [
        {
            "id": event.id,
            "source": "export_event",
            "channel": event.channel,
            "legacy_target_type": None,
            "scope": event.scope,
            "scoring_run_id": None,
            "status": event.status,
            "actor_id": event.actor_id,
            "actor_display": event.actor_id or "未知",
            "result_revision": event.result_revision,
            "error_message": event.error_message,
            "created_at": event.created_at,
        }
        for event in rows
    ]


def build_export_history(session, batch, *, limit=DEFAULT_LIMIT, cursor=None):
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    entries = _event_entries(session, batch.id) + _legacy_entries(session, batch.id)
    entries.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)

    start = int(cursor) if cursor is not None else 0
    start = max(0, min(start, len(entries)))
    page = entries[start : start + limit]
    next_cursor = start + limit if start + limit < len(entries) else None

    return {
        "batch_id": batch.id,
        "entries": page,
        "next_cursor": None if next_cursor is None else str(next_cursor),
    }


__all__ = [
    "build_export_precheck",
    "build_export_history",
    "sheets_available",
    "LEGACY_CHANNELS",
    "CHANNELS",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
]
