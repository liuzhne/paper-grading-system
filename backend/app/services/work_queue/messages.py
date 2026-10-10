"""叫醒消息的处理：与平台无关的那一半（Vercel 订阅只负责把消息交到这里）。"""

from __future__ import annotations

import logging

from sqlalchemy import select

from backend.app.db.models import utcnow
from backend.app.services.work_queue.kinds import all_kinds


logger = logging.getLogger("work-queue")


def _legacy_source(session_factory, item_id):
    with session_factory() as session:
        for kind in all_kinds():
            key = session.scalar(select(kind.model.source_key).where(kind.model.id == item_id))
            if key:
                return key
    return None


def handle_wake_payload(session_factory, payload):
    """``{source_key}`` 执行一个条目；``{sweep}`` 先投下一条巡检再巡检；
    旧格式 ``{job_id, item_id}`` 叫醒该条目所属的来源。"""

    from backend.app.services.work_queue.runner import execute_next
    from backend.app.services.work_queue.sweep import run_global_sweep
    from backend.app.services.work_queue.wake import schedule_next_sweep

    if not isinstance(payload, dict):
        raise ValueError("work message must be an object")
    if "sweep" in payload:
        now = utcnow()
        # 先续期再巡检：巡检失败被平台重投时，同一个幂等键不会多出一条链。
        schedule_next_sweep(now)
        return run_global_sweep(session_factory, now=now)
    source_key = payload.get("source_key")
    if not isinstance(source_key, str) or not source_key:
        item_id = payload.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("work message requires source_key")
        source_key = _legacy_source(session_factory, item_id)
        logger.info("work_legacy_message item_id=%s source=%s", item_id, source_key)
        if source_key is None:
            return None
    claimed = execute_next(session_factory, source_key=source_key)
    if claimed is None:
        logger.info("work_wake_idle source=%s", source_key)
    return claimed


__all__ = ["handle_wake_payload"]
