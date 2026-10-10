"""Run the work-queue worker (batch scoring and AI tasks) outside HTTP requests.

内网与本地的叫醒层：``--threads`` 个线程循环调用与 Vercel 相同的领取函数，主线程每
``--sweep-seconds`` 巡检一次（找回已死的执行、收敛任务状态）。
"""

import argparse
import logging
import signal
from threading import Event

from backend.app.db.session import SessionLocal
from backend.app.services.work_queue.limits import SWEEP_INTERVAL_SECONDS
from backend.app.services.work_queue.runner import run_worker_loop
from backend.app.services.work_queue.wake import queue_concurrency


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help="同时执行的条目数（默认读 BATCH_SCORING_QUEUE_CONCURRENCY，未设置为 8）",
    )
    parser.add_argument("--sweep-seconds", type=float, default=float(SWEEP_INTERVAL_SECONDS))
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    if args.sweep_seconds <= 0:
        parser.error("--sweep-seconds must be positive")
    if args.threads is not None and args.threads < 1:
        parser.error("--threads must be at least 1")

    logging.basicConfig(level=logging.INFO)
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
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
