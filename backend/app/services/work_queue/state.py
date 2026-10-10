"""``work_runtime_state`` 的读写：最近一次全系统巡检、最近一次补投巡检链。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from backend.app.db.models import WorkRuntimeState


LAST_SWEEP = "sweep"
LAST_SWEEP_REVIVE = "sweep_revive"


def read_state(session, name):
    return session.scalar(select(WorkRuntimeState.value_at).where(WorkRuntimeState.name == name))


def record_state(session_factory, name, now) -> None:
    with session_factory() as session:
        changed = session.execute(
            update(WorkRuntimeState)
            .where(WorkRuntimeState.name == name)
            .values(value_at=now, updated_at=now)
        ).rowcount
        if not changed:
            session.add(WorkRuntimeState(name=name, value_at=now, updated_at=now))
        try:
            session.commit()
        except IntegrityError:
            # 两个进程同时写第一行：另一方已经写进去了，时间相同量级，不必重试。
            session.rollback()


def try_acquire(session_factory, name, now, *, min_interval_seconds) -> bool:
    """原子条件更新：距上次超过 ``min_interval_seconds`` 才返回 True 并记下本次时间。"""

    cutoff = now - timedelta(seconds=min_interval_seconds)
    with session_factory() as session:
        changed = session.execute(
            update(WorkRuntimeState)
            .where(
                WorkRuntimeState.name == name,
                (WorkRuntimeState.value_at.is_(None)) | (WorkRuntimeState.value_at < cutoff),
            )
            .values(value_at=now, updated_at=now)
        ).rowcount
        if changed:
            session.commit()
            return True
        exists = session.scalar(select(WorkRuntimeState.name).where(WorkRuntimeState.name == name))
        if exists is not None:
            session.rollback()
            return False
        session.add(WorkRuntimeState(name=name, value_at=now, updated_at=now))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return False
        return True


__all__ = ["LAST_SWEEP", "LAST_SWEEP_REVIVE", "read_state", "record_state", "try_acquire"]
