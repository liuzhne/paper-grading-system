"""Run the work-queue worker (batch scoring and AI tasks) outside HTTP requests.

内网与本地的叫醒层：``--threads`` 个线程循环调用与 Vercel 相同的领取函数，主线程每
``--sweep-seconds`` 巡检一次（找回已死的执行、收敛任务状态）。空闲时只留一条线程探测，
间隔从 ``--poll-seconds`` 翻倍到 ``--idle-max-seconds``。收到 SIGTERM 后不再领取，
最多等 ``--drain-seconds`` 让在执行的条目做完。
"""

import argparse
import math
import signal
from threading import Event

from backend.app.core.config import settings
from backend.app.core.log_config import configure_logging
from backend.app.db.session import SessionLocal
from backend.app.services.work_queue.limits import IDLE_POLL_MAX_SECONDS
from backend.app.services.work_queue.limits import SWEEP_INTERVAL_SECONDS
from backend.app.services.work_queue.runner import run_worker_loop
from backend.app.services.work_queue.wake import queue_concurrency


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument(
        "--idle-max-seconds",
        type=float,
        default=float(IDLE_POLL_MAX_SECONDS),
        help="空闲时只留一条线程探测，间隔从 --poll-seconds 翻倍到这里（等于 --poll-seconds 即不退避）",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help="同时执行的条目数（默认读 BATCH_SCORING_QUEUE_CONCURRENCY，未设置为 8）",
    )
    parser.add_argument("--sweep-seconds", type=float, default=float(SWEEP_INTERVAL_SECONDS))
    parser.add_argument(
        "--drain-seconds",
        type=float,
        default=0.0,
        help="收到停止信号后最多等多少秒让在执行的条目做完（生产 compose 传 100；默认不等）",
    )
    args = parser.parse_args(argv)
    # float() 收 nan 与 inf；nan 与任何数比较都是 False，会绕过下面的大小检查。
    if not math.isfinite(args.poll_seconds) or args.poll_seconds <= 0:
        parser.error("--poll-seconds must be a positive finite number")
    if not math.isfinite(args.idle_max_seconds) or args.idle_max_seconds < args.poll_seconds:
        parser.error("--idle-max-seconds must be a finite number not less than --poll-seconds")
    if args.sweep_seconds <= 0:
        parser.error("--sweep-seconds must be positive")
    if args.threads is not None and args.threads < 1:
        parser.error("--threads must be at least 1")
    if args.drain_seconds < 0:
        parser.error("--drain-seconds must not be negative")

    configure_logging(settings.LOG_LEVEL)
    stop = Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run_worker_loop(
        SessionLocal,
        poll_seconds=args.poll_seconds,
        stop_event=stop,
        threads=args.threads or queue_concurrency(),
        sweep_seconds=args.sweep_seconds,
        drain_seconds=args.drain_seconds,
        idle_max_seconds=args.idle_max_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
