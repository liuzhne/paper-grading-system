"""模型可以调用的业务工具（Agent 化方案 §11.2）。

每个工具对应老师的一个问题，经一期的工具层（`services/assistant/tools.py`）走共享守卫与服务层，与页面接口
同一权限边界。约定：

- 返回名称、序号与引用编号，统计、排序由代码完成；**不返回分数数字**（分数由卡片显示，模型不转述）；
- 不返回学生原文：扣分条目只给理由（疑似注入的不给），证据只给条数；
- 列表默认 10 条，附“还有 N 条”；
- 提议工具只生成确认卡片（或进入一期的流程），写动作由老师确认后由代码执行；
- 涉及某个任务或材料时更新焦点与右侧工作区，下一轮的 `t0`、`p0` 就是它。

常驻：`help_lookup`、`task_list`、`task_overview`、`paper_result`。原方案的 `paper_find` 并入 `paper_result`
（按编号、序号或姓名定位，不唯一时返回候选），查一份少一步调用。
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import select

from backend.app.db.models import Paper
from backend.app.db.models import RubricCriterion
from backend.app.db.models import ScoreItem
from backend.app.services.assistant import tools
from backend.app.services.assistant.agent import help as help_entries
from backend.app.services.assistant.agent.catalog import AgentTool
from backend.app.services.assistant.agent.catalog import register
from backend.app.services.assistant.agent.refs import ToolError
from backend.app.services.assistant.agent.refs import TurnContext
from backend.app.services.assistant.views import document_type_label
from backend.app.services.assistant.views import paper_label
from backend.app.services.assistant.views import papers_in_upload_order
from backend.app.services.assistant.views import resolve_paper
from backend.app.services.scoring.core.evidence import detect_injection

LIST_LIMIT = 10
ACTIVE_JOB_STATUSES = {"queued", "running", "cancel_requested"}
RETRYABLE_ITEM_STATUSES = {"failed", "canceled", "running"}
TASK_REF_NOTE = "任务编号（来自 task_list 等工具的结果，或当前状态里的 t0）；不填就用当前任务，没有当前任务时用最近的一个"


# --- 公用 -------------------------------------------------------------------------

def _overview_card(batch_id: str) -> dict:
    return {"type": "overview", "batch_id": batch_id}


SEARCH_TASKS = 8


def _task_id(turn: TurnContext, task_ref: str | None) -> str:
    if task_ref:
        return turn.refs.resolve(task_ref, "t")
    focus = turn.run.conversation.focus or {}
    if focus.get("batch_id"):
        return focus["batch_id"]
    recent = tools.call(turn.run, "list_recent_batches", limit=1)
    if not recent:
        raise ToolError("还没有评分任务。")
    return recent[0]["id"]


def _has_task(turn: TurnContext, task_ref: str | None) -> bool:
    """老师指定了任务，或对话里有当前任务。"""

    return bool(task_ref) or bool((turn.run.conversation.focus or {}).get("batch_id"))


def _task_choices(turn: TurnContext) -> list[dict]:
    batches = tools.call(turn.run, "list_recent_batches", limit=SEARCH_TASKS)
    return [{"ref": turn.refs.add("t", batch["id"], card=_overview_card(batch["id"])), "name": batch["name"]}
            for batch in batches]


def _task(turn: TurnContext, task_ref: str | None) -> tuple[str, dict]:
    batch_id = _task_id(turn, task_ref)
    summary = tools.call(turn.run, "batch_summary", batch_id=batch_id)
    if (turn.run.conversation.focus or {}).get("batch_id") != batch_id:
        # 换了任务：旧任务里的材料不再是“当前材料”。
        turn.run.set_focus(batch_id=batch_id, batch_name=summary["batch_name"], paper_id=None, job_id=None)
    else:
        turn.run.set_focus(batch_name=summary["batch_name"])
    return batch_id, summary


def _profile(summary: dict) -> str:
    return summary.get("business_profile_key") or "thesis"


def _paper_card(batch_id: str, paper: dict) -> dict:
    return {"type": "paper_result", "batch_id": batch_id, "paper_id": paper["paper_id"], "run_id": paper["latest_run_id"]}


def _paper_entry(turn: TurnContext, batch_id: str, paper: dict, ordinal: int, profile: str) -> dict:
    card = _paper_card(batch_id, paper) if paper.get("latest_run_id") else None
    return {"ref": turn.refs.add("p", paper["paper_id"], card=card), "序号": ordinal,
            "名称": paper_label(paper, profile), "状态": _paper_status(paper)}


def _paper_status(paper: dict) -> str:
    if not paper.get("latest_run_id"):
        return "解析失败" if paper.get("paper_status") == "failed" else "还没有评分结果"
    return "需要人工复核" if paper.get("latest_need_manual_review") else "已评分"


def _items(db, run_ids: list[str]):
    if not run_ids:
        return []
    return db.execute(
        select(ScoreItem, RubricCriterion.name, RubricCriterion.display_order)
        .join(RubricCriterion, RubricCriterion.id == ScoreItem.criterion_id)
        .where(ScoreItem.scoring_run_id.in_(run_ids))
    ).all()


def _loss_rate(item: ScoreItem) -> float:
    maximum = float(item.max_score or 0)
    if maximum <= 0:
        return 0.0
    awarded = item.final_score if item.final_score is not None else item.ai_score
    return max(0.0, (maximum - float(awarded or 0)) / maximum)


def _job(turn: TurnContext, batch_id: str) -> dict | None:
    return tools.call(turn.run, "latest_job", batch_id=batch_id)


def _finished(job: dict) -> int:
    return job["succeeded_count"] + job["skipped_count"] + job["failed_count"] + job["canceled_count"]


class TaskParams(BaseModel):
    task_ref: str | None = Field(default=None, max_length=10)


# --- help_lookup --------------------------------------------------------------------

class HelpParams(BaseModel):
    topic: str = Field(min_length=1, max_length=100)


def _help_lookup(turn: TurnContext, params: HelpParams) -> dict:
    found = help_entries.lookup(params.topic)
    for entry in found:
        turn.refs.help_seen.add(entry.key)
    if not found:
        return {"entries": [], "提示": "没有对应的帮助条目；不知道的功能就直说，不要猜。",
                "可查的条目": [entry.key for entry in help_entries.ENTRIES]}
    return {"entries": [{"ref": entry.key, "标题": entry.title, "内容": entry.text} for entry in found],
            "说明": "回答用法问题时，把 ref 放进 reply 的 help_refs，界面会显示条目原文；text 只写一两句引导。"}


register(AgentTool(
    name="help_lookup", group="help", title="帮助条目",
    purpose="查系统的用法：怎么开始评分、怎么导入评分标准、在哪复核和改分、失败了怎么办等",
    when="老师问“怎么……”“在哪……”“是什么”“有什么区别”时先查；不用于查询具体任务或材料的数据",
    params=HelpParams, param_notes={"topic": "老师问的是什么，用他的说法，例如“模板怎么上传”"},
    returns="entries：[{ref, 标题, 内容}]，最多 3 条",
    kind="read", risk="low", resident=True,
    keywords=("帮助", "怎么用", "怎么上传", "在哪", "区别", "用法"),
    eval_phrases=("模板怎么上传", "评分标准和评分模板有什么区别", "在哪改分"),
    run=_help_lookup,
))


# --- task_list ----------------------------------------------------------------------

class TaskListParams(BaseModel):
    limit: int = Field(default=8, ge=1, le=20)


def _task_list(turn: TurnContext, params: TaskListParams) -> dict:
    batches = tools.call(turn.run, "list_recent_batches", limit=params.limit)
    return {
        "tasks": [{"ref": turn.refs.add("t", batch["id"], card=_overview_card(batch["id"])), "name": batch["name"]}
                  for batch in batches],
        "说明": "按创建时间倒序；没有任务时为空列表。要看某个任务的情况，用 task_overview。",
    }


register(AgentTool(
    name="task_list", group="task", title="评分任务列表",
    purpose="列出当前组织最近的评分任务（名称与编号）",
    when="老师问有哪些任务、要在几个任务里选一个时用；问某个任务的进度或分数时，先用它拿到任务编号",
    params=TaskListParams, param_notes={"limit": "最多返回几个任务，默认 8，最多 20"},
    returns="tasks：[{ref, name}]，最多 limit 个",
    kind="read", risk="low", resident=True,
    keywords=("任务", "评分任务", "批次", "有哪些", "列表", "最近"),
    eval_phrases=("现在有哪些评分任务", "我最近建了哪些任务", "列一下评分批次"),
    run=_task_list,
))


# --- task_overview ------------------------------------------------------------------

def _task_overview(turn: TurnContext, params: TaskParams) -> dict:
    chose_latest = not _has_task(turn, params.task_ref)
    batch_id, summary = _task(turn, params.task_ref)
    profile = _profile(summary)
    papers = papers_in_upload_order(summary["papers"])
    job = _job(turn, batch_id)
    ref = turn.refs.add("t", batch_id, card=_overview_card(batch_id))
    scored = [paper for paper in papers if paper.get("latest_run_id")]
    result = {
        "task": {"ref": ref, "name": summary["batch_name"], "文档类型": document_type_label(profile)},
        "材料": {"总数": len(papers), "已评分": len(scored),
               "需要人工复核": sum(1 for paper in papers if paper.get("latest_need_manual_review")),
               "还没有评分结果": len(papers) - len(scored)},
    }
    if job is None:
        result["评分"] = "还没有开始评分"
    else:
        result["评分"] = {"状态": job["status"], "已完成": _finished(job), "总数": job["total_items"],
                        "失败": job["failed_count"]}
    rates: dict[str, list[float]] = defaultdict(list)
    order: dict[str, int] = {}
    for item, name, display_order in _items(turn.run.db, [paper["latest_run_id"] for paper in scored]):
        rates[name].append(_loss_rate(item))
        order[name] = display_order or 0
    if rates:
        ranked = sorted(rates, key=lambda name: (-sum(rates[name]) / len(rates[name]), order[name]))
        result["失分从多到少的评分项"] = ranked[:5]
    lowest = sorted((paper for paper in scored if paper.get("latest_final_score") is not None),
                    key=lambda paper: paper["latest_final_score"])[:3]
    if lowest:
        result["总分最低的材料"] = [_paper_entry(turn, batch_id, paper, papers.index(paper) + 1, profile) for paper in lowest]
    result["说明"] = "这里不给分数数字；要展示分数，把 task.ref 或材料的 ref 放进 reply 的 cards。"
    if chose_latest:
        result["说明"] += "老师没有指定任务，用的是最近的一个；不对的话先用 task_list 找到任务再调用。"
    turn.run.show_workspace(("/tasks/%s/run" if job and job["status"] in ACTIVE_JOB_STATUSES else "/batches/%s/grade") % batch_id)
    return result


register(AgentTool(
    name="task_overview", group="task", title="评分任务概况",
    purpose="一个评分任务的进度、材料数、需要复核与没评完的数量、失分最多的评分项、总分最低的几份",
    when="老师问某个任务评得怎么样、评完没有、哪个评分项丢分最多、哪几份最差时用；问某一份的细节用 paper_result",
    params=TaskParams, param_notes={"task_ref": TASK_REF_NOTE},
    returns="task、材料计数、评分进度、失分从多到少的评分项（最多 5 个）、总分最低的材料（最多 3 份，带 ref）；不含分数数字",
    kind="read", risk="low", resident=True,
    keywords=("进度", "评完", "概况", "整体", "平均", "失分最多", "最差", "分布"),
    eval_phrases=("这批评得怎么样", "评分进度", "哪个评分项丢分最多", "总分最低的是哪几份"),
    run=_task_overview,
))


# --- paper_result -------------------------------------------------------------------

class PaperParams(BaseModel):
    task_ref: str | None = Field(default=None, max_length=10)
    paper_ref: str | None = Field(default=None, max_length=10)
    ordinal: int | None = Field(default=None, ge=1, le=999)
    name: str | None = Field(default=None, max_length=50)


def _search_tasks(turn: TurnContext, name: str):
    """没有当前任务时按姓名、标题或文件名在最近的几个任务里一起找（§11.2 paper_find 的职责）。"""

    needle = name.strip()
    hits = []
    for batch in tools.call(turn.run, "list_recent_batches", limit=SEARCH_TASKS):
        summary = tools.call(turn.run, "batch_summary", batch_id=batch["id"])
        papers = papers_in_upload_order(summary["papers"])
        profile = _profile(summary)
        for paper in papers:
            if needle and (needle in paper_label(paper, profile) or needle in (paper.get("file_name") or "")):
                hits.append((batch["id"], summary, papers, paper))
    return hits


def _locate(turn: TurnContext, params: PaperParams):
    if not params.paper_ref and not _has_task(turn, params.task_ref):
        if not params.name:
            return None  # 只有“第几份”没有任务：序号离开任务没有意义
        hits = _search_tasks(turn, params.name)
        if len(hits) == 1:
            batch_id, summary, papers, paper = hits[0]
            _task(turn, turn.refs.add("t", batch_id, card=_overview_card(batch_id)))
            return batch_id, summary, papers, paper, papers
        if hits:
            return "candidates", hits
    if params.paper_ref:
        paper_id = turn.refs.resolve(params.paper_ref, "p")
        paper = turn.run.db.get(Paper, paper_id)
        if paper is None:
            raise ToolError("这份材料不存在了。")
        batch_id, summary = _task(turn, turn.refs.add("t", paper.batch_id, card=_overview_card(paper.batch_id)))
    else:
        batch_id, summary = _task(turn, params.task_ref)
        paper_id = None
    papers = papers_in_upload_order(summary["papers"])
    if paper_id:
        match = next((paper for paper in papers if paper["paper_id"] == paper_id), None)
        return batch_id, summary, papers, match, papers
    if params.ordinal is None and not params.name:
        return batch_id, summary, papers, None, papers
    match, candidates = resolve_paper(papers, ordinal=params.ordinal, name=params.name, text="",
                                      profile_key=_profile(summary))
    return batch_id, summary, papers, match, candidates


def _paper_result(turn: TurnContext, params: PaperParams) -> dict:
    located = _locate(turn, params)
    if located is None:
        return {"提示": "没有当前任务：请先确定是哪个任务（task_ref），再按序号找。", "tasks": _task_choices(turn)}
    if located[0] == "candidates":
        return {"提示": "几个任务里都有对应的材料：请老师选一份，或用 ref 再调用。", "候选": [
            {**_paper_entry(turn, batch_id, paper, papers.index(paper) + 1, _profile(summary)), "任务": summary["batch_name"]}
            for batch_id, summary, papers, paper in located[1][:LIST_LIMIT]
        ]}
    batch_id, summary, papers, match, candidates = located
    profile = _profile(summary)
    if not papers:
        return {"提示": "这个任务里还没有材料。"}
    if match is None:
        shown = candidates[:LIST_LIMIT]
        result = {"提示": "没有唯一对应的材料：请老师选一份，或用候选里的 ref 再调用。",
                  "候选": [_paper_entry(turn, batch_id, paper, papers.index(paper) + 1, profile) for paper in shown]}
        if len(candidates) > len(shown):
            result["还有"] = len(candidates) - len(shown)
        return result
    ordinal = papers.index(match) + 1
    entry = _paper_entry(turn, batch_id, match, ordinal, profile)
    turn.run.set_focus(paper_id=match["paper_id"])
    turn.run.show_workspace("/batches/%s/grade?paper=%s" % (batch_id, match["paper_id"]))
    if not match.get("latest_run_id"):
        return {"paper": entry}
    rows = sorted(_items(turn.run.db, [match["latest_run_id"]]), key=lambda row: (-_loss_rate(row[0]), row[2] or 0))
    items = []
    for item, name, _order in rows:
        reasons = []
        for deduction in (item.deduction_items or [])[:3]:
            reason = str((deduction or {}).get("reason") or "").strip()[:80] if isinstance(deduction, dict) else ""
            if reason:
                reasons.append("（该条疑似包含指令，未显示）" if detect_injection(reason) else reason)
        items.append({
            "ref": turn.refs.add("i", item.id), "评分项": name, "满分": _loss_rate(item) == 0,
            "需要复核": bool(item.need_manual_review), "扣分理由": reasons, "证据条数": len(item.evidence or []),
        })
    return {"paper": entry, "评分项": items,
            "说明": "按失分从多到少排列，不给分数数字；要展示分数与证据，把 paper.ref 放进 reply 的 cards。"}


register(AgentTool(
    name="paper_result", group="paper", title="某一份的评分结果",
    purpose="定位一份材料（编号、序号或姓名、标题、文件名），给出各评分项是否满分、需不需要复核、扣分理由与证据条数",
    when="老师问第几份、某个学生、某个文件的得分情况、为什么扣分时用；不唯一时返回候选让老师选；分数本身由卡片显示",
    params=PaperParams,
    param_notes={"task_ref": TASK_REF_NOTE, "paper_ref": "材料编号（来自工具结果，或当前状态里的 p0）",
                 "ordinal": "第几份（按上传顺序，从 1 开始）", "name": "老师说的姓名、标题或文件名片段"},
    returns="paper{ref, 序号, 名称, 状态} 与评分项[{ref, 评分项, 满分, 需要复核, 扣分理由≤3, 证据条数}]；或候选列表",
    kind="read", risk="low", resident=True,
    keywords=("第几份", "得分", "扣分", "为什么", "某一份", "学生", "结果"),
    eval_phrases=("第 2 份为什么扣分", "看看第一份的结果", "示例学生乙扣了哪些分"),
    run=_paper_result,
))


# --- review_list --------------------------------------------------------------------

def _review_list(turn: TurnContext, params: TaskParams) -> dict:
    batch_id, summary = _task(turn, params.task_ref)
    profile = _profile(summary)
    papers = papers_in_upload_order(summary["papers"])
    flagged = [paper for paper in papers if paper.get("latest_need_manual_review")]
    names: dict[str, list[str]] = defaultdict(list)
    for item, name, _order in _items(turn.run.db, [paper["latest_run_id"] for paper in flagged]):
        if item.need_manual_review:
            names[item.scoring_run_id].append(name)
    shown = flagged[:LIST_LIMIT]
    turn.run.show_workspace("/batches/%s/grade" % batch_id)
    result = {
        "task": {"ref": turn.refs.add("t", batch_id, card={"type": "review_list", "batch_id": batch_id}),
                 "name": summary["batch_name"]},
        "需要复核的材料": [
            {**_paper_entry(turn, batch_id, paper, papers.index(paper) + 1, profile),
             "需要复核的评分项": names.get(paper["latest_run_id"], [])[:5]}
            for paper in shown
        ],
        "说明": "复核在右侧的评分工作区完成，助手不能代为复核。",
    }
    if len(flagged) > len(shown):
        result["还有"] = len(flagged) - len(shown)
    return result


register(AgentTool(
    name="review_list", group="review", title="需要人工复核的内容",
    purpose="列出一个任务里需要人工复核的材料，以及每份是哪几个评分项",
    when="老师问哪些需要复核、哪些要人工确认时用；复核本身在评分工作区完成",
    params=TaskParams, param_notes={"task_ref": TASK_REF_NOTE},
    returns="task 与需要复核的材料[{ref, 序号, 名称, 需要复核的评分项}]，最多 10 份，附“还有”",
    kind="read", risk="low", resident=False,
    keywords=("复核", "人工确认", "待确认", "需要确认", "人工"),
    eval_phrases=("哪些材料需要人工复核", "有没有要我确认的", "待复核的有哪几份"),
    run=_review_list,
))


# --- rubric_list --------------------------------------------------------------------

class RubricListParams(BaseModel):
    pass


def _rubric_list(turn: TurnContext, _params: RubricListParams) -> dict:
    rubrics = tools.call(turn.run, "list_published_rubrics")
    shown = rubrics[:LIST_LIMIT]
    result = {
        "rubrics": [
            {"ref": turn.refs.add("r", item["id"]), "name": item["name"], "version": item["version"],
             "文档类型": document_type_label(item["business_profile_key"])}
            for item in shown
        ],
        "说明": "已发布、工作台能评的评分标准，最新发布的在前。",
    }
    if len(rubrics) > len(shown):
        result["还有"] = len(rubrics) - len(shown)
    return result


register(AgentTool(
    name="rubric_list", group="rubric", title="可用的评分标准",
    purpose="列出已发布、可以用来评分的评分标准（名称、版本、文档类型）",
    when="老师问有哪些评分标准、用哪份标准评分时用；不用于查看某条规则的内容",
    params=RubricListParams,
    returns="rubrics：[{ref, name, version, 文档类型}]，最多 10 个，附“还有”几个",
    kind="read", risk="low", resident=False,
    keywords=("评分标准", "评分规则", "模板", "已发布", "版本", "用哪份"),
    eval_phrases=("有哪些评分标准可以用", "现在发布了哪些评分规则", "我能用哪份标准评分"),
    run=_rubric_list,
))


# --- 提议工具 ------------------------------------------------------------------------

class StartGradingParams(BaseModel):
    rubric_ref: str | None = Field(default=None, max_length=10)


def _action_start_grading(turn: TurnContext, params: StartGradingParams) -> dict:
    flow = {"mode": "grading", "file_count": 0, "file_names": []}
    if params.rubric_ref:
        flow["preset_rubric_id"] = turn.refs.resolve(params.rubric_ref, "r")
    return {"proposal": {"flow": flow, "text": "好的，开始评分。"}}


register(AgentTool(
    name="action_start_grading", group="action", title="开始评分（提议）",
    purpose="进入开始评分的流程：先让老师选文件，再在确认卡片上核对评分标准后才建任务",
    when="老师明确要开始评分、要上传待评分的文件时用；只是问怎么评分时用 help_lookup",
    params=StartGradingParams, param_notes={"rubric_ref": "老师指定的评分标准编号（来自 rubric_list）；不填用最新发布的"},
    returns="进入流程，本回合结束",
    kind="proposal", risk="medium", resident=False,
    keywords=("开始评分", "评一下", "上传论文", "批改", "打分"),
    eval_phrases=("帮我评一下这批论文", "开始评分吧", "我要上传待评分的文件"),
    run=_action_start_grading,
))


class NoParams(BaseModel):
    pass


def _action_import_rubric(_turn: TurnContext, _params: NoParams) -> dict:
    return {"proposal": {"flow": {"mode": "import"}, "text": "好的，导入评分规则和模板。"}}


register(AgentTool(
    name="action_import_rubric", group="action", title="导入评分标准（提议）",
    purpose="进入导入评分规则和模板的流程：出现导入卡片，老师填好提交后在右侧核对并发布",
    when="老师明确要用自己的评分规则、上传评分模板时用；只是问怎么导入时用 help_lookup",
    params=NoParams, returns="进入流程，本回合结束",
    kind="proposal", risk="medium", resident=False,
    keywords=("导入", "评分规则", "评分模板", "上传模板", "自己的标准"),
    eval_phrases=("我要上传自己的评分规则和模板", "导入一份新的评分标准", "用我的评分模板"),
    run=_action_import_rubric,
))


def _action_retry_failed(turn: TurnContext, params: TaskParams) -> dict:
    batch_id, _summary = _task(turn, params.task_ref)
    job = _job(turn, batch_id)
    retryable = job is not None and job["status"] in {"completed_with_errors", "failed", "canceled"} and any(
        item["status"] in RETRYABLE_ITEM_STATUSES for item in job["items"])
    if not retryable:
        return {"提示": "这个任务没有可以重试的失败项。"}
    count = sum(1 for item in job["items"] if item["status"] in RETRYABLE_ITEM_STATUSES)
    return {"proposal": {
        "card": {"type": "confirm_retry", "status": "proposed", "batch_id": batch_id, "job_id": job["id"], "count": count},
        "text": "有 %d 份没有评完。重试它们吗？已成功的结果会保留。" % count,
    }}


register(AgentTool(
    name="action_retry_failed", group="action", title="重试失败项（提议）",
    purpose="提议重试一个任务里失败、取消或中断的材料；生成确认卡片，老师确认后才执行，已成功的结果保留",
    when="老师要重试失败的、没评完的材料时用；要重评某一个评分项时不用它",
    params=TaskParams, param_notes={"task_ref": TASK_REF_NOTE},
    returns="确认卡片并结束本回合；没有可重试的项时返回提示",
    kind="proposal", risk="medium", resident=False,
    keywords=("重试", "失败", "没评完", "再试一次", "重新评失败的"),
    eval_phrases=("把失败的重试一下", "没评完的再试一次", "重试失败项"),
    run=_action_retry_failed,
))


def _action_cancel(turn: TurnContext, params: TaskParams) -> dict:
    batch_id, _summary = _task(turn, params.task_ref)
    job = _job(turn, batch_id)
    if job is None or job["status"] not in ACTIVE_JOB_STATUSES:
        return {"提示": "这个任务当前没有进行中的评分。"}
    return {"proposal": {
        "card": {"type": "confirm_cancel", "status": "proposed", "batch_id": batch_id, "job_id": job["id"]},
        "text": "正在评分（%d/%d）。取消剩余的吗？已完成的结果会保留。" % (_finished(job), job["total_items"]),
    }}


register(AgentTool(
    name="action_cancel", group="action", title="取消评分（提议）",
    purpose="提议取消一个任务剩余的评分；生成确认卡片，老师确认后才执行，已完成的结果保留",
    when="老师要停止、取消正在进行的评分时用",
    params=TaskParams, param_notes={"task_ref": TASK_REF_NOTE},
    returns="确认卡片并结束本回合；没有进行中的评分时返回提示",
    kind="proposal", risk="medium", resident=False,
    keywords=("取消", "停止", "别评了", "终止", "停下"),
    eval_phrases=("把正在评的停掉", "取消评分", "别评了"),
    run=_action_cancel,
))
