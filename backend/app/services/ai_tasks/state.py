"""AI 任务行的加锁与计数：与批量评分任务相同的做法（增量更新，收敛时重新计数）。"""

from __future__ import annotations

from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import selectinload

from backend.app.db.models import AITask
from backend.app.db.models import AITaskItem


ITEM_STATUSES = ("pending", "running", "succeeded", "failed", "canceled")
ACTIVE_TASK_STATUSES = ("queued", "running")
TERMINAL_TASK_STATUSES = ("succeeded", "failed", "canceled", "superseded")


def lock_task(session, task_id, *, with_items=False, skip_locked=False):
    statement = (
        select(AITask)
        .where(AITask.id == task_id)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
    if with_items:
        statement = statement.options(selectinload(AITask.items))
    return session.scalar(statement)


def shift_counts(session, task_id, **deltas):
    values = {}
    for status, delta in deltas.items():
        if not delta:
            continue
        column = getattr(AITask, "%s_count" % status)
        values[column] = case((column + delta < 0, 0), else_=column + delta)
    if values:
        values[AITask.state_version] = AITask.state_version + 1
        session.execute(
            update(AITask)
            .where(AITask.id == task_id)
            .values(values)
            .execution_options(synchronize_session=False)
        )


def recount(session, task_id):
    """按条目重新计数；没变就不写（不刷新 updated_at）。"""

    counts = {status: 0 for status in ITEM_STATUSES}
    for status, value in session.execute(
        select(AITaskItem.status, func.count(AITaskItem.id))
        .where(AITaskItem.task_id == task_id)
        .group_by(AITaskItem.status)
    ):
        counts[status] = int(value)
    values = {
        "total_items": sum(counts.values()),
        **{"%s_count" % status: value for status, value in counts.items()},
    }
    current = session.execute(
        select(*(getattr(AITask, name) for name in values)).where(AITask.id == task_id)
    ).one_or_none()
    if current is not None and tuple(current) != tuple(values.values()):
        session.execute(
            update(AITask)
            .where(AITask.id == task_id)
            .values({**values, "state_version": AITask.state_version + 1})
            .execution_options(synchronize_session=False)
        )
    return counts


def cancel_pending_items(session, task_id, now):
    return session.execute(
        update(AITaskItem)
        .where(AITaskItem.task_id == task_id, AITaskItem.status == "pending")
        .values(status="canceled", finished_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount


__all__ = [
    "ACTIVE_TASK_STATUSES",
    "ITEM_STATUSES",
    "TERMINAL_TASK_STATUSES",
    "cancel_pending_items",
    "lock_task",
    "recount",
    "shift_counts",
]
