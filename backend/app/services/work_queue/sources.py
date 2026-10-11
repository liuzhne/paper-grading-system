"""模型来源：来源键、名额上限、加锁与在跑统计。

厂商按 Key 限并发，所以名额按“模型来源”计算：私有连接按连接 ID，没有绑定连接的
任务共用平台默认模型的名额。批量评分条目与 AI 条目共用同一个来源的名额。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import text

from backend.app.db.models import AIConnection
from backend.app.services.ai_connections import connection_max_concurrency
from backend.app.services.work_queue.limits import ITEM_LEASE_SECONDS


PLATFORM_SOURCE_KEY = "platform"
CONNECTION_PREFIX = "connection:"


def source_key_for_connection(connection_id: str | None) -> str:
    return CONNECTION_PREFIX + connection_id if connection_id else PLATFORM_SOURCE_KEY


def connection_id_of(source_key: str) -> str | None:
    if source_key.startswith(CONNECTION_PREFIX):
        return source_key[len(CONNECTION_PREFIX):] or None
    return None


@dataclass(frozen=True)
class Source:
    key: str
    # 声明的同时请求数；没有声明时为 None，只受全局并发约束。
    limit: int | None
    lock_table: str | None
    lock_id: str | None


def resolve_source(session, source_key: str) -> Source:
    connection_id = connection_id_of(source_key)
    if connection_id is not None:
        options = session.scalar(
            select(AIConnection.provider_options).where(AIConnection.id == connection_id)
        )
        return Source(source_key, connection_max_concurrency(options), "ai_connections", connection_id)
    from backend.app.services import platform_llm

    config = platform_llm.get_active_config(session)
    if config is None:
        return Source(source_key, None, None, None)
    return Source(
        source_key,
        connection_max_concurrency(getattr(config, "provider_options", None)),
        "platform_llm_config",
        config.id,
    )


def lock_source(session, source: Source) -> None:
    """同一来源的领取在这里串行。

    Postgres 锁来源行；SQLite 没有行锁，用一次不改值的写入拿到库级写锁，效果相同
    （SQLite 同一时刻只有一个写事务）。直接写 SQL，不经 ORM，避免触发 updated_at。
    """

    if source.lock_table is None or source.lock_id is None:
        return
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT id FROM %s WHERE id = :id FOR UPDATE" % source.lock_table),
            {"id": source.lock_id},
        )
    else:
        session.execute(
            text("UPDATE %s SET id = id WHERE id = :id" % source.lock_table),
            {"id": source.lock_id},
        )


def lease_cutoff(now):
    return now - timedelta(seconds=ITEM_LEASE_SECONDS)


def running_count(session, source_key: str, now) -> int:
    """该来源在跑且租约有效的条目数（走 status = 'running' 的部分索引）。"""

    from backend.app.services.work_queue.kinds import all_kinds

    cutoff = lease_cutoff(now)
    total = 0
    for kind in all_kinds():
        model = kind.model
        total += int(
            session.scalar(
                select(func.count(model.id)).where(
                    model.source_key == source_key,
                    model.status == "running",
                    model.heartbeat_at > cutoff,
                )
            )
            or 0
        )
    return total


def ready_pending_count(session, source_key: str, now) -> int:
    from backend.app.services.work_queue.kinds import all_kinds

    total = 0
    for kind in all_kinds():
        model = kind.model
        total += int(
            session.scalar(
                select(func.count(model.id)).where(
                    model.source_key == source_key,
                    model.status == "pending",
                    (model.not_before.is_(None)) | (model.not_before <= now),
                )
            )
            or 0
        )
    return total


def global_concurrency() -> int:
    from backend.app.services.work_queue.wake import queue_concurrency

    return queue_concurrency()


def free_slots(session, source_key: str, now, *, source: Source | None = None) -> int:
    """还能再开始几个条目：声明了同时请求数按它算，否则按全局并发。"""

    source = source or resolve_source(session, source_key)
    capacity = source.limit if source.limit is not None else global_concurrency()
    return max(0, capacity - running_count(session, source_key, now))


__all__ = [
    "CONNECTION_PREFIX",
    "PLATFORM_SOURCE_KEY",
    "Source",
    "connection_id_of",
    "free_slots",
    "lease_cutoff",
    "lock_source",
    "ready_pending_count",
    "resolve_source",
    "running_count",
    "source_key_for_connection",
]
