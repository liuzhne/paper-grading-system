"""评分助手的查询图（LangGraph，无状态，方案 §7.1）。

查询不保存流程状态：流程停在“等评分结束”时，用户照样可以随时查询，不会打断
或改变流程。需要用户选择时（选任务、选论文）给出选择卡片，所需参数放在卡片里，
选完由运行器再发起一次查询。

分数、扣分点、证据由前端从接口现取渲染，这里只放引用编号与模板文案（B8）。
"""

from __future__ import annotations

from typing import TypedDict

from fastapi import HTTPException
from langgraph.graph import END
from langgraph.graph import START
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime

from backend.app.services.assistant import tools
from backend.app.services.assistant.context import RunContext
from backend.app.services.assistant.views import overview_text
from backend.app.services.assistant.views import papers_in_upload_order
from backend.app.services.assistant.views import quick_actions_card
from backend.app.services.assistant.views import resolve_paper
from backend.app.services.assistant.views import score_overview

ACTIVE_JOB_STATUSES = {"queued", "running", "cancel_requested"}
RETRYABLE_ITEM_STATUSES = {"failed", "canceled", "running"}

QUERY_INTENTS = ("query_progress", "query_paper", "query_review", "query_overview", "retry_failed", "cancel_job")


class QueryState(TypedDict, total=False):
    intent: str
    ordinal: int | None
    name: str | None
    text: str
    batch_id: str | None
    paper_id: str | None
    next: str


def _ctx(runtime: Runtime[RunContext]) -> RunContext:
    return runtime.context


def resolve_batch(state: QueryState, runtime: Runtime[RunContext]):
    ctx = _ctx(runtime)
    if state.get("batch_id"):
        return {"next": state["intent"]}
    focus_batch = (ctx.conversation.focus or {}).get("batch_id")
    if focus_batch:
        try:
            tools.call(ctx, "batch_summary", batch_id=focus_batch)
            return {"batch_id": focus_batch, "next": state["intent"]}
        except HTTPException:
            # 焦点里的任务已删除或无权访问：不沿用旧信息回答（方案 §9.3）。
            ctx.set_focus(batch_id=None, batch_name=None, job_id=None, paper_id=None)
            ctx.say("之前的评分任务现在不可用了（可能已删除或无权访问），换一个吧。")
    batches = tools.call(ctx, "list_recent_batches", limit=8)
    if not batches:
        ctx.say("还没有评分任务。要开始评分吗？", [quick_actions_card(["start_grading"])])
        return {"next": END}
    if len(batches) == 1:
        ctx.set_focus(batch_id=batches[0]["id"], batch_name=batches[0]["name"])
        return {"batch_id": batches[0]["id"], "next": state["intent"]}
    ctx.say("要查哪个评分任务？", [{
        "type": "choose_batch", "status": "proposed", "next_intent": state["intent"],
        "paper_ordinal": state.get("ordinal"), "paper_name": state.get("name"),
        "text": (state.get("text") or "")[:200], "batches": batches,
    }])
    return {"next": END}


def query_progress(state: QueryState, runtime: Runtime[RunContext]):
    ctx = _ctx(runtime)
    batch_id = state["batch_id"]
    job = tools.call(ctx, "latest_job", batch_id=batch_id)
    ctx.show_workspace("/tasks/%s/run" % batch_id)
    if job is None:
        ctx.say("这个任务还没有开始评分。")
        return {"next": END}
    finished = job["succeeded_count"] + job["skipped_count"] + job["failed_count"] + job["canceled_count"]
    if job["status"] in ACTIVE_JOB_STATUSES:
        text = "正在评分：已完成 %d/%d 份。" % (finished, job["total_items"])
    else:
        text = "评分已结束：完成 %d 份，失败 %d 份。" % (job["succeeded_count"] + job["skipped_count"], job["failed_count"])
    ctx.say(text, [{"type": "job_progress", "status": "info", "batch_id": batch_id, "job_id": job["id"]}],
            intent="query_progress")
    return {"next": END}


def _show_paper(ctx: RunContext, batch_id: str, paper: dict, ordinal: int):
    ctx.set_focus(paper_id=paper["paper_id"])
    ctx.show_workspace("/batches/%s/grade?paper=%s" % (batch_id, paper["paper_id"]))
    name = paper.get("student_name") or paper.get("file_name")
    if not paper.get("latest_run_id"):
        ctx.say("第 %d 份《%s》还没有评分结果。" % (ordinal, name))
        return
    score = "未出总分" if paper.get("latest_final_score") is None else "总分 %s" % paper["latest_final_score"]
    review = "，有评分项需要人工复核" if paper.get("latest_need_manual_review") else ""
    ctx.say(
        "第 %d 份《%s》：%s%s。各项得分、扣分点与证据如下，右侧可以对照原文。" % (ordinal, name, score, review),
        [{"type": "paper_result", "batch_id": batch_id, "paper_id": paper["paper_id"], "run_id": paper["latest_run_id"]}],
        intent="query_paper",
    )


