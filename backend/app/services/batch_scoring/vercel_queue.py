"""Vercel Queues transport for durable, per-paper scoring checkpoints."""

from __future__ import annotations

import asyncio
import logging
import os
from vercel.queue import send
from vercel.queue import subscribe

SCORING_TOPIC = "batch-scoring-items"
logger = logging.getLogger("batch-scoring-queue")


def vercel_queue_enabled() -> bool:
    """Use push delivery on Vercel, with an explicit local override for tests."""
    configured = os.getenv("BATCH_SCORING_DISPATCH", "").strip().lower()
    if configured:
        return configured == "vercel_queue"
    return bool(os.getenv("VERCEL"))


async def dispatch_batch_scoring_job(job) -> list[str | None]:
    """Publish every pending item; idempotency makes redispatch safe."""
    if not vercel_queue_enabled():
        return []
    pending = [item for item in job.items if item.status == "pending"]
    results = []
    for item in pending:
        message_id = await send(
            SCORING_TOPIC,
            {"job_id": job.id, "item_id": item.id},
            idempotency_key=f"score-{item.id}-{item.attempt_count}",
            retention=86400,
        )
        results.append(str(message_id) if message_id is not None else None)
    logger.info(
        "batch_scoring_dispatched job_id=%s item_count=%s",
        job.id,
        len(pending),
    )
    return results


@subscribe(
    topic=SCORING_TOPIC,
    consumer_group="paper-grading-production",
    retry_after=30,
    max_concurrency=2,
    max_attempts=12,
)
async def score_batch_item(payload) -> None:
    """Consume one paper so a whole batch never occupies one function lifetime."""
    # Vercel imports subscriber modules during build-time discovery.  Defer the
    # application/runtime imports until an actual delivery so discovery stays
    # independent of native database and Pydantic wheels.
    from backend.app.db.session import SessionLocal
    from backend.app.services.batch_scoring.jobs import run_batch_scoring_item

    job_id = payload.get("job_id")
    item_id = payload.get("item_id")
    if not isinstance(job_id, str) or not isinstance(item_id, str):
        raise ValueError("queue message requires job_id and item_id")
    logger.info(
        "batch_scoring_item_started job_id=%s item_id=%s",
        job_id,
        item_id,
    )
    await asyncio.to_thread(
        run_batch_scoring_item,
        SessionLocal,
        job_id=job_id,
        item_id=item_id,
    )
    logger.info(
        "batch_scoring_item_finished job_id=%s item_id=%s",
        job_id,
        item_id,
    )


__all__ = [
    "SCORING_TOPIC",
    "dispatch_batch_scoring_job",
    "score_batch_item",
    "vercel_queue_enabled",
]
