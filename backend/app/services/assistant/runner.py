"""评分助手的运行器：`POST /assistant/conversations/{id}/runs` 的全部逻辑（方案 §7、§9、§11）。

一次运行做的事：
1. 抢会话的运行锁（同一会话同一时间只有一个请求在推进流程，第二个 409）；
2. 作废超过 7 天没回应的等待（M3）；
3. 按类型处理：`message`（识别意图 → 查询图或开流程）、`resume`（恢复流程图）、
   `select`（选择卡片：选任务、选论文、确认重试或取消）；
4. 记下流程当前等待的中断，修剪状态快照（只留最近 10 份，M2）；
5. 返回本次新增或更新的消息、当前等待的中断与焦点。
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from typing import Literal

from fastapi import HTTPException
from langgraph.types import Command
from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import Session

from backend.app.api.deps import CurrentPrincipal
from backend.app.db.models import AssistantConversation
from backend.app.db.models import AssistantMessage
from backend.app.db.models import utcnow
from backend.app.services.assistant import intents
from backend.app.services.assistant import model as assistant_model
from backend.app.services.assistant import tools
from backend.app.services.assistant.checkpointer import SqlCheckpointSaver
from backend.app.services.assistant.context import RunContext
from backend.app.services.assistant.flow import USER_WAIT_KINDS
from backend.app.services.assistant.flow import build_flow_graph
from backend.app.services.assistant.queries import QUERY_INTENTS
from backend.app.services.assistant.queries import build_query_graph
from backend.app.services.assistant.views import HELP_TEXT
from backend.app.services.assistant.views import MAX_FILES
from backend.app.services.assistant.views import quick_actions_card

LOCK_SECONDS = 120
PENDING_TTL = timedelta(days=7)        # M3
KEEP_CHECKPOINTS = 10                  # M2
RECENT_USER_MESSAGES = 3               # M1
DEFAULT_TITLE = "新对话"

_QUERY_GRAPH = build_query_graph()


# --- 恢复值校验：浏览器带回的数据先按中断类型校验，图只接收合规的值 ----------------------------

class _PickFiles(BaseModel):
    count: int = Field(ge=1, le=MAX_FILES)
    names: list[str] = Field(default_factory=list, max_length=MAX_FILES)


class _ConfirmStart(BaseModel):
    action: Literal["confirm", "change_rubric", "repick", "cancel"]
    rubric_id: str | None = Field(default=None, max_length=36)


class _UploadPapers(BaseModel):
    paper_ids: list[str] = Field(default_factory=list, max_length=MAX_FILES)


class _ConfirmRemove(BaseModel):
    action: Literal["remove", "cancel"]


class _WaitJob(BaseModel):
    event: Literal["job_finished"]


class _ImportSession(BaseModel):
    action: Literal["created", "cancel"] = "created"
    import_session_id: str | None = Field(default=None, max_length=36)


class _WaitPublish(BaseModel):
    action: Literal["check", "cancel"] = "check"


RESUME_SCHEMAS = {
    "pick_files": _PickFiles,
    "confirm_start": _ConfirmStart,
    "upload_papers": _UploadPapers,
    "confirm_remove": _ConfirmRemove,
    "wait_job": _WaitJob,
    "create_import_session": _ImportSession,
    "wait_publish": _WaitPublish,
}


def _conflict(code: str, message: str):
    raise HTTPException(status_code=409, detail={"code": code, "message": message})


# --- 运行锁 -------------------------------------------------------------------------

def _acquire_lock(db: Session, conversation: AssistantConversation) -> None:
    now = utcnow()
    result = db.execute(
        update(AssistantConversation)
        .where(
            AssistantConversation.id == conversation.id,
            or_(AssistantConversation.run_locked_until.is_(None), AssistantConversation.run_locked_until < now),
        )
        .values(run_locked_until=now + timedelta(seconds=LOCK_SECONDS))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if result.rowcount == 0:
        _conflict("ASSISTANT_RUN_IN_PROGRESS", "上一步还在处理，请稍候。")
    db.refresh(conversation)


def _release_lock(db: Session, conversation_id: str) -> None:
    db.execute(
        update(AssistantConversation)
        .where(AssistantConversation.id == conversation_id)
        .values(run_locked_until=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()


# --- 流程线程 -------------------------------------------------------------------------

def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _end_flow(ctx: RunContext, saver: SqlCheckpointSaver) -> None:
    thread_id = ctx.conversation.thread_id
    if thread_id:
        saver.prune_thread(thread_id, keep=KEEP_CHECKPOINTS)
    ctx.conversation.thread_id = None
    ctx.conversation.pending = None


def _abandon_flow(ctx: RunContext, saver: SqlCheckpointSaver, *, status: str) -> None:
    pending = ctx.conversation.pending or {}
    ctx.update_card(pending.get("message_id"), pending.get("card_id"), status)
    if ctx.conversation.thread_id:
        saver.delete_thread(ctx.conversation.thread_id)
    ctx.conversation.thread_id = None
    ctx.conversation.pending = None


def _expire_stale_pending(ctx: RunContext, saver: SqlCheckpointSaver) -> None:
    pending = ctx.conversation.pending
    if not pending or pending.get("kind") not in USER_WAIT_KINDS:
        return
    created = datetime.fromisoformat(pending["created_at"])
    if utcnow() - created < PENDING_TTL:
        return
    _abandon_flow(ctx, saver, status="expired")
    ctx.say("上一步已经超过 7 天没有回应，已作废，请重新开始。", [quick_actions_card(["start_grading"])])


def _after_flow(ctx: RunContext, graph, saver: SqlCheckpointSaver) -> None:
    thread_id = ctx.conversation.thread_id
    state = graph.get_state(_config(thread_id))
    interrupts = [item.value for task in state.tasks for item in task.interrupts]
    if interrupts:
        ctx.conversation.pending = {**interrupts[0], "created_at": utcnow().isoformat(), "thread_id": thread_id}
        saver.prune_thread(thread_id, keep=KEEP_CHECKPOINTS)
    else:
        _end_flow(ctx, saver)


def _start_flow(ctx: RunContext, saver: SqlCheckpointSaver, flow_input: dict) -> None:
    if ctx.conversation.thread_id:
        pending = ctx.conversation.pending or {}
        if pending.get("kind") in {"wait_job", "upload_papers"}:
            ctx.say("这个会话里有一批论文正在上传或评分。要同时评另一批，请新开一个会话。",
                    [quick_actions_card(["query_progress"])])
            return
        # 还停在“等用户确认”这一类步骤：放弃旧流程，开始新的。
        _abandon_flow(ctx, saver, status="dismissed")
    ctx.conversation.flow_seq = int(ctx.conversation.flow_seq or 0) + 1
    ctx.conversation.thread_id = "%s:%d" % (ctx.conversation.id, ctx.conversation.flow_seq)
    graph = build_flow_graph(saver)
    graph.invoke(flow_input, _config(ctx.conversation.thread_id), context=ctx)
    _after_flow(ctx, graph, saver)


def _resume_flow(ctx: RunContext, saver: SqlCheckpointSaver, card_id: str, value: dict) -> None:
    pending = ctx.conversation.pending
    if not pending or not ctx.conversation.thread_id or pending.get("card_id") != card_id:
        _conflict("ASSISTANT_CARD_STALE", "这张卡片已经失效，请看最新的消息。")
    schema = RESUME_SCHEMAS.get(pending["kind"])
    if schema is None:
        _conflict("ASSISTANT_CARD_STALE", "这张卡片已经失效，请看最新的消息。")
    try:
        validated = schema.model_validate(value or {})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="恢复数据不合规：%s" % exc.errors()[0]["msg"]) from exc
    payload = validated.model_dump()
    if pending["kind"] == "create_import_session" and payload["action"] == "created" and not payload["import_session_id"]:
        raise HTTPException(status_code=422, detail="恢复数据不合规：缺少导入草稿编号")
    graph = build_flow_graph(saver)
    graph.invoke(Command(resume=payload), _config(ctx.conversation.thread_id), context=ctx)
    _after_flow(ctx, graph, saver)


# --- 查询 ---------------------------------------------------------------------------

def _run_query(ctx: RunContext, intent: str, **params) -> None:
    _QUERY_GRAPH.invoke({"intent": intent, **params}, context=ctx)


# --- 三种请求 -------------------------------------------------------------------------

def _recent_user_texts(ctx: RunContext, exclude_id: str) -> list[str]:
    rows = ctx.db.scalars(
        select(AssistantMessage.text)
        .where(
            AssistantMessage.conversation_id == ctx.conversation.id,
            AssistantMessage.role == "user",
            AssistantMessage.id != exclude_id,
        )
        .order_by(AssistantMessage.created_at.desc())
        .limit(RECENT_USER_MESSAGES)
    ).all()
    return [text[: intents.MAX_TEXT_CHARS] for text in reversed(rows)]


def _handle_message(ctx: RunContext, saver: SqlCheckpointSaver, text: str, attachments: dict | None) -> None:
    message = AssistantMessage(conversation_id=ctx.conversation.id, role="user", text=text, cards=[])
    ctx.db.add(message)
    ctx.db.flush()
    ctx.new_message_ids.append(message.id)
    if ctx.conversation.title == DEFAULT_TITLE:
        ctx.conversation.title = text[:30]
    ctx.conversation.updated_at = utcnow()

    if attachments:
        result = intents.IntentResult(intent="start_grading")
    else:
        result = intents.match_rules(text)
        if result.intent == "unknown":
            effective = assistant_model.effective_model(
                ctx.db, user_id=ctx.user_id, organization_id=ctx.principal.organization_id
            )
            scorer = assistant_model.build_scorer(
                ctx.db, effective, user_id=ctx.user_id, organization_id=ctx.principal.organization_id
            )
            focus = dict(ctx.conversation.focus or {})
            result = intents.interpret(
                text, focus=focus, scorer=scorer, recent=_recent_user_texts(ctx, message.id)
            )
    message.intent = result.intent
    message.model_name = result.model_name

    if result.intent == "start_grading":
        files = attachments or {}
        _start_flow(ctx, saver, {
            "mode": "grading",
            "file_count": int(files.get("count") or 0),
            "file_names": list(files.get("names") or [])[:MAX_FILES],
        })
    elif result.intent == "import_rubric":
        _start_flow(ctx, saver, {"mode": "import"})
    elif result.intent == "help":
        ctx.say(HELP_TEXT, [quick_actions_card()], intent="help")
    elif result.intent in QUERY_INTENTS:
        _run_query(ctx, result.intent, ordinal=result.paper_ordinal, name=result.paper_name, text=text)
    else:
        reply = result.clarify or "我没理解这句话。你可以换个说法，或者点下面的按钮。"
        if result.error_code == "ASSISTANT_MODEL_FAILED":
            reply = "助手模型暂时没有响应，我只能识别常见的说法。可以点下面的按钮继续。"
        ctx.say(reply, [quick_actions_card()], intent="unknown")


def _handle_select(ctx: RunContext, saver: SqlCheckpointSaver, card_id: str, value: dict) -> None:
    message, card = ctx.find_card(card_id)
    if card is None or card.get("status") != "proposed":
        _conflict("ASSISTANT_CARD_STALE", "这张卡片已经失效，请看最新的消息。")
    kind = card.get("type")
    value = value or {}

    if kind == "choose_batch":
        batch = next((item for item in card.get("batches", []) if item["id"] == value.get("batch_id")), None)
        if batch is None:
            raise HTTPException(status_code=422, detail="请选择卡片里的任务")
        ctx.update_card(message.id, card_id, "done", {"batch_id": batch["id"]})
        ctx.set_focus(batch_id=batch["id"], batch_name=batch["name"], job_id=None, paper_id=None)
        _run_query(ctx, card["next_intent"], batch_id=batch["id"], ordinal=card.get("paper_ordinal"),
                   name=card.get("paper_name"), text=card.get("text") or "")
        return
    if kind == "choose_paper":
        paper = next((item for item in card.get("papers", []) if item["paper_id"] == value.get("paper_id")), None)
        if paper is None:
            raise HTTPException(status_code=422, detail="请选择卡片里的论文")
        ctx.update_card(message.id, card_id, "done", {"paper_id": paper["paper_id"]})
        _run_query(ctx, "query_paper", batch_id=card["batch_id"], paper_id=paper["paper_id"])
        return
    if kind in {"confirm_retry", "diagnosis"}:
        if value.get("action") == "cancel":
            ctx.update_card(message.id, card_id, "dismissed")
            return
        if value.get("action") not in {"confirm", "retry"}:
            raise HTTPException(status_code=422, detail="不支持的操作")
        try:
            tools.call(ctx, "retry_scoring", allow_write=True, job_id=card["job_id"])
        except HTTPException as exc:
            ctx.update_card(message.id, card_id, "failed", {"error": str(exc.detail)})
            ctx.say("重试失败：%s" % exc.detail)
            return
        ctx.update_card(message.id, card_id, "done", {"retried": True})
        pending = ctx.conversation.pending or {}
        if pending.get("kind") == "wait_job":
            ctx.say("失败项已重新排队，评完我会告诉你结果。")
            return
        _start_flow(ctx, saver, {"mode": "watch", "batch_id": card["batch_id"], "job_id": card["job_id"],
                                 "auto_retry_used": True})
        return
    if kind == "confirm_cancel":
        if value.get("action") == "cancel":
            ctx.update_card(message.id, card_id, "dismissed")
            return
        if value.get("action") != "confirm":
            raise HTTPException(status_code=422, detail="不支持的操作")
        try:
            tools.call(ctx, "cancel_scoring", allow_write=True, job_id=card["job_id"])
        except HTTPException as exc:
            ctx.update_card(message.id, card_id, "failed", {"error": str(exc.detail)})
            ctx.say("取消失败：%s" % exc.detail)
            return
        ctx.update_card(message.id, card_id, "done", {"canceled": True})
        ctx.say("已请求取消，正在完成已经开始的那几份。")
        return
    raise HTTPException(status_code=422, detail="这张卡片不能这样操作")


def run(db: Session, principal: CurrentPrincipal, conversation: AssistantConversation, request: dict) -> dict:
    _acquire_lock(db, conversation)
    ctx = RunContext(db=db, principal=principal, conversation=conversation)
    saver = SqlCheckpointSaver(db)
    try:
        _expire_stale_pending(ctx, saver)
        kind = request["type"]
        if kind == "message":
            _handle_message(ctx, saver, request["text"], request.get("attachments"))
        elif kind == "resume":
            _resume_flow(ctx, saver, request["card_id"], request.get("value") or {})
        else:
            _handle_select(ctx, saver, request["card_id"], request.get("value") or {})
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        _release_lock(db, conversation.id)
    db.refresh(conversation)
    ids = list(dict.fromkeys([*ctx.new_message_ids, *ctx.updated_message_ids]))
    messages = db.scalars(select(AssistantMessage).where(AssistantMessage.id.in_(ids))).all() if ids else []
    messages = sorted(messages, key=lambda item: (item.created_at, item.id))
    return {"messages": messages, "pending": conversation.pending, "conversation": conversation}


def expire_if_stale(db: Session, principal: CurrentPrincipal, conversation: AssistantConversation) -> None:
    """读会话时也作废过期的等待，避免界面上一直显示一张早已不能用的卡片。"""

    pending = conversation.pending
    if not pending or pending.get("kind") not in USER_WAIT_KINDS:
        return
    if utcnow() - datetime.fromisoformat(pending["created_at"]) < PENDING_TTL:
        return
    ctx = RunContext(db=db, principal=principal, conversation=conversation)
    _expire_stale_pending(ctx, SqlCheckpointSaver(db))
    db.commit()
    db.refresh(conversation)


def delete_conversation_state(db: Session, conversation: AssistantConversation) -> None:
    SqlCheckpointSaver(db).delete_threads_with_prefix("%s:" % conversation.id)