def query_paper(state: QueryState, runtime: Runtime[RunContext]):
    ctx = _ctx(runtime)
    batch_id = state["batch_id"]
    summary = tools.call(ctx, "batch_summary", batch_id=batch_id)
    papers = papers_in_upload_order(summary["papers"])
    if not papers:
        ctx.say("这个任务里还没有文件。")
        return {"next": END}
    if state.get("paper_id"):
        match = next((paper for paper in papers if paper["paper_id"] == state["paper_id"]), None)
        candidates = papers
    else:
        match, candidates = resolve_paper(papers, ordinal=state.get("ordinal"), name=state.get("name"),
                                          text=state.get("text") or "")
    if match is not None:
        _show_paper(ctx, batch_id, match, papers.index(match) + 1)
        return {"next": END}
    prompt = "这个任务只有 %d 份文件，要查哪一份？" % len(papers) if state.get("ordinal") else "要查哪一份？"
    ctx.say(prompt, [{
        "type": "choose_paper", "status": "proposed", "batch_id": batch_id,
        "papers": [{"paper_id": paper["paper_id"],
                    "label": "%d. %s" % (papers.index(paper) + 1, paper.get("student_name") or paper.get("file_name"))}
                   for paper in candidates[:30]],
    }])
    return {"next": END}


def query_review(state: QueryState, runtime: Runtime[RunContext]):
    ctx = _ctx(runtime)
    batch_id = state["batch_id"]
    summary = tools.call(ctx, "batch_summary", batch_id=batch_id)
    flagged = [paper for paper in summary["papers"] if paper.get("latest_need_manual_review")]
    ctx.show_workspace("/batches/%s/grade" % batch_id)
    if not flagged:
        ctx.say("没有需要人工复核的论文。")
        return {"next": END}
    ctx.say("有 %d 份论文需要人工复核。点开某一份可以看到具体是哪几项、为什么；复核请在右侧的评分工作区完成。" % len(flagged),
            [{"type": "review_list", "batch_id": batch_id}], intent="query_review")
    return {"next": END}


def query_overview(state: QueryState, runtime: Runtime[RunContext]):
    ctx = _ctx(runtime)
    batch_id = state["batch_id"]
    summary = tools.call(ctx, "batch_summary", batch_id=batch_id)
    ctx.show_workspace("/batches/%s/grade" % batch_id)
    ctx.say(overview_text(score_overview(summary["papers"])), [{"type": "overview", "batch_id": batch_id}],
            intent="query_overview")
    return {"next": END}


def retry_failed(state: QueryState, runtime: Runtime[RunContext]):
    """重试是写动作：这里只给确认卡片，用户确认后由运行器执行（方案 §7.6）。"""

    ctx = _ctx(runtime)
    batch_id = state["batch_id"]
    job = tools.call(ctx, "latest_job", batch_id=batch_id)
    retryable = job is not None and job["status"] in {"completed_with_errors", "failed", "canceled"} and any(
        item["status"] in RETRYABLE_ITEM_STATUSES for item in job["items"]
    )
    if not retryable:
        ctx.say("没有可以重试的失败项。")
        return {"next": END}
    count = sum(1 for item in job["items"] if item["status"] in RETRYABLE_ITEM_STATUSES)
    ctx.say("有 %d 份没有评完。重试它们吗？已成功的结果会保留。" % count, [{
        "type": "confirm_retry", "status": "proposed", "batch_id": batch_id, "job_id": job["id"], "count": count,
    }])
    return {"next": END}


def cancel_job(state: QueryState, runtime: Runtime[RunContext]):
    ctx = _ctx(runtime)
    batch_id = state["batch_id"]
    job = tools.call(ctx, "latest_job", batch_id=batch_id)
    if job is None or job["status"] not in ACTIVE_JOB_STATUSES:
        ctx.say("当前没有进行中的评分。")
        return {"next": END}
    finished = job["succeeded_count"] + job["skipped_count"] + job["failed_count"] + job["canceled_count"]
    ctx.say("正在评分（%d/%d）。取消剩余的吗？已完成的结果会保留。" % (finished, job["total_items"]), [{
        "type": "confirm_cancel", "status": "proposed", "batch_id": batch_id, "job_id": job["id"],
    }])
    return {"next": END}


def build_query_graph():
    builder = StateGraph(QueryState, context_schema=RunContext)
    builder.add_node("resolve_batch", resolve_batch)
    for name, fn in (
        ("query_progress", query_progress), ("query_paper", query_paper), ("query_review", query_review),
        ("query_overview", query_overview), ("retry_failed", retry_failed), ("cancel_job", cancel_job),
    ):
        builder.add_node(name, fn)
        builder.add_edge(name, END)
    builder.add_edge(START, "resolve_batch")
    builder.add_conditional_edges("resolve_batch", lambda state: state["next"], [*QUERY_INTENTS, END])
    return builder.compile()
