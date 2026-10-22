"""模型可以调用的业务工具（Agent 化方案 §11.2）。

每个工具对应老师的一个问题，经一期的工具层（`services/assistant/tools.py`）走共享守卫与服务层，
与页面接口同一权限边界。返回值只给名称、序号与引用编号，统计、排序由代码完成；列表默认 10 条，
附“还有 N 条”。

A0 先登记两个只读工具，用来跑通常驻与延迟两条路径；A1 补齐查询类与提议类工具（§31）。
"""

from __future__ import annotations

from pydantic import BaseModel
from pydantic import Field

from backend.app.services.assistant import tools
from backend.app.services.assistant.agent.catalog import AgentTool
from backend.app.services.assistant.agent.catalog import register
from backend.app.services.assistant.agent.refs import TurnContext
from backend.app.services.assistant.views import document_type_label

LIST_LIMIT = 10


class TaskListParams(BaseModel):
    limit: int = Field(default=8, ge=1, le=20)


def _task_list(turn: TurnContext, params: TaskListParams) -> dict:
    batches = tools.call(turn.run, "list_recent_batches", limit=params.limit)
    return {
        "tasks": [{"ref": turn.refs.add("t", batch["id"]), "name": batch["name"]} for batch in batches],
        "说明": "按创建时间倒序；没有任务时为空列表。",
    }


register(AgentTool(
    name="task_list",
    group="task",
    title="评分任务列表",
    purpose="列出当前组织最近的评分任务（名称与编号）",
    when="老师问有哪些任务、要在几个任务里选一个时用；问某个任务的进度或分数时，先用它拿到任务编号",
    params=TaskListParams,
    param_notes={"limit": "最多返回几个任务，默认 8，最多 20"},
    returns="tasks：[{ref, name}]，最多 limit 个",
    kind="read",
    risk="low",
    resident=True,
    keywords=("任务", "评分任务", "批次", "有哪些", "列表", "最近"),
    eval_phrases=("现在有哪些评分任务", "我最近建了哪些任务", "列一下评分批次"),
    run=_task_list,
))


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
    name="rubric_list",
    group="rubric",
    title="可用的评分标准",
    purpose="列出已发布、可以用来评分的评分标准（名称、版本、文档类型）",
    when="老师问有哪些评分标准、用哪份标准评分时用；不用于查看某条规则的内容",
    params=RubricListParams,
    returns="rubrics：[{ref, name, version, 文档类型}]，最多 10 个，附“还有”几个",
    kind="read",
    risk="low",
    resident=False,
    keywords=("评分标准", "评分规则", "模板", "已发布", "版本", "用哪份"),
    eval_phrases=("有哪些评分标准可以用", "现在发布了哪些评分规则", "我能用哪份标准评分"),
    run=_rubric_list,
))
