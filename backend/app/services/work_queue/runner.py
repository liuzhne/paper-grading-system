"""一次执行：领取一个条目 → 心跳 → 执行并落库 → 接力叫醒。worker 循环也在这里。"""

from __future__ import annotations

import logging
from threading import Condition
from threading import Event
from threading import Thread
from threading import get_ident
from time import monotonic

from sqlalchemy import update

from backend.app.db.models import utcnow
from backend.app.services.work_queue.claim import claim_next_item
from backend.app.services.work_queue.kinds import get_kind
from backend.app.services.work_queue.limits import HEARTBEAT_SECONDS
from backend.app.services.work_queue.limits import IDLE_POLL_MAX_SECONDS
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


def execute_next(session_factory, *, source_key=None, parent_id=None, on_claimed=None, **options):
    """领取并执行至多一个条目；没有可执行的条目时返回 None。

    ``on_claimed`` 在领到条目、开始执行之前调用：worker 用它叫醒其它空闲线程，
    不必等这一条执行完。
    """

    claimed = claim_next_item(session_factory, source_key, parent_id=parent_id)
    if claimed is None:
        return None
    if on_claimed is not None:
        on_claimed()
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


class IdleGate:
    """worker 空闲时的领取节奏：只让一条线程探测，连续领不到就把间隔翻倍到上限。

    任何线程领到条目，间隔复位为 ``poll_seconds``，并叫醒其它空闲线程一起领。原来每条
    线程各自每 ``poll_seconds`` 领一次：8 条线程空闲时每秒约 2.7 轮（每轮 BEGIN、两次
    查询、ROLLBACK），14 天 1200 万条语句，连接池出流量把 Supabase 免费额度用超了
    （RUNBOOK 排错 2026-10-27）。

    探测线程卡在领取里（例如数据库无响应）超过 ``takeover_seconds`` 没回来，等着的线程
    接手探测，不会因为一条线程卡住就没人领活。
    """

    def __init__(self, poll_seconds, max_seconds=IDLE_POLL_MAX_SECONDS, *, stop=None, takeover_seconds=None):
        self._poll = float(poll_seconds)
        self._max = max(float(max_seconds), self._poll)
        self._takeover = float(takeover_seconds) if takeover_seconds else 2 * self._max + 30
        self._stop = stop or Event()
        self._cond = Condition()
        self._interval = self._poll
        self._prober = None
        self._probes = 0
        self._generation = 0
        self._closed = False

    @property
    def interval(self) -> float:
        return self._interval

    def claimed(self):
        """领到了条目：间隔复位，叫醒所有空闲线程再领一次。"""

        with self._cond:
            self._interval = self._poll
            if self._prober == get_ident():
                self._prober = None
            self._generation += 1
            self._cond.notify_all()

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def idle(self):
        """领不到条目之后调用；返回时该再领一次（或已经停止）。"""

        me = get_ident()
        with self._cond:
            seen = self._generation

            def woken():
                return self._closed or self._stop.is_set() or self._generation != seen

            while not woken():
                if self._prober is None or self._prober == me:
                    self._prober = me
                    self._probes += 1
                    delay = self._interval
                    self._interval = min(self._interval * 2, self._max)
                    self._cond.wait_for(woken, timeout=delay)
                    return
                probes = self._probes
                if self._cond.wait_for(woken, timeout=self._takeover):
                    return
                if self._probes == probes:
                    logger.warning("work_idle_prober_stuck takeover_seconds=%s", self._takeover)
                    self._prober = None


def run_worker_loop(
    session_factory,
    *,
    poll_seconds=3.0,
    stop_event=None,
    threads=None,
    sweep_seconds=SWEEP_INTERVAL_SECONDS,
    drain_seconds=0.0,
    idle_max_seconds=IDLE_POLL_MAX_SECONDS,
):
    """内网与本地的叫醒层：``threads`` 个线程循环领取，主线程定期巡检。

    线程数即全局并发（默认与 Vercel 订阅相同，读 BATCH_SCORING_QUEUE_CONCURRENCY）；
    每个来源的同时请求数在领取时检查，多开 worker 也不会超过。空闲时由 ``IdleGate``
    只留一条线程探测，间隔从 ``poll_seconds`` 翻倍到 ``idle_max_seconds``。

    停止后各线程不再领取新条目；``drain_seconds`` 内等它们把手上的条目做完再返回。
    领取线程是守护线程，返回即进程退出：到时没做完的条目由租约与巡检续评。不等的
    代价是每次发版都可能给正在评的条目记一次“无进展”。
    """

    from backend.app.services.work_queue.sweep import run_global_sweep

    stop = stop_event or Event()
    count = max(1, int(threads or queue_concurrency()))
    gate = IdleGate(poll_seconds, idle_max_seconds, stop=stop)

    def lane():
        while not stop.is_set():
            if run_worker_cycle(session_factory, on_claimed=gate.claimed):
                continue
            gate.idle()

    lanes = [Thread(target=lane, name="pgs-work-%s" % index, daemon=True) for index in range(count)]
    for thread in lanes:
        thread.start()
    logger.info(
        "work_worker_started threads=%s sweep_seconds=%s idle_poll_seconds=%s-%s",
        count,
        sweep_seconds,
        poll_seconds,
        max(float(idle_max_seconds), float(poll_seconds)),
    )
    while not stop.is_set():
        try:
            run_global_sweep(session_factory)
        except Exception:
            logger.exception("work_sweep_failed")
        stop.wait(sweep_seconds)
    gate.close()
    # 不设 drain_seconds 时只等一个轮询周期多一点，让空闲线程干净退出（原有行为）。
    _drain(lanes, max(float(drain_seconds), poll_seconds + 1))


def _drain(lanes, seconds):
    deadline = monotonic() + seconds
    for thread in lanes:
        thread.join(timeout=max(0.0, deadline - monotonic()))
    running = sum(1 for thread in lanes if thread.is_alive())
    logger.info("work_worker_stopped drained=%s still_running=%s", len(lanes) - running, running)


__all__ = ["IdleGate", "execute_next", "heartbeat", "relay", "run_worker_cycle", "run_worker_loop"]
