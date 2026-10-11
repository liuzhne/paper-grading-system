"""一次执行：领取一个条目 → 心跳 → 执行并落库 → 接力叫醒。worker 循环也在这里。"""

from __future__ import annotations

import logging
from threading import Event
from threading import Thread

from sqlalchemy import update

from backend.app.db.models import utcnow
from backend.app.services.work_queue.claim import claim_next_item
from backend.app.services.work_queue.kinds import get_kind
from backend.app.services.work_queue.limits import HEARTBEAT_SECONDS
from backend.app.services.work_queue.limits import SWEEP_INTERVAL_SECONDS
from backend.app.services.work_queue.wake import queue_concurrency
from backend.app.services.work_queue.wake import wake_enabled
from backend.app.services.work_queue.wake import wake_for_capacity


logger = logging.getLogger("work-queue")


def heartbeat(session_factory, claimed) -> bool:
    """刷新这次执行的心跳；条目已被巡检重置或被另一次执行领走时返回 False。"""

    model = get_kind(claimed.kind).model
    with session_factory() as session:
        changed = session.execute(
            update(model)
            .where(
                model.id == claimed.item_id,
                model.status == "running",
                model.attempt_count == claimed.attempt,
            )
            .values(heartbeat_at=utcnow())
        ).rowcount
        session.commit()
    return bool(changed)


def _heartbeat_loop(session_factory, claimed, stop):
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            if not heartbeat(session_factory, claimed):
                logger.warning(
                    "work_item_lease_lost kind=%s item_id=%s attempt=%s",
                    claimed.kind,
                    claimed.item_id,
                    claimed.attempt,
                )
                return
        except Exception:
            # 一次心跳失败不终止执行；连续失败超过租约，巡检会把这次执行当作已死。
            logger.exception("work_item_heartbeat_failed item_id=%s", claimed.item_id)


def relay(session_factory, claimed) -> int:
    """执行结束后：该来源还有待处理条目且有空位，就再叫醒（Vercel 上的接力）。"""

    if not wake_enabled():
        return 0
    try:
        with session_factory() as session:
            return wake_for_capacity(
                session,
                claimed.source_key,
                reason="relay",
                token="%s-%s" % (claimed.item_id, claimed.attempt),
            )
    except Exception:
        logger.exception("work_relay_failed source=%s", claimed.source_key)
        return 0


def execute_next(session_factory, *, source_key=None, parent_id=None, **options):
    """领取并执行至多一个条目；没有可执行的条目时返回 None。"""

    claimed = claim_next_item(session_factory, source_key, parent_id=parent_id)
    if claimed is None:
        return None
    kind = get_kind(claimed.kind)
    stop = Event()
    beat = Thread(target=_heartbeat_loop, args=(session_factory, claimed, stop), daemon=True)
    beat.start()
    try:
        kind.execute(session_factory, claimed, **options)
    finally:
        stop.set()
        beat.join(timeout=1)
    relay(session_factory, claimed)
    return claimed


def run_worker_cycle(session_factory, **options) -> bool:
    """worker 的一轮：领取并执行一个条目，报告是否取到了活。"""

    try:
        return execute_next(session_factory, **options) is not None
    except Exception:
        # 落库前的异常（例如数据库断开）不能让线程退出；这次执行若已领取，
        # 心跳停止后由巡检按“连续无进展”处理。
        logger.exception("work_cycle_failed")
        return False


def run_worker_loop(
    session_factory,
    *,
    poll_seconds=3.0,
    stop_event=None,
    threads=None,
    sweep_seconds=SWEEP_INTERVAL_SECONDS,
):
    """内网与本地的叫醒层：``threads`` 个线程各自循环领取，主线程定期巡检。

    线程数即全局并发（默认与 Vercel 订阅相同，读 BATCH_SCORING_QUEUE_CONCURRENCY）；
    每个来源的同时请求数在领取时检查，多开 worker 也不会超过。
    """

    from backend.app.services.work_queue.sweep import run_global_sweep

    stop = stop_event or Event()
    count = max(1, int(threads or queue_concurrency()))

    def lane():
        while not stop.is_set():
            if not run_worker_cycle(session_factory):
                stop.wait(poll_seconds)

    lanes = [Thread(target=lane, name="pgs-work-%s" % index, daemon=True) for index in range(count)]
    for thread in lanes:
        thread.start()
    logger.info("work_worker_started threads=%s sweep_seconds=%s", count, sweep_seconds)
    while not stop.is_set():
        try:
            run_global_sweep(session_factory)
        except Exception:
            logger.exception("work_sweep_failed")
        stop.wait(sweep_seconds)
    for thread in lanes:
        thread.join(timeout=poll_seconds + 1)


__all__ = ["execute_next", "heartbeat", "relay", "run_worker_cycle", "run_worker_loop"]
