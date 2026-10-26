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
from backend.app.services.scoring.suggestions import INJECTION_PLACEHOLDER
from backend.app.services.scoring.suggestions import revision_suggestion
from backend.app.services.scoring.suggestions import without_scores

LIST_LIMIT = 10
ACTIVE_JOB_STATUSES = {"queued", "running", "cancel_requested"}
RETRYABLE_ITEM_STATUSES = {"failed", "canceled", "running"}
TASK_REF_NOTE = "任务编号（来自 task_list 等工具的结果，或当前状态里的 t0）；不填就用当前任务，没有当前任务时用最近的一个"
#: 提议工具不替老师挑任务：重试、取消错了任务的代价比查询大（A1 第二轮评测：不填时用了最近的任务，答“没有可以重试的”）。
ACTION_TASK_REF_NOTE = ("任务编号（来自 task_list 等工具的结果，或当前状态里的 t0）；不填就用 task_name 或当前任务；"
                        "都没有时按状态在最近的任务里找唯一的那个，不唯一给候选")
ACTION_TASK_NAME_NOTE = "老师说的任务名（或其中一段，例如“课程报告评分”）；不知道编号时填它，会在最近的任务里按名称找"
REVIEW_TASK_REF_NOTE = ("任务编号（来自 task_list 等工具的结果，或当前状态里的 t0）；不填就用当前任务；"
                        "没有当前任务时在最近的任务里一起找")


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
    for batch in batches:
        turn.refs.name("t", batch["id"], batch["name"])
    return [{"ref": turn.refs.add("t", batch["id"], card=_overview_card(batch["id"])), "name": batch["name"]}
            for batch in batches]


def _task(turn: TurnContext, task_ref: str | None) -> tuple[str, dict]:
    batch_id = _task_id(turn, task_ref)
    summary = tools.call(turn.run, "batch_summary", batch_id=batch_id)
    turn.refs.name("t", batch_id, summary["batch_name"])
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


NAME_LIMIT = 40


def _safe_name(paper: dict, profile: str) -> str:
    """材料名称来自上传的文档（学生姓名、标题、文件名），学生能控制：过注入检测并截断，与扣分理由同等对待。"""

    name = paper_label(paper, profile)
    if detect_injection(name):
        return "（名称疑似包含指令，未显示）"
    return name[:NAME_LIMIT]


def _paper_entry(turn: TurnContext, batch_id: str, paper: dict, ordinal: int, profile: str) -> dict:
    card = _paper_card(batch_id, paper) if paper.get("latest_run_id") else None
    name = _safe_name(paper, profile)
    turn.refs.name("p", paper["paper_id"], name, parent=("t", batch_id))
    return {"ref": turn.refs.add("p", paper["paper_id"], card=card), "序号": ordinal,
            "名称": name, "状态": _paper_status(paper)}


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


#: 评分理由、修改建议给模型的长度上限（§31.3）。
REASON_LIMIT = 80
ADVICE_LIMIT = 120


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _safe_text(text, limit: int) -> str:
    """评分侧写的文字：源自学生文档，过注入检测；去掉分数句；截断。"""

    text = str(text or "").strip()
    if detect_injection(text):
        return INJECTION_PLACEHOLDER
    return _clip(without_scores(text), limit)


#: 没有评出有效结果的评分项（与评分页“不能直接改分”的判断一致）：不是“失了分”，不能据此说怎么改。
UNRESOLVED_STATUSES = {"invalid", "blocked"}


def _unresolved(item: ScoreItem) -> bool:
    return item.auto_score_status in UNRESOLVED_STATUSES or (item.final_score is None and item.ai_score is None)


