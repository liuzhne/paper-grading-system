"""worker 空闲时的领取节奏（RUNBOOK 排错 2026-10-27）：只留一条线程探测、间隔翻倍到上限、
领到条目立刻叫醒其它线程；探测线程卡住时有人接手，停止时空闲线程及时退出。"""

from __future__ import annotations

from threading import Barrier
from threading import BrokenBarrierError
from threading import Event
from threading import Lock
from threading import Thread
from types import SimpleNamespace
import time

import pytest

from backend.app.services.work_queue import runner
from backend.app.services.work_queue import sweep
from backend.app.services.work_queue.runner import IdleGate


def _start_loop(monkeypatch, cycle, **kwargs):
    monkeypatch.setattr(runner, "run_worker_cycle", cycle)
    monkeypatch.setattr(sweep, "run_global_sweep", lambda _factory: None)
    stop = Event()
    options = {"poll_seconds": 0.02, "stop_event": stop, "threads": 8, "sweep_seconds": 0.01}
    options.update(kwargs)
    loop = Thread(target=runner.run_worker_loop, args=(None,), kwargs=options)
    loop.start()
    return stop, loop


def test_idle_interval_doubles_up_to_the_cap_and_resets_on_a_claim():
    gate = IdleGate(0.01, 0.04)
    seen = []
    for _ in range(4):
        gate.idle()
        seen.append(gate.interval)

    assert seen == [0.02, 0.04, 0.04, 0.04]
    gate.claimed()
    assert gate.interval == 0.01


def test_idle_worker_probes_from_one_thread_instead_of_every_thread(monkeypatch):
    calls = []
    lock = Lock()

    def cycle(_factory, **_options):
        with lock:
            calls.append(time.monotonic())
        return False

    # 不退避（上限等于间隔）也只有一条线程在探测：原来 8 条线程各自每 0.02 秒领一次，
    # 0.4 秒约 160 次；现在是开头每条线程各一次，之后每 0.02 秒一次，约 28 次。
    stop, loop = _start_loop(monkeypatch, cycle, idle_max_seconds=0.02)
    time.sleep(0.4)
    stop.set()
    loop.join(2)

    assert not loop.is_alive()
    assert 8 <= len(calls) < 60


def test_a_claim_wakes_the_other_idle_threads_without_waiting_for_their_turn(monkeypatch):
    pending = []
    lock = Lock()
    together = Barrier(4, timeout=2)
    done = []

    def cycle(_factory, on_claimed=None, **_options):
        with lock:
            if not pending:
                return False
            pending.pop()
        on_claimed()
        try:
            together.wait()  # 4 条同时在执行才能通过：其余 3 条必须被叫醒，而不是等 40 秒的接手
            done.append(True)
        except BrokenBarrierError:
            pass
        return True

    stop, loop = _start_loop(monkeypatch, cycle, threads=4, idle_max_seconds=0.5)
    time.sleep(0.3)  # 都空闲下来，只剩一条线程按退避后的间隔探测
    with lock:
        pending.extend(range(4))
    deadline = time.monotonic() + 3
    while len(done) < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    loop.join(2)

    assert len(done) == 4
    assert not loop.is_alive()


def test_waiting_thread_takes_over_when_the_prober_never_comes_back():
    gate = IdleGate(0.01, 0.01, takeover_seconds=0.1)
    gate.idle()  # 主线程成为探测线程，之后“卡在领取里”不再回来

    other = Thread(target=gate.idle)
    began = time.monotonic()
    other.start()
    other.join(2)

    assert not other.is_alive()
    assert 0.09 <= time.monotonic() - began < 1.5


def test_close_releases_idle_threads_at_once():
    gate = IdleGate(5, 5)
    waiter = Thread(target=gate.idle)
    waiter.start()
    time.sleep(0.05)
    began = time.monotonic()
    gate.close()
    waiter.join(1)

    assert not waiter.is_alive()
    assert time.monotonic() - began < 0.5


def test_worker_stops_promptly_while_idle_with_a_long_backoff(monkeypatch):
    stop, loop = _start_loop(monkeypatch, lambda _factory, **_options: False, poll_seconds=5, idle_max_seconds=30)
    time.sleep(0.1)
    began = time.monotonic()
    stop.set()
    loop.join(3)

    assert not loop.is_alive()
    assert time.monotonic() - began < 1.5


def test_execute_next_reports_the_claim_before_executing(monkeypatch):
    order = []
    claimed = SimpleNamespace(kind="fake", item_id="i1", attempt=1, source_key="s1")
    monkeypatch.setattr(runner, "claim_next_item", lambda *_args, **_kwargs: claimed)
    monkeypatch.setattr(
        runner,
        "get_kind",
        lambda _kind: SimpleNamespace(execute=lambda _factory, _claimed, **_options: order.append("execute")),
    )
    monkeypatch.setattr(runner, "relay", lambda *_args: 0)

    assert runner.execute_next(None, on_claimed=lambda: order.append("claimed")) is claimed
    assert order == ["claimed", "execute"]

    monkeypatch.setattr(runner, "claim_next_item", lambda *_args, **_kwargs: None)
    assert runner.execute_next(None, on_claimed=lambda: order.append("claimed")) is None
    assert order == ["claimed", "execute"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--poll-seconds", "3", "--idle-max-seconds", "1"],
        ["--idle-max-seconds", "nan"],
        ["--idle-max-seconds", "inf"],
        ["--poll-seconds", "nan"],
        ["--poll-seconds", "inf"],
    ],
)
def test_worker_cli_rejects_a_bad_idle_cap_or_poll_interval(argv):
    from backend.app.scripts import run_batch_worker

    with pytest.raises(SystemExit):
        run_batch_worker.main(argv)
