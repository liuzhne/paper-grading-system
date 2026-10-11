"""评分助手的工具登记（方案 T14、§7.6）。

一期模型不调用任何工具，只输出意图；图按意图选择工具。每个工具登记名称、说明、
参数（Pydantic 模型，可导出 JSON Schema）、是否只读与实现。格式与 function calling、
MCP 的工具描述基本一致，将来升级任一种都不用重写。

写工具只能由图里的确定性节点在用户确认之后调用（`allow_write=True`），永远不开放给
模型。实现一律经共享守卫与共享动作（`api/actions.py`），与页面接口同一权限边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Callable

from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import select

from backend.app.api import actions
from backend.app.api import guards
from backend.app.db.models import GradingBatch
from backend.app.schemas.batch import BatchCreate
from backend.app.schemas.batch_job import BatchScoringJobCreate
from backend.app.services.assistant.context import RunContext
from backend.app.services.assistant.views import MAX_FILES
from backend.app.services.batch_scoring.jobs import get_latest_batch_scoring_job
from backend.app.services.batches import get_batch_summary


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: type[BaseModel]
    read_only: bool
    run: Callable[[RunContext, Any], Any]

    def schema(self) -> dict:
        return self.params.model_json_schema()


class ToolNotAllowed(RuntimeError):
    pass


_REGISTRY: dict[str, Tool] = {}


def register(name: str, description: str, params: type[BaseModel], *, read_only: bool):
    def decorator(fn):
        _REGISTRY[name] = Tool(name, description, params, read_only, fn)
        return fn
    return decorator


def registry() -> dict[str, Tool]:
    return dict(_REGISTRY)


def call(ctx: RunContext, tool_name: str, /, *, allow_write: bool = False, **arguments):
    # 工具名只能按位置传：工具自己的参数里也可能叫 `name`（例如建任务的任务名）。
    tool = _REGISTRY[tool_name]
    if not tool.read_only and not allow_write:
        raise ToolNotAllowed("写工具 %s 只能在用户确认之后由流程节点调用" % tool_name)
    return tool.run(ctx, tool.params.model_validate(arguments))


# --- 只读工具 -----------------------------------------------------------------------

class NoParams(BaseModel):
    pass


class BatchParams(BaseModel):
    batch_id: str = Field(min_length=1, max_length=36)


class JobParams(BaseModel):
    job_id: str = Field(min_length=1, max_length=36)


class RecentBatchesParams(BaseModel):
    limit: int = Field(default=8, ge=1, le=20)


@register("list_published_rubrics", "列出当前用户可见的已发布评分标准，最新发布的在前", NoParams, read_only=True)
def _list_published_rubrics(ctx: RunContext, _params: NoParams):
    rubrics = [r for r in actions.list_visible_rubrics(ctx.db, ctx.principal) if r.status == "published"]
    rubrics.sort(key=lambda r: (r.published_at or r.created_at), reverse=True)
    return [{"id": r.id, "name": r.name, "version": r.version} for r in rubrics]


@register("list_recent_batches", "列出当前组织最近的评分任务", RecentBatchesParams, read_only=True)
def _list_recent_batches(ctx: RunContext, params: RecentBatchesParams):
    query = select(GradingBatch).order_by(GradingBatch.created_at.desc()).limit(params.limit)
    if ctx.principal.organization_id is not None:
        query = query.where(GradingBatch.organization_id == ctx.principal.organization_id)
    return [{"id": b.id, "name": b.name} for b in ctx.db.scalars(query).all()]


@register("batch_summary", "读取某个评分任务的论文与分数汇总", BatchParams, read_only=True)
def _batch_summary(ctx: RunContext, params: BatchParams):
    guards.visible_batch(ctx.db, params.batch_id, ctx.principal)
    summary = get_batch_summary(ctx.db, params.batch_id)
    return {"batch_id": params.batch_id, "batch_name": summary["batch"].name, "papers": summary["papers"]}


@register("latest_job", "读取某个评分任务最近一次的后台评分作业", BatchParams, read_only=True)
def _latest_job(ctx: RunContext, params: BatchParams):
    guards.visible_batch(ctx.db, params.batch_id, ctx.principal)
    job = get_latest_batch_scoring_job(ctx.db, params.batch_id)
    return _job_view(job) if job is not None else None


@register("job", "读取一次后台评分作业的状态与条目", JobParams, read_only=True)
def _job(ctx: RunContext, params: JobParams):
    return _job_view(guards.visible_job(ctx.db, params.job_id, ctx.principal))


def _job_view(job) -> dict:
    return {
        "id": job.id,
        "batch_id": job.grading_batch_id,
        "status": job.status,
        "total_items": job.total_items,
        "succeeded_count": job.succeeded_count,
        "skipped_count": job.skipped_count,
        "failed_count": job.failed_count,
        "canceled_count": job.canceled_count,
        "items": [
            {"paper_id": item.paper_id, "status": item.status, "error_code": item.error_code,
             "error_message": item.error_message}
            for item in job.items
        ],
    }


# --- 写工具（只能在用户确认之后由流程节点调用） ----------------------------------------------

class CreateBatchParams(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    rubric_id: str = Field(min_length=1, max_length=36)
    ai_connection_id: str | None = Field(default=None, max_length=36)


class PrecheckParams(BaseModel):
    batch_id: str = Field(min_length=1, max_length=36)
    paper_ids: list[str] = Field(min_length=1, max_length=MAX_FILES)


class PaperParams(BaseModel):
    paper_id: str = Field(min_length=1, max_length=36)


@register("create_batch", "新建评分任务（与“新建评分任务”页同一动作）", CreateBatchParams, read_only=False)
def _create_batch(ctx: RunContext, params: CreateBatchParams):
    batch = actions.create_batch(
        ctx.db, ctx.principal, ctx.user_id,
        BatchCreate(name=params.name, rubric_id=params.rubric_id, ai_connection_id=params.ai_connection_id),
    )
    return {"id": batch.id, "name": batch.name}


@register("run_precheck", "对已上传的论文做解析预检", PrecheckParams, read_only=False)
def _run_precheck(ctx: RunContext, params: PrecheckParams):
    return actions.upload_precheck(ctx.db, ctx.principal, params.batch_id, params.paper_ids)


@register("remove_paper", "移除解析失败且从未评分的材料", PaperParams, read_only=False)
def _remove_paper(ctx: RunContext, params: PaperParams):
    actions.delete_paper(ctx.db, ctx.principal, params.paper_id)
    return {"removed": params.paper_id}


@register("start_scoring", "开始后台评分（进行中的作业按幂等返回）", BatchParams, read_only=False)
def _start_scoring(ctx: RunContext, params: BatchParams):
    job, _created = actions.start_scoring_job(
        ctx.db, ctx.principal, ctx.user_id, params.batch_id, BatchScoringJobCreate(),
    )
    actions.dispatch_job_from_worker_thread(job)
    return _job_view(job)


@register("retry_scoring", "重试失败、取消或中断的论文；已成功的结果复用", JobParams, read_only=False)
def _retry_scoring(ctx: RunContext, params: JobParams):
    job = actions.retry_scoring_job(ctx.db, ctx.principal, params.job_id)
    actions.dispatch_job_from_worker_thread(job)
    return _job_view(job)


@register("cancel_scoring", "取消剩余的评分；已完成的结果保留", JobParams, read_only=False)
def _cancel_scoring(ctx: RunContext, params: JobParams):
    return _job_view(actions.cancel_scoring_job(ctx.db, ctx.principal, params.job_id))