def _improvement(item: ScoreItem) -> dict:
    """失了分的评分项：评分理由，与评分模型写过的修改建议（系统说明、分数句、注入已由共用判断处理，§31.3）。"""

    found = {}
    reason = _safe_text(item.reason, REASON_LIMIT)
    if reason:
        found["评分理由"] = reason
    advice = revision_suggestion(item.suggestion, item.auto_score_status)
    if advice:
        found["修改建议"] = _clip(advice, ADVICE_LIMIT)
    return found


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
    when="老师问系统“怎么用”“在哪……”“是什么”“有什么区别”“……怎么办”时先查；不用于查询具体任务或材料的数据。"
         "老师表达要做什么（如“帮我评一下”“我要用自己的模板”“失败的重试”“别评了”）时不要调本工具，直接调对应的 action_*；"
         "问某一份论文怎么改（修改建议）用 paper_result",
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
    """任务名与数量：“现在评了几篇”一步就能答，跨任务也能合计（§31.3）。数量由代码统计，不含分数。"""

    batches = tools.call(turn.run, "list_recent_batches", limit=params.limit)
    tasks, total = [], defaultdict(int)
    for batch in batches:
        turn.refs.name("t", batch["id"], batch["name"])
        papers = tools.call(turn.run, "batch_summary", batch_id=batch["id"])["papers"]
        job = _job(turn, batch["id"])
        if job is not None:
            turn.refs.saw_status(job["status"], "t", batch["id"])
        counts = {"材料": len(papers), "已评分": sum(1 for paper in papers if paper.get("latest_run_id")),
                  "需要复核": sum(1 for paper in papers if paper.get("latest_need_manual_review"))}
        for key, value in counts.items():
            total[key] += value
        tasks.append({"ref": turn.refs.add("t", batch["id"], card=_overview_card(batch["id"])), "name": batch["name"],
                      **counts, "评分状态": job["status"] if job is not None else "还没有开始评分"})
    return {
        "tasks": tasks,
        "合计": {"任务": len(tasks), **total} if tasks else {"任务": 0},
        "说明": "按创建时间倒序；没有任务时为空列表。只有一个任务时直接用它，不必问老师是不是这个；"
                "要看失分最多的评分项、最差的几份，用 task_overview。",
    }


