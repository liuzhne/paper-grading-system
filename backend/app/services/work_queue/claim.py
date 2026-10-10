"""``claim_next_item``：全系统唯一的取活入口。

一个短事务里完成：锁来源行并统计在跑条目（满了就返回“无空位”）→ 用
``FOR UPDATE SKIP LOCKED`` 取下一个待处理条目 → 置为 running 并写心跳。
顺序：AI 条目优先；同类内按任务内序号轮转（所有任务的第 1 个条目先执行，再执行
各自的第 2 个），再按创建时间。
"""

from __future__ import annotations

import logging

from sqlalchemy import func
from sqlalchemy import select

from backend.app.db.models import utcnow
from backend.app.services.work_queue.kinds import all_kinds
from backend.app.services.work_queue.limits import CLAIM_CANDIDATE_LIMIT
from backend.app.services.work_queue.sources import lock_source
from backend.app.services.work_queue.sources import resolve_source
from backend.app.services.work_queue.sources import running_count


logger = logging.getLogger("work-queue")


def _ready(model, now):
    return (model.not_before.is_(None)) | (model.not_before <= now)


def ready_sources(session, now, *, kinds=None, parent_id=None) -> list[str]:
    """有可执行条目的来源，按种类优先级、再按最靠前的序号排列（worker 用）。"""

    ordered: list[str] = []
    for kind in kinds or all_kinds():
        model = kind.model
        statement = (
            select(model.source_key)
            .where(model.status == "pending", _ready(model, now))
            .group_by(model.source_key)
            .order_by(func.min(model.ordinal), func.min(model.created_at), model.source_key)
        )
        if parent_id is not None:
            statement = statement.where(getattr(model, kind.parent_column) == parent_id)
        for key in session.scalars(statement):
            if key not in ordered:
                ordered.append(key)
    return ordered


def _claim_from_source(session, source_key, now, kinds, parent_id):
    source = resolve_source(session, source_key)
    if source.limit is not None:
        lock_source(session, source)
        running = running_count(session, source_key, now)
        if running >= source.limit:
            logger.info(
                "work_source_full source=%s running=%s limit=%s",
                source_key,
                running,
                source.limit,
            )
            return None
    for kind in kinds:
        model = kind.model
        for _ in range(CLAIM_CANDIDATE_LIMIT):
            statement = (
                select(model)
                .where(
                    model.source_key == source_key,
                    model.status == "pending",
                    _ready(model, now),
                )
                .order_by(model.ordinal, model.created_at, model.id)
                .limit(1)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if parent_id is not None:
                statement = statement.where(getattr(model, kind.parent_column) == parent_id)
            item = session.scalar(statement)
            if item is None:
                break
            claimed = kind.begin(session, item, now)
            if claimed is not None:
                return claimed
    return None


def claim_next_item(session_factory, source_key=None, *, kinds=None, parent_id=None, now=None):
    """取下一个条目并置为 running；没有可执行的条目或来源已满时返回 None。

    ``source_key`` 为空时（worker）依次尝试所有有待处理条目的来源。``parent_id``
    只把领取限制在一个任务内（同步执行入口与测试用），名额检查不变。
    """

    kinds = list(kinds or all_kinds())
    with session_factory() as session:
        now = now or utcnow()
        keys = [source_key] if source_key else ready_sources(
            session, now, kinds=kinds, parent_id=parent_id
        )
        # 列来源的只读查询不该占着连接上的事务，否则下面每个来源的锁会叠在一起。
        session.rollback()
        for key in keys:
            claimed = _claim_from_source(session, key, now, kinds, parent_id)
            if claimed is not None:
                session.commit()
                logger.info(
                    "work_item_claimed kind=%s item_id=%s parent_id=%s source=%s attempt=%s",
                    claimed.kind,
                    claimed.item_id,
                    claimed.parent_id,
                    claimed.source_key,
                    claimed.attempt,
                )
                return claimed
            # 释放来源锁，再试下一个来源；begin 里取消掉的条目同样随之提交。
            session.commit()
    return None


__all__ = ["claim_next_item", "ready_sources"]
