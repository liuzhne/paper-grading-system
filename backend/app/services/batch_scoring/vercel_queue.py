"""Vercel Queues：执行模型在 Vercel 上的叫醒层。

数据库是唯一的事实来源。订阅 ``pgs-work`` 的消息只带来源键（``{source_key}``）或巡检
槽号（``{sweep}``），不带任务或条目：被叫醒的函数调用 ``claim_next_item`` 领取一个
条目执行，取不到就退出。消息重复无害，丢失由巡检补发，所以队列的重投次数与条目
状态无关——执行次数由数据库按“连续无进展”计数。

旧主题 ``batch-scoring-items`` 的消费者保留一个发布周期：部署时队列里可能还有旧格式
消息 ``{job_id, item_id}``，按“叫醒该条目所属来源”处理。

本模块在 Vercel 构建期被导入以发现订阅，顶层只能依赖标准库与 vercel.queue。
"""

from __future__ import annotations

import asyncio
import logging
import os

from vercel.queue import subscribe


WORK_TOPIC = "pgs-work"
# 统一执行模型之前一篇一条消息的主题；只为消费部署前留下的旧消息。
SCORING_TOPIC = "batch-scoring-items"
CONSUMER_GROUP = "paper-grading-production"
# 全系统同时执行的条目数（所有用户共用一个消费组）。每个来源另有自己的名额检查，
# 限额小的连接不会因为这里调高而超限。worker 的线程数读同一个变量。
DEFAULT_QUEUE_CONCURRENCY = 8
MAX_QUEUE_CONCURRENCY = 32
logger = logging.getLogger("batch-scoring-queue")


def queue_concurrency() -> int:
    """读取 BATCH_SCORING_QUEUE_CONCURRENCY。在模块导入（构建期发现订阅）时求值，
    不依赖应用配置，保持发现阶段不加载数据库与 Pydantic。"""

    raw = os.getenv("BATCH_SCORING_QUEUE_CONCURRENCY", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_QUEUE_CONCURRENCY
    except ValueError:
        value = DEFAULT_QUEUE_CONCURRENCY
    return max(1, min(MAX_QUEUE_CONCURRENCY, value))


def vercel_queue_enabled() -> bool:
    """Use push delivery on Vercel, with an explicit local override for tests."""
    configured = os.getenv("BATCH_SCORING_DISPATCH", "").strip().lower()
    if configured:
        return configured == "vercel_queue"
    return bool(os.getenv("VERCEL"))


async def _handle(payload) -> None:
    # Vercel imports subscriber modules during build-time discovery.  Defer the
    # application/runtime imports until an actual delivery so discovery stays
    # independent of native database and Pydantic wheels.
    from backend.app.db.session import SessionLocal
    from backend.app.services.work_queue.messages import handle_wake_payload

    await asyncio.to_thread(handle_wake_payload, SessionLocal, payload)


@subscribe(
    topic=WORK_TOPIC,
    consumer_group=CONSUMER_GROUP,
    retry_after=30,
    max_concurrency=queue_concurrency(),
    max_attempts=3,
)
async def handle_work_message(payload) -> None:
    """一次叫醒执行一个条目（或一次巡检）；结束前按空位接力叫醒。"""

    await _handle(payload)


@subscribe(
    topic=SCORING_TOPIC,
    consumer_group=CONSUMER_GROUP,
    retry_after=30,
    max_concurrency=queue_concurrency(),
    max_attempts=12,
)
async def score_batch_item(payload) -> None:
    """旧格式消息：按“叫醒该条目所属来源”处理，不再按消息里的条目执行。"""

    await _handle(payload)


__all__ = [
    "SCORING_TOPIC",
    "WORK_TOPIC",
    "handle_work_message",
    "queue_concurrency",
    "score_batch_item",
    "vercel_queue_enabled",
]