register(AgentTool(
    name="task_list", group="task", title="评分任务列表",
    purpose="列出当前组织最近的评分任务：名称、编号，以及每个任务的材料数、已评分、需要复核、评分状态与合计",
    when="老师问有哪些任务、一共评了几篇、有几份要复核、要在几个任务里选一个时用；问某个任务的细节（失分最多的项、"
         "最差的几份）用 task_overview",
    params=TaskListParams, param_notes={"limit": "最多返回几个任务，默认 8，最多 20"},
    returns="tasks：[{ref, name, 材料, 已评分, 需要复核, 评分状态}]，最多 limit 个；合计",
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
        turn.refs.saw_status(job["status"], "t", batch_id)
    rates: dict[str, list[float]] = defaultdict(list)
    order: dict[str, int] = {}
    for item, name, display_order in _items(turn.run.db, [paper["latest_run_id"] for paper in scored]):
        if _unresolved(item):
            continue  # 没评出结果不算失分，不进“失分从多到少”的排名（#60 复审）
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
    # 只有一个任务时不必提醒“不对的话先找任务”：没有别的可选，提醒只会让模型反问（§31.3）。
    if chose_latest and len(tools.call(turn.run, "list_recent_batches", limit=2)) > 1:
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
        return "not_found", params.name
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
    if located[0] == "not_found":
        return {"提示": "最近 %d 个任务里没有找到“%s”：请老师确认姓名、标题或文件名，或先指定任务。"
                      % (SEARCH_TASKS, str(located[1])[:NAME_LIMIT]), "tasks": _task_choices(turn)}
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
    turn.refs.saw_status(entry["状态"], "p", match["paper_id"])  # 这一份的状态，不代表整个任务
    turn.run.set_focus(paper_id=match["paper_id"])
    turn.run.show_workspace("/batches/%s/grade?paper=%s" % (batch_id, match["paper_id"]))
    if not match.get("latest_run_id"):
        return {"paper": entry}
    # 没评出结果的项排在最后：它们不是“失分最多”的。
    rows = sorted(_items(turn.run.db, [match["latest_run_id"]]),
                  key=lambda row: (_unresolved(row[0]), -_loss_rate(row[0]), row[2] or 0))
    items = []
    for item, name, _order in rows:
        reasons = []
        for deduction in (item.deduction_items or [])[:3]:
            reason = str((deduction or {}).get("reason") or "").strip()[:80] if isinstance(deduction, dict) else ""
            if reason:
                reasons.append("（该条疑似包含指令，未显示）" if detect_injection(reason) else reason)
        if _unresolved(item):
            # 没评出结果：不是失了分（#60 复审），不给评分理由与修改建议，说明状态，让老师去重试。
            items.append({
                "ref": turn.refs.add("i", item.id), "评分项": name, "满分": False,
                "评分状态": "没有评出有效结果，不能据此说怎么改；可到评分进度页重试失败项",
                "需要复核": bool(item.need_manual_review), "扣分理由": [], "证据条数": len(item.evidence or []),
            })
            continue
        lost = _loss_rate(item) > 0
        items.append({
            "ref": turn.refs.add("i", item.id), "评分项": name, "满分": not lost,
            "需要复核": bool(item.need_manual_review), "扣分理由": reasons, "证据条数": len(item.evidence or []),
            **(_improvement(item) if lost else {}),
        })
    return {"paper": entry, "评分项": items,
            "说明": "按失分从多到少排列，不给分数数字；要展示分数与证据，把 paper.ref 放进 reply 的 cards。"
                    "老师问怎么改时，按评分项把扣分理由、评分理由（有修改建议就一并用）转成怎么改，不编造理由里没有的内容；"
                    "带“评分状态”的项没有评出结果，不给修改建议。"}


register(AgentTool(
    name="paper_result", group="paper", title="某一份的评分结果",
    purpose="定位一份材料（编号、序号或姓名、标题、文件名），给出各评分项是否满分、需不需要复核、扣分理由与证据条数；"
            "失分的项另有评分理由与修改建议，用来回答“怎么改”",
    when="老师问第几份、某个学生、某个文件的得分情况、为什么扣分、怎么改（修改建议、怎么改进、怎么提高）时用；"
         "按姓名找人不用先选任务（不填 task_ref 会在最近的任务里一起找）；不唯一时返回候选让老师选；分数本身由卡片显示",
    params=PaperParams,
    param_notes={"task_ref": "任务编号（来自工具结果，或当前状态里的 t0）；按姓名找人时可以不填，会在最近的任务里一起找",
                 "paper_ref": "材料编号（来自工具结果，或当前状态里的 p0）",
                 "ordinal": "第几份（按上传顺序，从 1 开始；要配合任务）", "name": "老师说的姓名、标题或文件名片段"},
    returns="paper{ref, 序号, 名称, 状态} 与评分项[{ref, 评分项, 满分, 需要复核, 扣分理由≤3, 证据条数, 失分项另有评分理由与修改建议}]；或候选列表",
    kind="read", risk="low", resident=True,
    keywords=("第几份", "得分", "扣分", "为什么", "某一份", "学生", "结果"),
    eval_phrases=("第 2 份为什么扣分", "看看第一份的结果", "示例学生乙扣了哪些分"),
    run=_paper_result,
))


# --- review_list --------------------------------------------------------------------

def _review_across_tasks(turn: TurnContext) -> dict | str:
    """没有当前任务：在最近的任务里找需要复核的。只有一个任务有，就返回它的编号（接着列明细）。"""

    found = []
    for batch in tools.call(turn.run, "list_recent_batches", limit=SEARCH_TASKS):
        summary = tools.call(turn.run, "batch_summary", batch_id=batch["id"])
        turn.refs.name("t", batch["id"], summary["batch_name"])
        count = sum(1 for paper in summary["papers"] if paper.get("latest_need_manual_review"))
        if count:
            found.append((batch["id"], summary["batch_name"], count))
    if not found:
        return {"提示": "最近的任务里没有需要人工复核的材料。"}
    if len(found) == 1:
        return turn.refs.add("t", found[0][0], card=_overview_card(found[0][0]))
    return {
        "有需要复核的任务": [
            {"ref": turn.refs.add("t", batch_id, card={"type": "review_list", "batch_id": batch_id}),
             "name": name, "需要复核的材料数": count}
            for batch_id, name, count in found
        ],
        "说明": "老师没有指定任务：告诉他这几个任务各有几份需要复核，请他选一个；明细用 task_ref 再调用。",
    }


def _review_list(turn: TurnContext, params: TaskParams) -> dict:
    if not _has_task(turn, params.task_ref):
        across = _review_across_tasks(turn)
        if isinstance(across, dict):
            return across
        params = TaskParams(task_ref=across)
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
    when="老师问哪些需要复核、哪些要人工确认时用（没说任务也可以，会在最近的任务里一起找）；复核本身在评分工作区完成",
    params=TaskParams, param_notes={"task_ref": REVIEW_TASK_REF_NOTE},
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
        "rubrics": [_rubric_entry(turn, item) for item in shown],
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
    rubric_name: str | None = Field(default=None, max_length=50)


def _rubric_entry(turn: TurnContext, item: dict) -> dict:
    return {"ref": turn.refs.add("r", item["id"]), "name": item["name"], "version": item["version"],
            "文档类型": document_type_label(item["business_profile_key"])}


def _action_start_grading(turn: TurnContext, params: StartGradingParams) -> dict:
    flow = {"mode": "grading", "file_count": 0, "file_names": []}
    if params.rubric_ref:
        flow["preset_rubric_id"] = turn.refs.resolve(params.rubric_ref, "r")
    elif (params.rubric_name or "").strip():
        # 老师点了评分标准的名字（或文档类型，如“通用文档”）：在已发布的里找，一步出卡片，不用先查 rubric_list。
        needle = params.rubric_name.strip()
        rubrics = tools.call(turn.run, "list_published_rubrics")
        hits = [item for item in rubrics
                if needle in item["name"] or needle in document_type_label(item["business_profile_key"])]
        if len(hits) == 1:
            flow["preset_rubric_id"] = hits[0]["id"]
        elif hits:
            return {"提示": "符合“%s”的评分标准不止一份：请老师选一份，或用候选里的 ref 再调用。" % needle[:20],
                    "候选": [_rubric_entry(turn, item) for item in hits[:LIST_LIMIT]]}
        else:
            return {"提示": "已发布的评分标准里没有“%s”：请老师从下面选一份，或不指定（用最新发布的）。" % needle[:20],
                    "rubrics": [_rubric_entry(turn, item) for item in rubrics[:LIST_LIMIT]]}
    return {"proposal": {"flow": flow, "text": "好的，开始评分。"}}


register(AgentTool(
    name="action_start_grading", group="action", title="开始评分（提议）",
    purpose="在界面上弹出【选择待评分文件】卡片；老师选好文件后再弹出确认卡片核对评分标准，点了确认才建任务",
    when="老师要评分、要评一批材料时直接调用（如“帮我评一下”“开始评分”）：即使还没选或上传文件、没说用哪份标准，"
         "也直接调用——调用后界面才会弹出选文件卡片让老师上传；不要用 help_lookup 教老师怎么操作。只是问“怎么评分”时才用 help_lookup",
    params=StartGradingParams,
    param_notes={"rubric_ref": "老师指定的评分标准编号（来自 rubric_list）；不填用最新发布的",
                 "rubric_name": "老师说的评分标准名称或文档类型（如“通用文档”）；不知道编号时填它，会在已发布的标准里找"},
    returns="进入流程，本回合结束",
    kind="proposal", risk="medium", resident=True,
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
    purpose="在界面上弹出【导入评分规则和模板】卡片；老师在卡片里上传、提交后在右侧核对并发布",
    when="老师要用自己的评分规则或模板时直接调用（如“我要用自己的评分规则和模板来评”）：即使还没上传规则或模板文件，"
         "也直接调用——卡片里才能上传；只是问“怎么导入”时才用 help_lookup",
    params=NoParams, returns="进入流程，本回合结束",
    kind="proposal", risk="medium", resident=True,
    keywords=("导入", "评分规则", "评分模板", "上传模板", "自己的标准"),
    eval_phrases=("我要上传自己的评分规则和模板", "导入一份新的评分标准", "用我的评分模板"),
    run=_action_import_rubric,
))


class ActionTaskParams(BaseModel):
    task_ref: str | None = Field(default=None, max_length=10)
    task_name: str | None = Field(default=None, max_length=50)


def _needs_task(turn: TurnContext) -> dict:
    return {"需要先确定任务": "老师没有说是哪个任务，也没有当前任务。老师的话里点了下面某个任务的名字，就带上它的 ref "
                          "（或 task_name）直接再调用，不要再问；真的看不出是哪个，再问老师。",
            "tasks": _task_choices(turn)}


def _retryable(job: dict | None) -> bool:
    return job is not None and job["status"] in {"completed_with_errors", "failed", "canceled"} and any(
        item["status"] in RETRYABLE_ITEM_STATUSES for item in job["items"])


def _cancelable(job: dict | None) -> bool:
    return job is not None and job["status"] in ACTIVE_JOB_STATUSES


def _choices(turn: TurnContext, batches: list[dict]) -> list[dict]:
    for batch in batches:
        turn.refs.name("t", batch["id"], batch["name"])
    return [{"ref": turn.refs.add("t", batch["id"], card=_overview_card(batch["id"])), "name": batch["name"]}
            for batch in batches]


def _task_by_state(turn: TurnContext, wanted, many: str, none: str) -> str | dict:
    """老师没说任务、也没有当前任务：在最近的任务里找唯一处于这个状态的那一个，一步出卡片。

    A1 第三轮评测：模型裸调 action_cancel({})，拿到“先确定任务”就去问老师。卡片上写明任务名，
    老师点了才执行，所以按状态唯一定位不会误执行；不唯一才给候选。
    """

    hits = [batch for batch in tools.call(turn.run, "list_recent_batches", limit=SEARCH_TASKS)
            if wanted(_job(turn, batch["id"]))]
    if len(hits) == 1:
        return _choices(turn, hits)[0]["ref"]
    if hits:
        return {"提示": "%s：请老师选一个，或用候选里的 ref 再调用。" % many, "候选": _choices(turn, hits)}
    return {"提示": none}


def _action_task(turn: TurnContext, params: ActionTaskParams, wanted, many: str, none: str) -> str | dict | None:
    """提议工具的任务：编号 → 名称 → 当前任务 → 按状态唯一定位；返回 dict 时直接作为工具结果（候选或提示）。"""

    if params.task_ref:
        return params.task_ref
    needle = (params.task_name or "").strip()
    if needle:
        # 只认“老师说的名字出现在任务名里”：反过来（任务名出现在老师的话里）会把“实验报告 A”对到“实验报告”上。
        hits = [batch for batch in tools.call(turn.run, "list_recent_batches", limit=SEARCH_TASKS)
                if needle in batch["name"]]
        if len(hits) == 1:
            return _choices(turn, hits)[0]["ref"]
        if hits:
            return {"提示": "名称里有“%s”的任务不止一个：请老师选一个，或用候选里的 ref 再调用。" % needle[:20],
                    "候选": _choices(turn, hits)}
        result = _needs_task(turn)
        result["提示"] = "最近的任务里没有名称里有“%s”的。" % needle[:20]
        return result
    if _has_task(turn, None):
        return None
    return _task_by_state(turn, wanted, many, none)


def _action_retry_failed(turn: TurnContext, params: ActionTaskParams) -> dict:
    task_ref = _action_task(turn, params, _retryable, "有好几个任务都有可以重试的失败项", "最近的任务里没有可以重试的失败项。")
    if isinstance(task_ref, dict):
        return task_ref
    batch_id, summary = _task(turn, task_ref)
    job = _job(turn, batch_id)
    if job is not None:
        turn.refs.saw_status(job["status"], "t", batch_id)
    if not _retryable(job):
        return {"提示": "这个任务没有可以重试的失败项。"}
    count = sum(1 for item in job["items"] if item["status"] in RETRYABLE_ITEM_STATUSES)
    return {"proposal": {
        "card": {"type": "confirm_retry", "status": "proposed", "batch_id": batch_id, "job_id": job["id"], "count": count},
        # 写明任务名：任务可能是按名称或状态找到的，老师要看得出是哪一个。
        "text": "《%s》有 %d 份没有评完。重试它们吗？已成功的结果会保留。" % (summary["batch_name"], count),
    }}


register(AgentTool(
    name="action_retry_failed", group="action", title="重试失败项（提议）",
    purpose="在界面上弹出【重试确认】卡片（写明任务名与份数），老师点了确认才重试失败、取消或中断的材料；已成功的结果保留",
    when="老师要重试失败的、没评完的材料时直接调用；没说任务、也没有当前任务时，会在最近的任务里找唯一有失败项的那个；"
         "要重评某一个评分项时不用它",
    params=ActionTaskParams, param_notes={"task_ref": ACTION_TASK_REF_NOTE, "task_name": ACTION_TASK_NAME_NOTE},
    returns="确认卡片并结束本回合；没有可重试的项时返回提示",
    kind="proposal", risk="medium", resident=True,
    keywords=("重试", "失败", "没评完", "再试一次", "重新评失败的"),
    eval_phrases=("把失败的重试一下", "没评完的再试一次", "重试失败项"),
    run=_action_retry_failed,
))


def _action_cancel(turn: TurnContext, params: ActionTaskParams) -> dict:
    task_ref = _action_task(turn, params, _cancelable, "有好几个任务正在评分", "最近的任务里没有正在进行的评分。")
    if isinstance(task_ref, dict):
        return task_ref
    batch_id, summary = _task(turn, task_ref)
    job = _job(turn, batch_id)
    if job is not None:
        turn.refs.saw_status(job["status"], "t", batch_id)
    if not _cancelable(job):
        return {"提示": "这个任务当前没有进行中的评分。"}
    return {"proposal": {
        "card": {"type": "confirm_cancel", "status": "proposed", "batch_id": batch_id, "job_id": job["id"]},
        "text": "《%s》正在评分（%d/%d）。取消剩余的吗？已完成的结果会保留。" % (
            summary["batch_name"], _finished(job), job["total_items"]),
    }}


register(AgentTool(
    name="action_cancel", group="action", title="取消评分（提议）",
    purpose="在界面上弹出【取消确认】卡片（写明任务名与进度），老师点了确认才取消剩余的评分；已完成的结果保留",
    when="老师要停止、取消正在进行的评分时直接调用（如“先别评了”）；没说任务、也没有当前任务时，会在最近的任务里找唯一正在评分的那个",
    params=ActionTaskParams, param_notes={"task_ref": ACTION_TASK_REF_NOTE, "task_name": ACTION_TASK_NAME_NOTE},
    returns="确认卡片并结束本回合；没有进行中的评分时返回提示",
    kind="proposal", risk="medium", resident=True,
    keywords=("取消", "停止", "别评了", "终止", "停下"),
    eval_phrases=("把正在评的停掉", "取消评分", "别评了"),
    run=_action_cancel,
))
