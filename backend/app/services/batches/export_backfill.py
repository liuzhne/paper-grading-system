"""旧导出日志的幂等补录（前端 v2 计划 §5-F，阶段 6B）。

放在**可调用的服务**里而不是迁移里，因为 ``alembic upgrade head`` 不会重跑
已完成的迁移：回退窗口内旧应用只写 `SpreadsheetWriteLog`，重新前进时必须能
再执行一次把这段补上。

三条硬约束：

1. **幂等。** 按 ``legacy_log_id`` 唯一约束跳过已补录的行。
2. **不补造操作人。** 旧表没有该字段，留空。拿 ``run.owner`` 顶会把评分的
   所有者写成点导出的人。
3. **不合并。** 一份 v1 批次 xlsx 可能对应多条 run 日志；按路径或时间并成
   一次「批次导出」是在写一个从未发生过的事件。

遇到未登记的 ``target_type`` 直接终止：整批停下，比默默写入一个猜出来的
映射好——后者会让历史看起来完整，实则失真。
"""

from __future__ import annotations

from sqlalchemy import select

from backend.app.db.models import ExportEvent
from backend.app.db.models import Paper
from backend.app.db.models import ScoringRun
from backend.app.db.models import SpreadsheetWriteLog
from backend.app.services.batches.exports import LEGACY_CHANNELS


def backfill_legacy_export_logs(session, *, limit=None):
    """把尚未补录的旧日志映射成 ExportEvent。

    :returns: ``{"created": int, "skipped": int, "scanned": int}``
    """
    done = set(
        session.scalars(
            select(ExportEvent.legacy_log_id).where(
                ExportEvent.legacy_log_id.is_not(None)
            )
        ).all()
    )

    query = (
        select(SpreadsheetWriteLog, Paper.batch_id, Paper.organization_id)
        .join(ScoringRun, ScoringRun.id == SpreadsheetWriteLog.scoring_run_id)
        .outerjoin(Paper, Paper.id == ScoringRun.paper_id)
        .order_by(SpreadsheetWriteLog.created_at, SpreadsheetWriteLog.id)
    )
    if limit:
        query = query.limit(limit)

    created = skipped = scanned = 0
    for log, batch_id, organization_id in session.execute(query).all():
        scanned += 1
        if log.id in done:
            skipped += 1
            continue

        channel = LEGACY_CHANNELS.get(log.target_type)
        if channel is None:
            raise ValueError(
                "未登记的历史导出通道 %r（日志 %s）；补录已终止。"
                "请先确认该通道应映射到哪个展示通道，再重新执行。"
                % (log.target_type, log.id)
            )

        session.add(
            ExportEvent(
                organization_id=organization_id,
                grading_batch_id=batch_id,
                channel=channel,
                scope="run",
                result_revision=None,
                status=log.status,
                # 旧表没有操作人字段。留空，由界面显示「历史记录未记录」。
                actor_id=None,
                error_message=log.error_message,
                legacy_log_id=log.id,
                created_at=log.created_at,
            )
        )
        created += 1

    return {"created": created, "skipped": skipped, "scanned": scanned}


__all__ = ["backfill_legacy_export_logs"]
