"""Long-lived executor for durable batch scoring jobs."""

from __future__ import annotations

import logging
from threading import Event

from backend.app.services.batch_scoring.jobs import next_runnable_batch_scoring_job_id
from backend.app.services.batch_scoring.jobs import finalize_batch_scoring_job_failure
from backend.app.services.batch_scoring.jobs import run_batch_scoring_job


logger = logging.getLogger(__name__)


def run_worker_cycle(session_factory):
    """Run at most one queued or stale job and report whether work was found."""
    with session_factory() as session:
        job_id = next_runnable_batch_scoring_job_id(session)
    if job_id is None:
        return False
    try:
        run_batch_scoring_job(session_factory, job_id=job_id)
    except ValueError as exc:
        # Another worker may have acquired the same candidate after selection.
        logger.info("batch scoring candidate was not acquired: %s", exc)
    except Exception:
        # The persisted heartbeat makes an interrupted execution recoverable.
        # Keep the process alive so other queued jobs are not starved.
        logger.exception("batch scoring job failed outside an item checkpoint")
        try:
            finalize_batch_scoring_job_failure(session_factory, job_id=job_id)
        except Exception:
            logger.exception("failed to persist the batch scoring worker failure")
    return True


def run_worker_loop(session_factory, *, poll_seconds=3.0, stop_event=None):
    stop_event = stop_event or Event()
    while not stop_event.is_set():
        worked = run_worker_cycle(session_factory)
        if not worked:
            stop_event.wait(poll_seconds)
