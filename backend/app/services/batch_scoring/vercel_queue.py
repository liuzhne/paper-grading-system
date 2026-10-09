"""Vercel Queues transport for durable, per-paper scoring checkpoints."""

from __future__ import annotations

import asyncio
import logging
import os
from uuid import uuid4
from vercel.queue import send
from vercel.queue import subscribe

SCORING_TOPIC = "batch-scoring-items"
# 全系统同时评分的论文数（所有用户共用一个消费组）。每个连接另有自己的名额检查
# （_ensure_connection_capacity），限额小的连接不会因为这里调高而超限。
DEFAULT_QUEUE_CONCURRENCY = 8
MAX_QUEUE_CONCURRENCY = 32


def queue_concurrency() -> int:
    """读取 BATCH_SCORING_QUEUE_CONCURRENCY。在模块导入（构建期发现订阅）时求值，
    不依赖应用配置，保持发现阶段不加载数据库与 Pydantic。"""

    raw = os.getenv("BATCH_SCORING_QUEUE_CONCURRENCY", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_QUEUE_CONCURRENCY
    except ValueError:
        value = DEFAULT_QUEUE_CONCURRENCY
    return max(1, min(MAX_QUEUE_CONCURRENCY, value))
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
    max_concurrency=queue_concurrency(),
    max_attempts=12,
)
async def score_batch_item(payload) -> None:
    """Consume one paper so a whole batch never occupies one function lifetime."""
    # Vercel imports subscriber modules during build-time discovery.  Defer the
    # application/runtime imports until an actual delivery so discovery stays
    # independent of native database and Pydantic wheels.
    from backend.app.db.session import SessionLocal
    from backend.app.services.batch_scoring.jobs import ConnectionAtCapacityError
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
    try:
        await asyncio.to_thread(
            run_batch_scoring_item,
            SessionLocal,
            job_id=job_id,
            item_id=item_id,
        )
    except ConnectionAtCapacityError as exc:
        # 连接并发名额已满：另投一条延迟消息，再正常返回确认这一条。若靠抛错让队列
        # 重投，排队等待会耗尽 max_attempts，消息被静默丢弃，任务卡在排队中。
        # 幂等键每次唯一：同键会被服务端去重，延迟消息一旦被吞掉，这篇论文就再没人领。
        await send(
            SCORING_TOPIC,
            {"job_id": job_id, "item_id": item_id},
            idempotency_key=f"score-{item_id}-wait-{uuid4().hex}",
            retention=86400,
            delay=exc.retry_after_seconds,
        )
        logger.info(
            "batch_scoring_item_deferred job_id=%s item_id=%s delay_seconds=%s",
            job_id,
            item_id,
            exc.retry_after_seconds,
        )
        return
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
