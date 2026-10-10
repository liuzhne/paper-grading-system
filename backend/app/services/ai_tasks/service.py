"""AI 任务的建立、查询、取消、重试与清理。

建任务时：校验输入 → 锁定模型来源（私有连接的 ID、密钥版本与快照，或平台模型）→
按内容指纹去重 → 写任务与条目 → 由路由按空闲名额叫醒。执行时用同一份锁定信息重建
模型客户端，连接或密钥变了就以明确错误失败，不换成别的模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import logging

from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from backend.app.db.models import AI_TASK_LIVE_STATUSES
from backend.app.db.models import AITask
from backend.app.db.models import AITaskItem
from backend.app.db.models import utcnow
from backend.app.services.ai_connections import AIConnectionBindingError
from backend.app.services.ai_connections import active_connection_id
from backend.app.services.ai_connections import resolve_connection_runtime
from backend.app.services.ai_connections import validate_outbound_base_url
from backend.app.services.ai_tasks.errors import AITaskItemError
from backend.app.services.ai_tasks.errors import AITaskProblem
from backend.app.services.ai_tasks.errors import AITaskReuse
from backend.app.services.ai_tasks.handlers import TaskView
from backend.app.services.ai_tasks.handlers import get_handler
from backend.app.services.ai_tasks.state import ACTIVE_TASK_STATUSES
from backend.app.services.ai_tasks.state import cancel_pending_items
from backend.app.services.ai_tasks.state import lock_task
from backend.app.services.ai_tasks.state import recount
from backend.app.services.llm.factory import get_llm_scorer
from backend.app.services.work_queue.sources import source_key_for_connection


logger = logging.getLogger("ai-tasks")


@dataclass(frozen=True)
class TaskModel:
    """建任务时锁定的模型来源。"""

    connection_id: str | None
    key_version: int | None
    snapshot: dict | None
    model_name: str | None
    provider: str
    source_key: str


def _close(scorer):
    close = getattr(scorer, "close", None)
    if callable(close):
        close()


def resolve_task_model(db, principal, ai_connection_id=None) -> TaskModel:
    """与同步接口相同的选择：显式或当前启用的私有连接优先，否则用平台默认模型。"""

    connection_id = ai_connection_id or active_connection_id(
        db, owner_id=principal.user_id, organization_id=principal.organization_id or ""
    )
    if connection_id:
        if not principal.organization_id:
            raise AITaskProblem(
                503,
                "AI_CONNECTION_MISSING",
                "当前上下文不能使用私有 AI 连接。",
                "请在组织内使用，或改用平台模型。",
            )
        try:
            runtime = resolve_connection_runtime(
                db,
                connection_id=connection_id,
                owner_id=principal.user_id,
                organization_id=principal.organization_id,
            )
        except ValueError as exc:
            raise AITaskProblem(
                503,
                "AI_CONNECTION_MISSING",
                "所选 AI 连接不存在或已停用。",
                "请到「账户与连接」检查连接后重试。",
            ) from exc
        scorer = get_llm_scorer(runtime)
        try:
            provider = str(getattr(scorer, "provider", "") or "")
        finally:
            _close(scorer)
        return TaskModel(
            connection_id=connection_id,
            key_version=runtime.key_version,
            snapshot=runtime.snapshot(),
            model_name=runtime.model_name,
            provider=provider,
            source_key=source_key_for_connection(connection_id),
        )
    try:
        scorer = get_llm_scorer(session=db)
    except (RuntimeError, ValueError) as exc:
        raise AITaskProblem(
            503,
            "AI_CONNECTION_MISSING",
            "当前没有可用的 AI 模型。",
            "请配置私有 AI 连接，或请管理员配置平台模型。",
        ) from exc
    try:
        provider = str(getattr(scorer, "provider", "") or "")
        model_name = str(getattr(scorer, "model_name", "") or "") or None
    finally:
        _close(scorer)
    return TaskModel(
        connection_id=None,
        key_version=None,
        snapshot=None,
        model_name=model_name,
        provider=provider,
        source_key=source_key_for_connection(None),
    )


def task_view(task) -> TaskView:
    return TaskView(
        id=task.id,
        kind=task.kind,
        rubric_id=task.rubric_id,
        owner_id=task.owner_id,
        organization_id=task.organization_id,
        scope=dict(task.scope or {}),
        input_snapshot=dict(task.input_snapshot or {}),
        model_name=task.model_name,
    )


def task_scorer(session, task):
    """按建任务时锁定的来源重建模型客户端；连接或密钥变了就明确失败。"""

    if task.ai_connection_snapshot is not None:
        if not task.ai_connection_id or not task.owner_id or not task.organization_id:
            raise AITaskItemError(
                "AI_CONNECTION_MISSING",
                "建任务时绑定的 AI 连接已被删除；请重新发起。",
            )
        try:
            runtime = resolve_connection_runtime(
                session,
                connection_id=task.ai_connection_id,
                owner_id=task.owner_id,
                organization_id=task.organization_id,
            )
        except ValueError as exc:
            raise AITaskItemError(
                "AI_CONNECTION_MISSING",
                "建任务时绑定的 AI 连接已停用或不可用；请检查连接后重新发起。",
            ) from exc
        if runtime.key_version != task.ai_connection_key_version:
            raise AITaskItemError(
                "AI_CONNECTION_KEY_CHANGED",
                "任务创建后，绑定的 AI 连接密钥已变更；请重新发起。",
            )
        if runtime.snapshot() != task.ai_connection_snapshot:
            raise AITaskItemError(
                "AI_CONNECTION_CONFIG_CHANGED",
                "任务创建后，绑定的 AI 连接配置已变更；请重新发起。",
            )
        try:
            validate_outbound_base_url(runtime.base_url)
        except ValueError as exc:
            raise AITaskItemError("AI_CONNECTION_BLOCKED", str(exc)) from exc
        return get_llm_scorer(runtime)
    try:
        return get_llm_scorer(session=session)
    except (RuntimeError, ValueError, AIConnectionBindingError) as exc:
        raise AITaskItemError(
            "AI_CONNECTION_MISSING", "平台模型当前不可用；请稍后重试或改用私有连接。"
        ) from exc


def _digest(value) -> str:
    # 规范 JSON（键排序、紧凑分隔）。评分项分值是浮点数，Core 的严格规范化不接受，这里
    # 只用于去重，不参与评分可复现性。
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(text.encode("utf-8")).hexdigest()


def task_fingerprint(*, kind, rubric_id, compilation_id, prepared, model, prompt_version, owner_id=None):
    """内容指纹：同样的输入、同一个连接与模型、同一版提示词，得到同一个任务。

    不含时间戳和客户端生成的键：双击、多个标签页、刷新后重点都命中同一个任务。
    没有评分标准的任务（导入前识别）按用户区分。
    """

    material = {
        "kind": kind,
        "rubric_id": rubric_id,
        "compilation_id": compilation_id,
        "scope": prepared.scope,
        "input": _digest(prepared.input_snapshot),
        "material": prepared.fingerprint_material,
        "connection_id": model.connection_id,
        "key_version": model.key_version,
        "model_name": model.model_name,
        "prompt_version": prompt_version,
    }
    if rubric_id is None:
        material["owner_id"] = owner_id
    return _digest(material)


def _active_compilation_id(db, rubric_id):
    from backend.app.services.rubrics.draft_graph import read_execution_draft

    if rubric_id is None:
        return None
    execution = read_execution_draft(session=db, rubric_id=rubric_id)
    return (execution.get("active_compilation") or {}).get("id")


def _live_task(db, rubric_id, fingerprint, owner_id=None):
    scope = (
        AITask.rubric_id == rubric_id
        if rubric_id is not None
        else (AITask.rubric_id.is_(None)) & (AITask.owner_id == owner_id)
    )
    return db.scalar(
        select(AITask)
        .where(scope, AITask.fingerprint == fingerprint, AITask.status.in_(AI_TASK_LIVE_STATUSES))
        .options(selectinload(AITask.items))
    )


def _prune_upload_tasks(db, owner_id):
    """导入前识别没有评分标准，不会随发布清理：同一用户再次发起时删掉已结束的旧任务。"""

    task_ids = list(
        db.scalars(
            select(AITask.id).where(
                AITask.rubric_id.is_(None),
                AITask.owner_id == owner_id,
                AITask.status.not_in(ACTIVE_TASK_STATUSES),
            )
        )
    )
    if task_ids:
        db.execute(delete(AITaskItem).where(AITaskItem.task_id.in_(task_ids)))
        db.execute(delete(AITask).where(AITask.id.in_(task_ids)))
    return len(task_ids)


def get_ai_task(db, task_id):
    return db.scalar(
        select(AITask).where(AITask.id == task_id).options(selectinload(AITask.items))
    )


def list_ai_tasks(db, rubric_id, *, kind=None, active=False):
    statement = (
        select(AITask)
        .where(AITask.rubric_id == rubric_id, AITask.status != "superseded")
        .options(selectinload(AITask.items))
        .order_by(AITask.created_at.desc(), AITask.id.desc())
    )
    if kind:
        statement = statement.where(AITask.kind == kind)
    if active:
        statement = statement.where(AITask.status.in_(ACTIVE_TASK_STATUSES))
    return list(db.scalars(statement).all())


def _supersede(db, task, now):
    cancel_pending_items(db, task.id, now)
    task.status = "superseded"
    task.finished_at = task.finished_at or now
    db.flush()
    recount(db, task.id)


def create_ai_task(db, *, rubric_id, kind, params, principal, ai_connection_id=None, regenerate=False):
    """建任务；命中同指纹的进行中或已成功任务时返回它（第二个值为 False）。

    ``rubric_id`` 为空只用于导入前的结构识别（参数由服务端解析上传文件得到）。
    """

    handler = get_handler(kind)
    model = resolve_task_model(db, principal, ai_connection_id)
    if handler.extra.get("requires_real_model") and model.provider.lower() == "mock":
        code, message, action = handler.extra["missing_model_problem"]
        raise AITaskProblem(503, code, message, action)
    try:
        prepared = handler.prepare(db, rubric_id, params or {}, principal)
    except AITaskReuse as reuse:
        # 要处理的内容已全部在进行中的任务里（例如另一个标签页正在归类同一批单元）。
        existing = get_ai_task(db, reuse.task_id)
        if existing is not None:
            return existing, False
        raise
    fingerprint = task_fingerprint(
        kind=kind,
        rubric_id=rubric_id,
        compilation_id=_active_compilation_id(db, rubric_id),
        prepared=prepared,
        model=model,
        prompt_version=handler.prompt_version,
        owner_id=principal.user_id,
    )
    now = utcnow()
    existing = _live_task(db, rubric_id, fingerprint, principal.user_id)
    if existing is not None and not regenerate:
        return existing, False
    if existing is not None:
        # “重新生成”：模型每次输出不同，用户可能确实想换一版；旧任务（进行中的先取消）作废。
        _supersede(db, lock_task(db, existing.id), now)
    if rubric_id is None:
        _prune_upload_tasks(db, principal.user_id)
    task = AITask(
        organization_id=principal.organization_id,
        owner_id=principal.user_id,
        kind=kind,
        rubric_id=rubric_id,
        scope=prepared.scope,
        input_snapshot=prepared.input_snapshot,
        fingerprint=fingerprint,
        ai_connection_id=model.connection_id,
        ai_connection_key_version=model.key_version,
        ai_connection_snapshot=model.snapshot,
        model_name=model.model_name,
        prompt_version=handler.prompt_version,
        status="queued" if prepared.items else "succeeded",
        total_items=len(prepared.items),
        pending_count=len(prepared.items),
        result=None if prepared.items else prepared.result,
        finished_at=None if prepared.items else now,
    )
    db.add(task)
    db.flush()
    for ordinal, item_input in enumerate(prepared.items):
        db.add(
            AITaskItem(
                task_id=task.id,
                ordinal=ordinal,
                source_key=model.source_key,
                owner_id=principal.user_id,
                input=item_input,
                status="pending",
            )
        )
    try:
        db.commit()
    except IntegrityError:
        # 并发的两次提交：部分唯一索引兜底，输的一方回查后返回胜出者。
        db.rollback()
        winner = _live_task(db, rubric_id, fingerprint, principal.user_id)
        if winner is None:
            raise
        return winner, False
    logger.info(
        "ai_task_created task_id=%s kind=%s items=%s source=%s",
        task.id,
        kind,
        len(prepared.items),
        model.source_key,
    )
    return get_ai_task(db, task.id), True


def cancel_ai_task(db, task_id):
    """未开始的条目直接取消；进行中的条目跑完后不再继续，结果不合并。"""

    task = db.get(AITask, task_id)
    if task is None:
        raise AITaskProblem(404, "AI_TASK_NOT_FOUND", "任务不存在。", "请刷新页面。")
    if task.status in ACTIVE_TASK_STATUSES:
        now = utcnow()
        cancel_pending_items(db, task_id, now)
        task = lock_task(db, task_id)
        if task.status in ACTIVE_TASK_STATUSES:
            task.status = "canceled"
            task.finished_at = now
            db.flush()
            recount(db, task_id)
        db.commit()
    return get_ai_task(db, task_id)


def retry_ai_task(db, task_id):
    """只重试失败（或被取消）的条目，已成功的保留。返回 (任务, 是否换成了另一个任务)。"""

    task = lock_task(db, task_id, with_items=True)
    if task is None:
        raise AITaskProblem(404, "AI_TASK_NOT_FOUND", "任务不存在。", "请刷新页面。")
    if task.status != "failed":
        raise AITaskProblem(
            409, "AI_TASK_NOT_RETRYABLE", "只有失败的任务可以重试失败的条目。", "请刷新页面查看任务状态。"
        )
    now = utcnow()
    other = _live_task(db, task.rubric_id, task.fingerprint, task.owner_id)
    if other is not None and other.id != task.id:
        # 期间已有同内容的任务（例如另一个标签页重新提交）：直接用它。
        _supersede(db, task, now)
        db.commit()
        return get_ai_task(db, other.id), True
    for item in task.items:
        if item.status not in ("failed", "canceled"):
            continue
        item.status = "pending"
        item.error_code = None
        item.error_message = None
        item.not_before = None
        item.started_at = None
        item.finished_at = None
        item.heartbeat_at = None
        item.stall_count = 0
        item.deferral_count = 0
        item.retry_count = 0
        item.repair_count = 0
        item.input = {key: value for key, value in (item.input or {}).items() if key != "repair_code"}
    task.status = "queued"
    task.error_code = None
    task.error_message = None
    task.result = None
    task.finished_at = None
    db.flush()
    recount(db, task.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        winner = _live_task(db, task.rubric_id, task.fingerprint, task.owner_id)
        if winner is None:
            raise
        return winner, True
    return get_ai_task(db, task_id), False


def delete_rubric_ai_tasks(db, rubric_id) -> int:
    """评分标准发布时删除它名下的全部 AI 任务与条目（在调用方的事务里）。"""

    task_ids = list(db.scalars(select(AITask.id).where(AITask.rubric_id == rubric_id)))
    if not task_ids:
        return 0
    db.execute(delete(AITaskItem).where(AITaskItem.task_id.in_(task_ids)))
    db.execute(delete(AITask).where(AITask.id.in_(task_ids)))
    logger.info("ai_tasks_cleared rubric_id=%s count=%s", rubric_id, len(task_ids))
    return len(task_ids)


__all__ = [
    "TaskModel",
    "cancel_ai_task",
    "create_ai_task",
    "delete_rubric_ai_tasks",
    "get_ai_task",
    "list_ai_tasks",
    "resolve_task_model",
    "retry_ai_task",
    "task_fingerprint",
    "task_scorer",
    "task_view",
]
