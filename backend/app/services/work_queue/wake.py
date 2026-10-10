"""叫醒：执行模型里唯一与部署平台相关的一层。

Vercel 上没有常驻进程，靠订阅 ``pgs-work`` 的叫醒消息拉起函数；消息只带来源键，
不带任务或条目信息，重复无害（取不到条目就退出），丢失由巡检补发。内网与本地由
worker 循环自己领取，这里的发送全部是空操作。
"""

from __future__ import annotations

from datetime import timezone
import logging
from math import ceil
import re
from uuid import uuid4

from backend.app.services.batch_scoring.vercel_queue import WORK_TOPIC
from backend.app.services.batch_scoring.vercel_queue import queue_concurrency
from backend.app.services.batch_scoring.vercel_queue import vercel_queue_enabled
from backend.app.services.work_queue.limits import SWEEP_INTERVAL_SECONDS


MESSAGE_RETENTION_SECONDS = 86400
logger = logging.getLogger("work-queue")
_UNSAFE_KEY = re.compile(r"[^A-Za-z0-9_-]+")


def wake_enabled() -> bool:
    return vercel_queue_enabled()


def _send(payload, *, idempotency_key, delay=None):
    # 同步客户端：叫醒都在工作线程里发出（路由用线程池、消费函数用 to_thread）。
    from vercel.queue.sync import send

    return send(
        WORK_TOPIC,
        payload,
        idempotency_key=idempotency_key,
        retention=MESSAGE_RETENTION_SECONDS,
        delay=delay,
    )


def _key(*parts) -> str:
    return "-".join(_UNSAFE_KEY.sub("_", str(part)) for part in parts)[:200]


def wake_source(source_key: str, count: int, *, reason: str, token: str | None = None, delay=None) -> int:
    """为一个来源发 ``count`` 条叫醒消息；返回实际发出的条数。

    ``token`` 让同一事件的重放（例如消费函数被平台重试）得到同一个幂等键而被去重。
    """

    if count <= 0 or not wake_enabled():
        return 0
    token = token or uuid4().hex
    sent = 0
    for index in range(count):
        try:
            _send(
                {"source_key": source_key},
                idempotency_key=_key("wake", reason, token, index),
                delay=delay,
            )
        except Exception:
            # 叫醒丢了不影响条目状态：巡检会给“有待处理、有空位、没人在跑”的来源补发。
            logger.exception("work_wake_failed source=%s reason=%s", source_key, reason)
            break
        sent += 1
    logger.info(
        "work_rung source=%s reason=%s count=%s delay=%s",
        source_key,
        reason,
        sent,
        delay or 0,
    )
    return sent


def wake_for_capacity(session, source_key: str, *, reason: str, token: str | None = None, now=None) -> int:
    """按该来源的空位与可执行条目数叫醒：建任务、接力、巡检共用。"""

    if not wake_enabled():
        return 0
    from backend.app.db.models import utcnow
    from backend.app.services.work_queue.sources import free_slots
    from backend.app.services.work_queue.sources import ready_pending_count

    now = now or utcnow()
    count = min(free_slots(session, source_key, now), ready_pending_count(session, source_key, now))
    return wake_source(source_key, count, reason=reason, token=token)


def _epoch(now) -> float:
    # utcnow() 是不带时区的 UTC；直接 .timestamp() 会按本机时区换算。
    return now.replace(tzinfo=timezone.utc).timestamp()


def sweep_slot(now) -> int:
    return int(_epoch(now)) // SWEEP_INTERVAL_SECONDS


def schedule_next_sweep(now) -> int:
    """投下一条巡检消息，定在下一个时间槽的开头。

    幂等键按槽号生成：同一时刻存在的多条巡检链会在下一个槽合成一条。
    """

    next_slot = sweep_slot(now) + 1
    delay = max(1, ceil(next_slot * SWEEP_INTERVAL_SECONDS - _epoch(now)) + 1)
    _send({"sweep": next_slot}, idempotency_key=_key("sweep", next_slot), delay=delay)
    logger.info("work_sweep_scheduled slot=%s delay=%s", next_slot, delay)
    return next_slot


def revive_sweep_chain(now) -> bool:
    """巡检链断了（消息被丢弃、首次部署）时补投一条立即执行的巡检消息。"""

    slot = sweep_slot(now)
    try:
        _send({"sweep": slot}, idempotency_key=_key("sweep", "revive", slot))
    except Exception:
        logger.exception("work_sweep_revive_failed slot=%s", slot)
        return False
    logger.info("work_sweep_revived slot=%s", slot)
    return True


__all__ = [
    "WORK_TOPIC",
    "queue_concurrency",
    "revive_sweep_chain",
    "schedule_next_sweep",
    "sweep_slot",
    "wake_enabled",
    "wake_for_capacity",
    "wake_source",
]
