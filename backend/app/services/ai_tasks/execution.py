"""AI 条目的执行：统一执行模型里的 ``ai_task`` 种类（优先于批量评分条目）。

一次执行只调用一次模型（最长约 120 秒）；429、超时、输出不合格都不在原地等，而是
按方案第 6.4 节回到待处理或判失败。所有条目都成功时锁任务行，按序号合并结果。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import logging
from time import monotonic

from sqlalchemy import select
from sqlalchemy import update

from backend.app.db.models import AITask
from backend.app.db.models import AITaskItem
from backend.app.db.models import utcnow
from backend.app.services.ai_tasks.errors import AITaskItemError
from backend.app.services.ai_tasks.handlers import get_handler
from backend.app.services.ai_tasks.service import task_scorer
from backend.app.services.ai_tasks.service import task_view
from backend.app.services.ai_tasks.state import ACTIVE_TASK_STATUSES
from backend.app.services.ai_tasks.state import cancel_pending_items
from backend.app.services.ai_tasks.state import lock_task
from backend.app.services.ai_tasks.state import recount
from backend.app.services.ai_tasks.state import shift_counts
from backend.app.services.work_queue.kinds import ClaimedItem
from backend.app.services.work_queue.kinds import WorkKind
from backend.app.services.work_queue.kinds import register
from backend.app.services.work_queue.wake import wake_source


logger = logging.getLogger("ai-tasks")

KIND = "ai_task"
# 429 延后的次数上限、默认等待与上下限；超时/5xx 与输出修正各再执行 1 次。
MAX_DEFERRALS = 5
DEFAULT_RETRY_AFTER_SECONDS = 30
MIN_RETRY_AFTER_SECONDS = 5
MAX_RETRY_AFTER_SECONDS = 300
MAX_RETRIES = 1
MAX_REPAIRS = 1
# 根因码：熔断是此前失败的结果，不是原因（与批量评分取根因码相同）。
_DERIVED_CODES = {"PROVIDER_CIRCUIT_OPEN"}


def _retry_after(error):
    value = error.retry_after_seconds
    if value is None:
        value = DEFAULT_RETRY_AFTER_SECONDS
    return int(max(MIN_RETRY_AFTER_SECONDS, min(MAX_RETRY_AFTER_SECONDS, value)))


def _begin(session, item, now):
    task = lock_task(session, item.task_id)
    if task is None or task.status not in ACTIVE_TASK_STATUSES:
        changed = session.execute(
            update(AITaskItem)
            .where(AITaskItem.id == item.id, AITaskItem.status == "pending")
            .values(status="canceled", finished_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
        if changed and task is not None:
            shift_counts(session, task.id, pending=-1, canceled=1)
        return None
    attempt = int(item.attempt_count or 0) + 1
    changed = session.execute(
        update(AITaskItem)
        .where(
            AITaskItem.id == item.id,
            AITaskItem.status == "pending",
            AITaskItem.attempt_count == item.attempt_count,
        )
        .values(
            status="running",
            attempt_count=attempt,
            started_at=now,
            heartbeat_at=now,
            finished_at=None,
            not_before=None,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        return None
    shift_counts(session, task.id, pending=-1, running=1)
    if task.status == "queued":
        task.status = "running"
    task.started_at = task.started_at or now
    return ClaimedItem(
        kind=KIND,
        item_id=item.id,
        parent_id=task.id,
        source_key=item.source_key,
        attempt=attempt,
        payload={"task_kind": task.kind},
    )


def _root_failure(items):
    failed = sorted(
        (item for item in items if item.status == "failed" and item.error_code),
        key=lambda value: value.ordinal,
    )
    for item in failed:
        if item.error_code not in _DERIVED_CODES:
            return item
    return failed[0] if failed else None


def settle_task(session, task_id, now):
    """所有条目都结束时收尾：全部成功就合并结果；有失败就取根因码判失败。"""

    task = lock_task(session, task_id)
    if task is None or task.status not in ACTIVE_TASK_STATUSES:
        return task
    if task.pending_count or task.running_count:
        return task
    task = lock_task(session, task_id, with_items=True)
    statuses = [item.status for item in task.items]
    if any(status in ("pending", "running") for status in statuses):
        recount(session, task_id)
        return task
    handler = get_handler(task.kind)
    if any(status == "failed" for status in statuses):
        root = _root_failure(task.items)
        task.status = "failed"
        task.error_code = root.error_code if root else "AI_TASK_FAILED"
        task.error_message = root.error_message if root else "AI 任务未能完成。"
    elif statuses and all(status == "succeeded" for status in statuses):
        outputs = [deepcopy(item.output) for item in sorted(task.items, key=lambda value: value.ordinal)]
        try:
            task.result = handler.merge(task_view(task), outputs)
            task.status = "succeeded"
        except AITaskItemError as exc:
            task.status = "failed"
            task.error_code = exc.code
            task.error_message = exc.message
    else:
        task.status = "canceled"
    task.finished_at = now
    task.state_version = int(task.state_version or 0) + 1
    logger.info(
        "ai_task_finished task_id=%s kind=%s status=%s code=%s",
        task.id,
        task.kind,
        task.status,
        task.error_code or "-",
    )
    return task


def _finish(session_factory, claimed, *, output=None, error=None, latency_ms=0):
    """带围栏写一次执行的结果；返回需要在提交后发出的延迟叫醒（秒）或 None。"""

    with session_factory() as session:
        item = session.scalar(
            select(AITaskItem)
            .where(
                AITaskItem.id == claimed.item_id,
                AITaskItem.status == "running",
                AITaskItem.attempt_count == claimed.attempt,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if item is None:
            logger.warning(
                "work_item_result_discarded kind=%s item_id=%s attempt=%s",
                KIND,
                claimed.item_id,
                claimed.attempt,
            )
            return None
        task = lock_task(session, item.task_id)
        now = utcnow()
        item.stall_count = 0
        item.heartbeat_at = None
        entry = {
            "attempt": item.attempt_count,
            "latency_ms": int(latency_ms),
            "error_code": None,
            "disposition": None,
        }
        delay = None
        if error is None and task.status in ACTIVE_TASK_STATUSES:
            handler = get_handler(task.kind)
            if handler.on_item_success is not None:
                try:
                    handler.on_item_success(session, task_view(task), deepcopy(item.input), output)
                except AITaskItemError as exc:
                    error = exc
        if error is None:
            item.status = "succeeded"
            item.output = output
            item.error_code = None
            item.error_message = None
            item.finished_at = now
            entry["status"] = "succeeded"
            shift_counts(session, task.id, running=-1, succeeded=1)
        else:
            entry["error_code"] = error.code
            disposition = error.disposition
            target = "failed"
            if disposition == "defer" and item.deferral_count < MAX_DEFERRALS:
                delay = _retry_after(error)
                item.deferral_count += 1
                item.not_before = now + timedelta(seconds=delay)
                target = "pending"
            elif disposition == "retry" and item.retry_count < MAX_RETRIES:
                item.retry_count += 1
                target = "pending"
            elif disposition == "repair" and item.repair_count < MAX_REPAIRS:
                item.repair_count += 1
                item.input = {**dict(item.input or {}), "repair_code": error.code}
                target = "pending"
            entry["disposition"] = disposition if target == "pending" else "fail"
            entry["status"] = "deferred" if delay else ("requeued" if target == "pending" else "failed")
            if task.status not in ACTIVE_TASK_STATUSES and target == "pending":
                target = "canceled"
            item.status = target
            if target == "failed":
                item.error_code = error.code
                item.error_message = error.message
                item.finished_at = now
                if task.error_code is None:
                    # 其余条目继续执行；页面先看到第一处失败的原因。
                    task.error_code = error.code
                    task.error_message = error.message
            elif target == "canceled":
                item.finished_at = now
            else:
                item.started_at = None
                item.finished_at = None
            shift_counts(session, task.id, running=-1, **{target: 1})
            if target == "failed":
                # 与同步起草相同：一批永久失败后不再发出新批次（结果反正不完整，继续只会
                # 多计费）。已成功的批次保留，“重试失败的条目”会连同这些被取消的一起重排。
                stopped = cancel_pending_items(session, task.id, now)
                if stopped:
                    shift_counts(session, task.id, pending=-stopped, canceled=stopped)
            log = logger.info if target == "pending" else logger.warning
            log(
                "work_item_%s kind=%s item_id=%s code=%s delay=%s",
                "deferred" if delay else ("requeued" if target == "pending" else "failed"),
                KIND,
                item.id,
                error.code,
                delay or 0,
            )
            if target != "pending":
                delay = None
        history = list(item.attempt_history or [])
        history.append(entry)
        item.attempt_history = history
        source_key = item.source_key
        deferral = item.deferral_count
        session.flush()
        settle_task(session, task.id, now)
        session.commit()
    if delay:
        # 429：带上最早可执行时间回到待处理，按 Retry-After 发一条延迟叫醒（Vercel）。
        wake_source(
            source_key,
            1,
            reason="deferred",
            token="%s-%s" % (claimed.item_id, deferral),
            delay=delay,
        )
    return delay


def _execute(session_factory, claimed, **_options):
    with session_factory() as session:
        task = session.get(AITask, claimed.parent_id)
        item = session.get(AITaskItem, claimed.item_id)
        if task is None or item is None:
            return
        view = task_view(task)
        item_input = deepcopy(item.input or {})
        handler = get_handler(task.kind)
        try:
            scorer = task_scorer(session, task)
        except AITaskItemError as exc:
            scorer = None
            binding_error = exc
        else:
            binding_error = None
    if binding_error is not None:
        _finish(session_factory, claimed, error=binding_error)
        return
    started = monotonic()
    try:
        output = handler.run_item(view, item_input, scorer)
    except AITaskItemError as exc:
        error, output = exc, None
    except Exception as exc:  # 未分类的异常：不重试，记一个不含原文的错误
        logger.exception("ai_task_item_unexpected item_id=%s", claimed.item_id)
        error, output = AITaskItemError(
            "AI_TASK_ITEM_FAILED",
            "%s执行失败（%s）；请稍后重试失败的条目。" % (handler.label, type(exc).__name__),
        ), None
    else:
        error = None
    finally:
        close = getattr(scorer, "close", None)
        if callable(close):
            close()
    latency_ms = max(0, int((monotonic() - started) * 1000))
    _finish(session_factory, claimed, output=output, error=error, latency_ms=latency_ms)


def _made_progress(_session, _item):
    # AI 条目只有一次模型调用：已死的执行一定没有写入结果。
    return False


def _abandon(session, item, now, *, progressed, exhausted):
    task = lock_task(session, item.task_id)
    history = list(item.attempt_history or [])
    history.append(
        {
            "attempt": item.attempt_count,
            "status": "failed" if exhausted else "abandoned",
            "error_code": "WORK_ITEM_STALLED" if exhausted else None,
            "latency_ms": 0,
        }
    )
    item.attempt_history = history
    item.heartbeat_at = None
    if exhausted:
        item.status = "failed"
        item.error_code = "WORK_ITEM_STALLED"
        item.error_message = "多次执行都没能完成（超过运行时间上限或进程中止）；请重试失败的条目。"
        item.finished_at = now
        if task is not None and task.error_code is None:
            task.error_code = item.error_code
            task.error_message = item.error_message
    elif task is not None and task.status not in ACTIVE_TASK_STATUSES:
        item.status = "canceled"
        item.finished_at = now
    else:
        item.status = "pending"
        item.started_at = None
    session.flush()
    if task is not None:
        recount(session, task.id)
        settle_task(session, task.id, now)


def _converge(session_factory, *, parent_ids=None, now=None, limit=200):
    now = now or utcnow()
    with session_factory() as session:
        statement = (
            select(AITask.id)
            .where(AITask.status.in_(ACTIVE_TASK_STATUSES))
            .order_by(AITask.updated_at, AITask.id)
            .limit(limit)
        )
        if parent_ids is not None:
            statement = statement.where(AITask.id.in_(parent_ids))
        task_ids = list(session.scalars(statement))
    converged = 0
    for task_id in task_ids:
        with session_factory() as session:
            task = lock_task(session, task_id, skip_locked=True)
            if task is None or task.status not in ACTIVE_TASK_STATUSES:
                session.rollback()
                continue
            before = (task.pending_count, task.running_count, task.status)
            counts = recount(session, task_id)
            settled = settle_task(session, task_id, now)
            after = (counts["pending"], counts["running"], settled.status if settled else None)
            session.commit()
            if after != before:
                converged += 1
    return converged


def _parent_source(session, task_id):
    return session.scalar(
        select(AITaskItem.source_key).where(AITaskItem.task_id == task_id).limit(1)
    )


AI_TASK_KIND = register(
    WorkKind(
        name=KIND,
        priority=10,
        model=AITaskItem,
        parent_model=AITask,
        parent_column="task_id",
        begin=_begin,
        execute=_execute,
        abandon=_abandon,
        made_progress=_made_progress,
        converge=_converge,
        parent_source=_parent_source,
    )
)


__all__ = ["AI_TASK_KIND", "KIND", "settle_task"]
