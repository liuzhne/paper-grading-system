"""巡检 ``sweep_stale_items``：找回卡住的条目、补发叫醒、收敛任务状态。

调用方：Vercel 上 2 分钟一次的自续期消息、进度接口（只针对本任务、限频）、worker
的巡检循环。租约过期的含义是“这次执行已死”，不是“这个条目失败了”：未达连续无进展
上限的条目重置为待处理后续评；达到上限的才标为失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging

from sqlalchemy import select
from sqlalchemy import update

from backend.app.db.models import utcnow
from backend.app.services.work_queue.claim import ready_sources
from backend.app.services.work_queue.kinds import all_kinds
from backend.app.services.work_queue.kinds import get_kind
from backend.app.services.work_queue.limits import PAGE_SWEEP_SECONDS
from backend.app.services.work_queue.limits import STALL_LIMIT
from backend.app.services.work_queue.limits import SWEEP_BATCH_LIMIT
from backend.app.services.work_queue.limits import SWEEP_INTERVAL_SECONDS
from backend.app.services.work_queue.sources import free_slots
from backend.app.services.work_queue.sources import lease_cutoff
from backend.app.services.work_queue.sources import ready_pending_count
from backend.app.services.work_queue.sources import running_count
from backend.app.services.work_queue.state import LAST_SWEEP
from backend.app.services.work_queue.state import LAST_SWEEP_REVIVE
from backend.app.services.work_queue.state import read_state
from backend.app.services.work_queue.state import record_state
from backend.app.services.work_queue.state import try_acquire
from backend.app.services.work_queue.wake import revive_sweep_chain
from backend.app.services.work_queue.wake import sweep_slot
from backend.app.services.work_queue.wake import wake_enabled
from backend.app.services.work_queue.wake import wake_source


logger = logging.getLogger("work-queue")


@dataclass
class SweepReport:
    recovered: int = 0
    failed: int = 0
    converged: int = 0
    rung: int = 0


def _stale(model, cutoff):
    return (model.heartbeat_at.is_(None)) | (model.heartbeat_at <= cutoff)


def _abandon_stale_item(session_factory, kind, item_id, now, cutoff):
    """一个已死的执行一个短事务；``SKIP LOCKED`` 跳过正在写结果的那次执行。"""

    model = kind.model
    with session_factory() as session:
        item = session.scalar(
            select(model)
            .where(model.id == item_id, model.status == "running", _stale(model, cutoff))
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if item is None:
            return None
        progressed = bool(kind.made_progress(session, item))
        stall_count = 0 if progressed else int(item.stall_count or 0) + 1
        exhausted = stall_count >= STALL_LIMIT
        item.stall_count = stall_count
        kind.abandon(session, item, now, progressed=progressed, exhausted=exhausted)
        session.commit()
    if exhausted:
        logger.warning(
            "work_item_failed kind=%s item_id=%s code=WORK_ITEM_STALLED stall_count=%s",
            kind.name,
            item_id,
            stall_count,
        )
    else:
        logger.info(
            "work_item_stalled kind=%s item_id=%s stall_count=%s progressed=%s",
            kind.name,
            item_id,
            stall_count,
            progressed,
        )
    return exhausted


def _wake_idle_sources(session_factory, now, *, parent_id=None) -> int:
    """有待处理、有空位、但没有在跑的来源：补发叫醒（消息丢了或接力断了）。"""

    rung = 0
    slot = sweep_slot(now)
    with session_factory() as session:
        for key in ready_sources(session, now, parent_id=parent_id):
            if running_count(session, key, now):
                continue
            count = min(free_slots(session, key, now), ready_pending_count(session, key, now))
            # 同一时间槽内全系统巡检与页面巡检得到同一个幂等键，不会重复叫醒。
            rung += wake_source(key, count, reason="sweep", token="%s-%s" % (slot, key))
    return rung


def sweep_stale_items(session_factory, *, parent_id=None, now=None, limit=SWEEP_BATCH_LIMIT, wake=True):
    """巡检全系统（``parent_id`` 为空）或一个任务；每次最多处理 ``limit`` 个已死的执行。"""

    now = now or utcnow()
    cutoff = lease_cutoff(now)
    report = SweepReport()
    remaining = limit
    for kind in all_kinds():
        if remaining <= 0:
            break
        model = kind.model
        with session_factory() as session:
            statement = (
                select(model.id)
                .where(model.status == "running", _stale(model, cutoff))
                .order_by(model.heartbeat_at, model.id)
                .limit(remaining)
            )
            if parent_id is not None:
                statement = statement.where(getattr(model, kind.parent_column) == parent_id)
            item_ids = list(session.scalars(statement))
        remaining -= len(item_ids)
        for item_id in item_ids:
            exhausted = _abandon_stale_item(session_factory, kind, item_id, now, cutoff)
            if exhausted is True:
                report.failed += 1
            elif exhausted is False:
                report.recovered += 1
    parent_ids = [parent_id] if parent_id is not None else None
    for kind in all_kinds():
        report.converged += kind.converge(
            session_factory, parent_ids=parent_ids, now=now, limit=limit
        )
    if wake and wake_enabled():
        report.rung = _wake_idle_sources(session_factory, now, parent_id=parent_id)
    return report


def run_global_sweep(session_factory, *, now=None):
    now = now or utcnow()
    report = sweep_stale_items(session_factory, now=now)
    record_state(session_factory, LAST_SWEEP, now)
    logger.info(
        "work_sweep_ran recovered=%s failed=%s converged=%s rung=%s",
        report.recovered,
        report.failed,
        report.converged,
        report.rung,
    )
    return report


def ensure_sweep_chain(session_factory, *, now=None) -> bool:
    """超过两个周期没巡检，说明 Vercel 上的巡检链断了：补投一条（每周期最多一次）。"""

    if not wake_enabled():
        return False
    now = now or utcnow()
    with session_factory() as session:
        last = read_state(session, LAST_SWEEP)
    if last is not None and last >= now - timedelta(seconds=2 * SWEEP_INTERVAL_SECONDS):
        return False
    if not try_acquire(
        session_factory, LAST_SWEEP_REVIVE, now, min_interval_seconds=SWEEP_INTERVAL_SECONDS
    ):
        return False
    return revive_sweep_chain(now)


def sweep_parent_on_read(session_factory, kind_name, parent_id, *, now=None):
    """进度读取时对本任务巡检：原子条件更新限频，每个任务 30 秒最多一次。"""

    kind = get_kind(kind_name)
    parent = kind.parent_model
    now = now or utcnow()
    cutoff = now - timedelta(seconds=PAGE_SWEEP_SECONDS)
    with session_factory() as session:
        changed = session.execute(
            update(parent)
            .where(
                parent.id == parent_id,
                (parent.last_swept_at.is_(None)) | (parent.last_swept_at < cutoff),
            )
            # 不能顺带刷新 updated_at：运维页按它判断任务是否停滞。
            .values(last_swept_at=now, updated_at=parent.updated_at)
        ).rowcount
        session.commit()
    report = sweep_stale_items(session_factory, parent_id=parent_id, now=now) if changed else None
    ensure_sweep_chain(session_factory, now=now)
    return report


__all__ = [
    "SweepReport",
    "ensure_sweep_chain",
    "run_global_sweep",
    "sweep_parent_on_read",
    "sweep_stale_items",
]
