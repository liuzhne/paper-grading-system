"""Run the durable batch-scoring worker outside the HTTP request lifecycle."""

import argparse
import logging
import signal
from threading import Event

from backend.app.db.session import SessionLocal
from backend.app.services.batch_scoring.worker import run_worker_loop


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")

    logging.basicConfig(level=logging.INFO)
    stop = Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run_worker_loop(SessionLocal, poll_seconds=args.poll_seconds, stop_event=stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
