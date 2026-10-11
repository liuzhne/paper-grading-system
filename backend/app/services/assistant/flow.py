"""评分助手的流程图（LangGraph，有状态，方案 §7.3–§7.5）。

三种流程共用一张图：
- grading：选文件 → 确认开始 → 建任务 → 等浏览器上传 → 预检 →（是否移除）→ 开评 → 等评分结束 → 汇报；
- import：等浏览器建导入草稿 → 等用户在工作区确认并发布 → 转 grading；
- watch：手动重试后观察一次已存在的作业，评完汇报。

约定（LangGraph 恢复时被中断的节点会从头重跑）：
- “等待”节点（`wait_*`）第一行就 `interrupt()`，之后只做校验与状态推进；
- 有副作用的操作放在等待节点之后的独立节点里，并做成幂等；
- 中断返回给浏览器的只有卡片编号与“需要浏览器做的事”，可 JSON 序列化；
- 状态里只放编号与标志位，不放对话全文、分数或引文（方案 §9）。
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import TypedDict

from fastapi import HTTPException
from langgraph.graph import END
from langgraph.graph import START
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from sqlalchemy import select

from backend.app.api import guards
from backend.app.db.models import GradingBatch
from backend.app.db.models import Paper
from backend.app.services.assistant import tools
from backend.app.services.assistant.context import RunContext
from backend.app.services.assistant.context import new_card_id
from backend.app.services.assistant.model import active_connections
from backend.app.services.assistant.model import platform_available
from backend.app.services.assistant.views import MAX_FILES
from backend.app.services.assistant.views import batch_name
from backend.app.services.assistant.views import blocking_findings
from backend.app.services.assistant.views import diagnose_job
from backend.app.services.assistant.views import overview_text
from backend.app.services.assistant.views import quick_actions_card
from backend.app.services.assistant.views import score_overview
from backend.app.services.auth import auth_active
from backend.app.services.rubric_import import import_sessions

ACTIVE_JOB_STATUSES = {"queued", "running", "cancel_requested"}
TERMINAL_JOB_STATUSES = {"completed", "completed_with_errors", "failed", "canceled"}

#: 等用户回应的中断：7 天无回应即作废（方案 M3）。等评分结束不设过期。
USER_WAIT_KINDS = frozenset({
    "pick_files", "confirm_start", "upload_papers", "confirm_remove",
    "create_import_session", "wait_publish",
})


class FlowState(TypedDict, total=False):
    mode: str
    next: str
    flow_started_at: str
    file_count: int
    file_names: list[str]
    preset_rubric_id: str
    rubric_id: str
    rubric_name: str
    rubric_version: str
    connection_label: str
    batch_name: str
    batch_id: str
    paper_ids: list[str]
    removable: list[dict]
    job_id: str
    auto_retry_used: bool
    card: dict
    progress_card: dict
    import_session_id: str
    import_name: str


def _ctx(runtime: Runtime[RunContext]) -> RunContext:
    return runtime.context


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _detail(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("code") or "操作失败")
    return str(detail or "操作失败")


def _card_ref(message, card_id: str) -> dict:
    return {"message_id": message.id, "card_id": card_id}


def _pending(kind: str, card: dict, client: dict | None = None) -> dict:
    return {"kind": kind, "card_id": card.get("card_id"), "message_id": card.get("message_id"),
            "client": client or {"type": "confirm"}}


def _fail(ctx: RunContext, card: dict | None, text: str) -> dict:
    if card:
        ctx.update_card(card.get("message_id"), card.get("card_id"), "failed", {"error": text})
    ctx.say(text, [quick_actions_card(["start_grading", "query_progress"])])
    return {"next": END}


# --- 入口 ---------------------------------------------------------------------------

def begin(state: FlowState, runtime: Runtime[RunContext]):
    mode = state.get("mode", "grading")
    if mode == "import":
        return {"next": "ask_rubric_files", "flow_started_at": _now_iso()}
    if mode == "watch":
        return {"next": "post_progress", "flow_started_at": _now_iso()}
    target = "propose_start" if state.get("file_count") else "ask_files"
    return {"next": target, "flow_started_at": _now_iso()}


# --- 场景 A：选文件与关键确认 -----------------------------------------------------------

def ask_files(state: FlowState, runtime: Runtime[RunContext]):
    ctx = _ctx(runtime)
    card_id = new_card_id("files")
    text = "请选择待评分的文件（.docx 或 .pdf，一次最多 %d 份）。" % MAX_FILES
    if state.get("rubric_name"):
        text = "请选择待评分的文件，评分标准为《%s》%s。" % (state["rubric_name"], state.get("rubric_version") or "")
    message = ctx.say(text, [{"id": card_id, "type": "pick_files", "status": "proposed", "max": MAX_FILES}],
                      intent="start_grading")
    return {"card": _card_ref(message, card_id), "next": "wait_files"}


def wait_files(state: FlowState, runtime: Runtime[RunContext]):
    card = state["card"]
    value = interrupt(_pending("pick_files", card, {"type": "pick_files", "max": MAX_FILES}))
    ctx = _ctx(runtime)
    names = [str(name)[:200] for name in value.get("names", [])][:MAX_FILES]
    count = int(value["count"])
    ctx.update_card(card["message_id"], card["card_id"], "done", {"count": count, "names": names})
    return {"file_count": count, "file_names": names, "next": "propose_start"}


def _resolve_rubric(ctx: RunContext, preset_id: str | None):
    published = tools.call(ctx, "list_published_rubrics")
    if preset_id:
        return next((rubric for rubric in published if rubric["id"] == preset_id), None)
    # 默认取用户可见、最近发布的一份（U3、B6）；卡片写明名称与版本，可以换。
    return published[0] if published else None


def _scoring_model_label(ctx: RunContext) -> str | None:
    """与“新建评分任务”页一致：当前启用的个人连接优先，没有时用平台模型。"""

    if not auth_active():
        return "开发模式模型"
    connections = active_connections(ctx.db, user_id=ctx.user_id, organization_id=ctx.principal.organization_id)
    if connections:
        return "%s · %s" % (connections[0].name, connections[0].model_name)
    if platform_available(ctx.db):
        return "平台模型"
    return None


def propose_start(state: FlowState, runtime: Runtime[RunContext]):
    ctx = _ctx(runtime)
    rubric = _resolve_rubric(ctx, state.get("preset_rubric_id"))
    if rubric is None:
        ctx.say("还没有已发布的评分标准。可以上传你的评分规则和模板，我来帮你导入。",
                [quick_actions_card(["import_rubric"]), {"type": "link", "label": "打开评分标准页", "path": "/rubrics"}])
        return {"next": END}
    label = _scoring_model_label(ctx)
    if label is None:
        ctx.say("还没有可用的评分模型，请先在账户页配置自己的模型连接。",
                [{"type": "link", "label": "去配置模型连接", "path": "/account"}])
        return {"next": END}
    count = int(state.get("file_count") or 0)
    started = datetime.fromisoformat(state["flow_started_at"])
    card_id = new_card_id("start")
    source = "" if state.get("preset_rubric_id") else "（最新发布的评分标准，可以换）"
    message = ctx.say(
        "用《%s》%s%s评这 %d 份文件，评分模型：%s。开始吗？" % (rubric["name"], rubric["version"], source, count, label),
        [{
            "id": card_id, "type": "confirm_start", "status": "proposed",
            "rubric_id": rubric["id"], "rubric_name": rubric["name"], "rubric_version": rubric["version"],
            "file_count": count, "connection_label": label,
        }],
        intent="start_grading",
    )
    return {
        "rubric_id": rubric["id"], "rubric_name": rubric["name"], "rubric_version": rubric["version"],
        "connection_label": label, "batch_name": batch_name(started, count),
        "card": _card_ref(message, card_id), "next": "wait_confirm",
    }


def wait_confirm(state: FlowState, runtime: Runtime[RunContext]):
    card = state["card"]
    value = interrupt(_pending("confirm_start", card))
    ctx = _ctx(runtime)
    action = value["action"]
    if action == "cancel":
        ctx.update_card(card["message_id"], card["card_id"], "dismissed")
        ctx.say("好的，这次不评了。", [quick_actions_card(["start_grading", "query_progress"])])
        return {"next": END}
    if action == "repick":
        ctx.update_card(card["message_id"], card["card_id"], "dismissed")
        return {"next": "ask_files", "file_count": 0, "file_names": []}
    if action == "change_rubric":
        chosen = _resolve_rubric(ctx, value.get("rubric_id"))
        if chosen is None:
            ctx.say("这份评分标准不可用（未发布或无权访问），请换一份。")
            return {"next": "wait_confirm"}
        ctx.update_card(card["message_id"], card["card_id"], "dismissed")
        return {"next": "propose_start", "preset_rubric_id": chosen["id"]}
    ctx.update_card(card["message_id"], card["card_id"], "running")
    return {"next": "create_batch"}


def create_batch(state: FlowState, runtime: Runtime[RunContext]):
    ctx = _ctx(runtime)
    card = state["card"]
    # 幂等：节点重跑（例如上次运行在落库之后、写快照之前中断）时找回本流程建过的批次。
    started = datetime.fromisoformat(state["flow_started_at"])
    existing = ctx.db.scalar(
        select(GradingBatch).where(
            GradingBatch.owner_id == ctx.user_id,
            GradingBatch.name == state["batch_name"],
            GradingBatch.created_at >= started,
        )
    )
    if existing is not None:
        batch = {"id": existing.id, "name": existing.name}
    else:
        try:
            batch = tools.call(ctx, "create_batch", allow_write=True,
                               name=state["batch_name"], rubric_id=state["rubric_id"])
        except HTTPException as exc:
            return _fail(ctx, card, "建任务失败：%s" % _detail(exc))
    ctx.set_focus(batch_id=batch["id"], batch_name=batch["name"], rubric_id=state["rubric_id"],
                  rubric_name=state["rubric_name"], job_id=None, paper_id=None)
    ctx.show_workspace("/tasks/new?batch=%s" % batch["id"])
    return {"batch_id": batch["id"], "next": "wait_upload"}


def wait_upload(state: FlowState, runtime: Runtime[RunContext]):
    card = state["card"]
    value = interrupt(_pending("upload_papers", card, {
        "type": "upload_papers", "batch_id": state["batch_id"], "file_count": state.get("file_count", 0),
    }))
    ctx = _ctx(runtime)
    paper_ids = list(dict.fromkeys(value.get("paper_ids") or []))[:MAX_FILES]
    if not paper_ids:
        return _fail(ctx, card, "文件都没能上传成功，可以检查文件后重新开始。")
    # 不信任浏览器：每份论文都必须属于本批次、对当前用户可见。
    for paper_id in paper_ids:
        try:
            paper = guards.visible_paper(ctx.db, paper_id, ctx.principal)
        except HTTPException:
            paper = None
        if paper is None or paper.batch_id != state["batch_id"]:
            return _fail(ctx, card, "上传结果校验失败：有文件不属于这个任务。请重新开始。")
    ctx.show_workspace("/tasks/new?batch=%s" % state["batch_id"])
    return {"paper_ids": paper_ids, "next": "precheck"}


def precheck(state: FlowState, runtime: Runtime[RunContext]):
    ctx = _ctx(runtime)
    card = state["card"]
    try:
        result = tools.call(ctx, "run_precheck", allow_write=True,
                            batch_id=state["batch_id"], paper_ids=state["paper_ids"])
    except HTTPException as exc:
        return _fail(ctx, card, "预检失败：%s" % _detail(exc))
    removable, stuck = blocking_findings(result)
    if stuck:
        lines = "\n".join("· %s：%s" % (f["file_name"], f["message"]) for f in stuck)
        ctx.update_card(card["message_id"], card["card_id"], "done", {"batch_id": state["batch_id"], "stage": "blocked"})
        ctx.say("有 %d 份文件还没解析完，暂时不能开始评分：\n%s\n请稍后在右侧页面重试解析。" % (len(stuck), lines))
        return {"next": END}
    if removable:
        ctx.update_card(card["message_id"], card["card_id"], "done", {"batch_id": state["batch_id"], "stage": "blocked"})
        card_id = new_card_id("blocked")
        lines = "\n".join("· %s：%s" % (f["file_name"], f["message"]) for f in removable)
        message = ctx.say(
            "有 %d 份文件无法评分：\n%s\n移除它们，评其余的文件吗？" % (len(removable), lines),
            [{"id": card_id, "type": "blocked_files", "status": "proposed", "batch_id": state["batch_id"],
              "papers": [{"paper_id": f["paper_id"], "file_name": f["file_name"]} for f in removable]}],
        )
        return {"removable": [{"paper_id": f["paper_id"], "file_name": f["file_name"]} for f in removable],
                "card": _card_ref(message, card_id), "next": "wait_remove"}
    return {"next": "start_scoring"}


def wait_remove(state: FlowState, runtime: Runtime[RunContext]):
    card = state["card"]
    value = interrupt(_pending("confirm_remove", card))
    ctx = _ctx(runtime)
    if value["action"] == "cancel":
        ctx.update_card(card["message_id"], card["card_id"], "dismissed")
        ctx.say("好的，先不评。任务已保存为草稿，可以在右侧页面处理后再开始。")
        return {"next": END}
    ctx.update_card(card["message_id"], card["card_id"], "running")
    return {"next": "remove_papers"}


def remove_papers(state: FlowState, runtime: Runtime[RunContext]):
    ctx = _ctx(runtime)
    card = state["card"]
    for paper in state.get("removable") or []:
        if ctx.db.get(Paper, paper["paper_id"]) is None:
            continue  # 重跑时已删过
        try:
            tools.call(ctx, "remove_paper", allow_write=True, paper_id=paper["paper_id"])
        except HTTPException as exc:
            return _fail(ctx, card, "移除文件失败：%s" % _detail(exc))
    remaining = [pid for pid in state["paper_ids"] if ctx.db.get(Paper, pid) is not None]
    if not remaining:
        ctx.update_card(card["message_id"], card["card_id"], "done", {"removed": len(state.get("removable") or [])})
        ctx.say("移除后没有可评分的文件了。")
        return {"next": END}
    try:
        result = tools.call(ctx, "run_precheck", allow_write=True, batch_id=state["batch_id"], paper_ids=remaining)
    except HTTPException as exc:
        return _fail(ctx, card, "预检失败：%s" % _detail(exc))
    if not result.get("can_start"):
        return _fail(ctx, card, "还有文件不能评分，请在右侧页面处理后再开始。")
    ctx.show_workspace("/tasks/new?batch=%s" % state["batch_id"])
    return {"paper_ids": remaining, "next": "start_scoring"}


def start_scoring(state: FlowState, runtime: Runtime[RunContext]):
    ctx = _ctx(runtime)
    card = state["card"]
    if state.get("job_id"):
        return {"next": "wait_job"}
    try:
        job = tools.call(ctx, "start_scoring", allow_write=True, batch_id=state["batch_id"])
    except HTTPException as exc:
        return _fail(ctx, card, "开始评分失败：%s" % _detail(exc))
    ctx.update_card(card["message_id"], card["card_id"], "done", {"batch_id": state["batch_id"], "job_id": job["id"]})
    ctx.set_focus(job_id=job["id"])
    ctx.show_workspace("/tasks/%s/run" % state["batch_id"])
    progress_id = new_card_id("job")
    message = ctx.say(
        "已开始评分，共 %d 份。右侧是实时进度，评完我会告诉你结果。" % len(state["paper_ids"]),
        [{"id": progress_id, "type": "job_progress", "status": "running",
          "batch_id": state["batch_id"], "job_id": job["id"]}],
    )
    return {"job_id": job["id"], "progress_card": _card_ref(message, progress_id), "next": "wait_job"}


# --- 等评分结束、自动重试与汇报 --------------------------------------------------------------

def post_progress(state: FlowState, runtime: Runtime[RunContext]):
    """watch 模式：手动重试之后观察已存在的作业。"""

    ctx = _ctx(runtime)
    progress_id = new_card_id("job")
    message = ctx.say("失败项已重新排队，评完我会告诉你结果。", [{
        "id": progress_id, "type": "job_progress", "status": "running",
        "batch_id": state["batch_id"], "job_id": state["job_id"],
    }])
    ctx.set_focus(job_id=state["job_id"])
    ctx.show_workspace("/tasks/%s/run" % state["batch_id"])
    return {"progress_card": _card_ref(message, progress_id), "next": "wait_job"}


def wait_job(state: FlowState, runtime: Runtime[RunContext]):
    card = state["progress_card"]
    interrupt(_pending("wait_job", card, {
        "type": "watch_job", "batch_id": state["batch_id"], "job_id": state["job_id"],
    }))
    ctx = _ctx(runtime)
    # 浏览器说“评完了”不算数，以服务器上的作业状态为准（方案 §7.3）。
    try:
        job = tools.call(ctx, "job", job_id=state["job_id"])
    except HTTPException as exc:
        return _fail(ctx, card, "读取评分作业失败：%s" % _detail(exc))
    if job["status"] not in TERMINAL_JOB_STATUSES:
        return {"next": "wait_job"}
    return {"next": "finish_job"}


def finish_job(state: FlowState, runtime: Runtime[RunContext]):
    ctx = _ctx(runtime)
    card = state["progress_card"]
    job = tools.call(ctx, "job", job_id=state["job_id"])
    if job["status"] in ACTIVE_JOB_STATUSES:
        return {"next": "wait_job"}  # 重跑时作业已被本节点重排，继续等
    summary = tools.call(ctx, "batch_summary", batch_id=state["batch_id"])
    papers_by_id = {paper["paper_id"]: paper for paper in summary["papers"]}
    diagnosis = diagnose_job(job["items"], papers_by_id)
    retryable = job["status"] in {"completed_with_errors", "failed", "canceled"} and any(
        item["status"] in {"failed", "canceled", "running"} for item in job["items"]
    )
    auto_retry = (
        job["status"] != "canceled" and diagnosis["transient_only"]
        and not state.get("auto_retry_used") and retryable
    )
    ctx.update_card(card["message_id"], card["card_id"], "done", {"status": job["status"], "auto_retried": auto_retry})

    if job["status"] == "canceled":
        ctx.say("评分已取消。已完成的 %d 份结果保留。" % (job["succeeded_count"] + job["skipped_count"]),
                [quick_actions_card(["query_overview"])])
        return {"next": END}
    if auto_retry:
        # 临时性错误只重跑失败的那几篇，已成功的结果复用，不影响评分结果（U4、B3）。
        labels = "、".join(group["label"] for group in diagnosis["groups"])
        try:
            tools.call(ctx, "retry_scoring", allow_write=True, job_id=state["job_id"])
        except HTTPException as exc:
            ctx.say("自动重试失败：%s" % _detail(exc))
            return {"next": END}
        progress_id = new_card_id("job")
        message = ctx.say(
            "有 %d 份因临时性错误（%s）没有评完，已自动重试一次。" % (diagnosis["failed_count"], labels),
            [{"id": progress_id, "type": "job_progress", "status": "running",
              "batch_id": state["batch_id"], "job_id": state["job_id"]}],
        )
        return {"auto_retry_used": True, "progress_card": _card_ref(message, progress_id), "next": "wait_job"}

    if job["status"] == "failed" and not diagnosis["failed_count"]:
        ctx.say("评分作业执行失败，没有产出结果。可以在右侧页面查看详情后重试。",
                [quick_actions_card(["query_progress"])])
        return {"next": END}

    overview = score_overview(summary["papers"])
    cards: list[dict] = []
    if diagnosis["failed_count"]:
        cards.append({
            "type": "diagnosis", "status": "proposed" if retryable else "info",
            "batch_id": state["batch_id"], "job_id": state["job_id"],
            "groups": diagnosis["groups"], "failed_count": diagnosis["failed_count"],
        })
    cards.append({"type": "overview", "batch_id": state["batch_id"]})
    cards.append(quick_actions_card(["query_paper", "query_review"]))
    head = ("评分结束，%d 份没有评完（原因见下）。" % diagnosis["failed_count"]) if diagnosis["failed_count"] else "评分完成。"
    ctx.say(head + overview_text(overview), cards)
    ctx.show_workspace("/batches/%s/grade" % state["batch_id"])
    return {"next": END}


# --- 场景 B：用户上传评分规则与模板 -------------------------------------------------------------

def ask_rubric_files(state: FlowState, runtime: Runtime[RunContext]):
    ctx = _ctx(runtime)
    card_id = new_card_id("rubricfiles")
    message = ctx.say(
        "请选择评分规则文件和评分模板文件（Word 或 Excel，至少一份），并分别说明是哪一份。"
        "导入后在右侧核对并发布，发布后我会接着让你上传待评分文件。",
        [{"id": card_id, "type": "pick_rubric_files", "status": "proposed"}],
        intent="import_rubric",
    )
    return {"card": _card_ref(message, card_id), "next": "wait_import"}


def wait_import(state: FlowState, runtime: Runtime[RunContext]):
    card = state["card"]
    value = interrupt(_pending("create_import_session", card, {"type": "create_import_session"}))
    ctx = _ctx(runtime)
    if value.get("action") == "cancel":
        ctx.update_card(card["message_id"], card["card_id"], "dismissed")
        ctx.say("好的，不导入了。")
        return {"next": END}
    try:
        session = guards.visible_import_session(ctx.db, value["import_session_id"], ctx.principal)
    except HTTPException:
        session = None
    if session is None or session.owner_id != ctx.user_id or session.status != "draft":
        return _fail(ctx, card, "导入草稿校验失败，请重新上传评分规则和模板。")
    ctx.update_card(card["message_id"], card["card_id"], "done", {"import_session_id": session.id})
    ctx.set_focus(rubric_id=None, rubric_name=session.name, batch_id=None, batch_name=None, job_id=None, paper_id=None)
    ctx.show_workspace("/rubrics?import_session=%s" % session.id)
    wait_id = new_card_id("rubricwait")
    message = ctx.say(
        "已解析《%s》，共 %d 个评分项。请在右侧核对评分项并确认，然后完成规则核对与发布。发布后我会继续。"
        % (session.name, len(import_sessions.serialize(session).get("criteria") or [])),
        [{"id": wait_id, "type": "rubric_wait", "status": "running", "import_session_id": session.id}],
    )
    return {"import_session_id": session.id, "import_name": session.name,
            "card": _card_ref(message, wait_id), "next": "wait_publish"}


def wait_publish(state: FlowState, runtime: Runtime[RunContext]):
    card = state["card"]
    value = interrupt(_pending("wait_publish", card, {
        "type": "watch_rubric", "import_session_id": state["import_session_id"],
    }))
    ctx = _ctx(runtime)
    if value.get("action") == "cancel":
        ctx.update_card(card["message_id"], card["card_id"], "dismissed")
        return {"next": END}
    try:
        session = guards.visible_import_session(ctx.db, state["import_session_id"], ctx.principal)
    except HTTPException as exc:
        if exc.status_code == 410:
            return _fail(ctx, card, "导入草稿已过期，请重新上传评分规则和模板。")
        return _fail(ctx, card, "读取导入草稿失败：%s" % _detail(exc))
    if session.status == "cancelled":
        return _fail(ctx, card, "导入已取消。")
    if session.status != "confirmed" or not session.rubric_id:
        return {"next": "wait_publish"}
    try:
        rubric = guards.visible_rubric(ctx.db, session.rubric_id, ctx.principal)
    except HTTPException as exc:
        return _fail(ctx, card, "读取评分标准失败：%s" % _detail(exc))
    ctx.set_focus(rubric_id=rubric.id, rubric_name=rubric.name)
    if rubric.status != "published":
        return {"next": "wait_publish"}
    ctx.update_card(card["message_id"], card["card_id"], "done", {"rubric_id": rubric.id})
    ctx.say("评分标准《%s》%s 已发布。" % (rubric.name, rubric.version))
    return {"preset_rubric_id": rubric.id, "rubric_name": rubric.name, "rubric_version": rubric.version,
            "mode": "grading", "file_count": 0, "next": "ask_files"}


# --- 组装 ----------------------------------------------------------------------------------

NODES = {
    "begin": begin,
    "ask_files": ask_files,
    "wait_files": wait_files,
    "propose_start": propose_start,
    "wait_confirm": wait_confirm,
    "create_batch": create_batch,
    "wait_upload": wait_upload,
    "precheck": precheck,
    "wait_remove": wait_remove,
    "remove_papers": remove_papers,
    "start_scoring": start_scoring,
    "post_progress": post_progress,
    "wait_job": wait_job,
    "finish_job": finish_job,
    "ask_rubric_files": ask_rubric_files,
    "wait_import": wait_import,
    "wait_publish": wait_publish,
}

#: 每个节点可能去往的下一个节点（`next` 字段），供条件边声明与图校验。
ROUTES = {
    "begin": ["ask_files", "propose_start", "ask_rubric_files", "post_progress"],
    "ask_files": ["wait_files"],
    "wait_files": ["propose_start"],
    "propose_start": ["wait_confirm", END],
    "wait_confirm": ["create_batch", "propose_start", "ask_files", "wait_confirm", END],
    "create_batch": ["wait_upload", END],
    "wait_upload": ["precheck", END],
    "precheck": ["wait_remove", "start_scoring", END],
    "wait_remove": ["remove_papers", END],
    "remove_papers": ["start_scoring", END],
    "start_scoring": ["wait_job", END],
    "post_progress": ["wait_job"],
    "wait_job": ["finish_job", "wait_job", END],
    "finish_job": ["wait_job", END],
    "ask_rubric_files": ["wait_import"],
    "wait_import": ["wait_publish", END],
    "wait_publish": ["wait_publish", "ask_files", END],
}


def build_flow_graph(checkpointer):
    builder = StateGraph(FlowState, context_schema=RunContext)
    for name, fn in NODES.items():
        builder.add_node(name, fn)
    builder.add_edge(START, "begin")
    for name, targets in ROUTES.items():
        builder.add_conditional_edges(name, lambda state: state["next"], targets)
    return builder.compile(checkpointer=checkpointer)
