"""AI 归类（``unit_classification``）：每 3 个未认领单元一个条目。

条目成功时用 ``parse_state.merge_unit_classification``（锁行 + 指纹校验 + 合并）写进
当前草稿的归类建议，页面可以边跑边看到；任务完成只是汇总计数。并发由领取时的来源
名额控制，前端不再自己调度批次。
"""

from __future__ import annotations

from backend.app.core.config import settings
from backend.app.db.models import AITask
from backend.app.services.ai_tasks.errors import AITaskItemError
from backend.app.services.ai_tasks.errors import AITaskProblem
from backend.app.services.ai_tasks.errors import AITaskReuse
from backend.app.services.ai_tasks.handlers import PreparedTask
from backend.app.services.ai_tasks.handlers import TaskHandler
from backend.app.services.ai_tasks.handlers import register_handler
from backend.app.services.ai_tasks.state import ACTIVE_TASK_STATUSES
from backend.app.services.rubric_import import parse_state
from backend.app.services.rubric_import.classification.llm_classifier import CLASSIFIER_PROMPT_VERSION
from backend.app.services.rubric_import.classification.llm_classifier import DEFAULT_BATCH_SIZE
from backend.app.services.rubric_import.classification.llm_classifier import ClassificationError
from backend.app.services.rubric_import.classification.llm_classifier import classification_fingerprint
from backend.app.services.rubric_import.classification.llm_classifier import classify_units


KIND = "unit_classification"
# 一次最多处理的单元数（与停用的同步接口相同）。
MAX_UNITS = 500
# 整批失败的原因 → 处理方式（方案第 6.4 节）。
_DEFER = {"rate_limited", "circuit_open"}
_RETRY = {"request_timeout", "provider_unavailable", "network_error", "capacity_unavailable", "unknown"}
_REPAIR = {"invalid_json", "invalid_envelope", "incomplete_output", "empty_content", "error_envelope", "invalid_output"}
_MESSAGES = {
    "rate_limited": "模型调用受到限流，稍后自动继续。",
    "circuit_open": "模型服务连续失败，已暂停调用，稍后自动继续。",
    "request_timeout": "模型响应超时。",
    "quota_exhausted": "模型额度已用完（余额不足或配额耗尽），等待不会恢复，请充值或更换连接。",
    "authentication_failed": "API Key 验证失败，请在账户与连接中检查密钥。",
    "permission_denied": "当前连接没有模型访问权限。",
    "model_or_endpoint_not_found": "模型或接口地址不存在，请检查连接配置。",
    "output_truncated": "模型输出达到长度上限，请检查连接的输出 token 配置。",
    "refused": "模型拒绝回答这批内容（安全策略），可调整后重试。",
    "invalid_json": "模型未返回有效 JSON。",
    "context_length_exceeded": "输入超出模型上下文限制。",
}


def _retry_after(value):
    if value is None:
        return None
    try:
        return max(0, int(float(str(value).strip())))
    except ValueError:
        return None


def _item_error(failure):
    code = (failure or {}).get("code") or "unknown"
    message = _MESSAGES.get(code, "模型调用失败（%s），请测试当前连接后重试失败的批次。" % code)
    if code in _DEFER:
        retry_after = (
            settings.PROVIDER_CIRCUIT_COOLDOWN_SECONDS
            if code == "circuit_open"
            else _retry_after((failure or {}).get("retry_after"))
        )
        return AITaskItemError(code, message, disposition="defer", retry_after_seconds=retry_after)
    if code in _RETRY:
        return AITaskItemError(code, message, disposition="retry")
    if code in _REPAIR:
        return AITaskItemError(code, message, disposition="repair")
    return AITaskItemError(code, message, disposition="fail")


def _busy_units(session, rubric_id):
    """进行中的归类任务各自负责的单元，按任务创建顺序。"""

    tasks = session.query(AITask).filter(
        AITask.rubric_id == rubric_id,
        AITask.kind == KIND,
        AITask.status.in_(ACTIVE_TASK_STATUSES),
    ).order_by(AITask.created_at).all()
    return [(task.id, set((task.scope or {}).get("unit_ids") or [])) for task in tasks]


def _prepare(session, rubric_id, params, _principal):
    unit_ids = params.get("unit_ids")
    if unit_ids is not None and (
        not isinstance(unit_ids, list)
        or len(unit_ids) > MAX_UNITS
        or not all(isinstance(value, str) for value in unit_ids)
    ):
        raise AITaskProblem(422, "AI_TASK_PARAMS_INVALID", "要归类的单元列表无效。", "请刷新页面后重试。")
    rejudge = bool(params.get("rejudge"))
    try:
        compilation, units, criteria = parse_state.classification_inputs(
            session, rubric_id, unit_ids=unit_ids
        )
    except parse_state.ParseStateError as exc:
        raise AITaskProblem(exc.status, exc.code, exc.message, "请刷新后重试。") from exc
    if not units:
        raise AITaskProblem(422, "NOTHING_TO_CLASSIFY", "没有需要分类的未认领内容。", "请刷新后重试。")
    skipped = []
    if not rejudge:
        # 续跑：已有有效建议的单元不再重复付费（显式勾选重新判断时 rejudge=true）。
        suggested = parse_state.suggested_unit_ids(session, rubric_id)
        skipped = [unit["unit_id"] for unit in units if unit["unit_id"] in suggested]
        units = [unit for unit in units if unit["unit_id"] not in suggested]
    # 按单元去重：已在其它进行中归类任务里的单元剔除；全部被占用时返回那些任务。
    busy = _busy_units(session, rubric_id)
    taken = set().union(*(ids for _task_id, ids in busy)) if busy else set()
    remaining = [unit for unit in units if unit["unit_id"] not in taken]
    if units and not remaining:
        overlapping = next(task_id for task_id, ids in busy if ids & {unit["unit_id"] for unit in units})
        raise AITaskReuse(overlapping)
    units = remaining
    scope = {"unit_ids": sorted(unit["unit_id"] for unit in units)}
    snapshot = {"criteria": criteria, "compilation_id": compilation.id}
    if not units:
        return PreparedTask(
            scope=scope,
            input_snapshot=snapshot,
            items=[],
            result={"unit_count": 0, "classified_count": 0, "skipped_unit_ids": skipped},
        )
    items = [
        {"units": units[start:start + DEFAULT_BATCH_SIZE]}
        for start in range(0, len(units), DEFAULT_BATCH_SIZE)
    ]
    return PreparedTask(
        scope=scope,
        input_snapshot=snapshot,
        items=items,
        fingerprint_material={"units": classification_fingerprint(units, criteria)},
    )


def _run_item(view, item_input, scorer):
    try:
        result = classify_units(
            item_input["units"],
            view.input_snapshot["criteria"],
            scorer,
            rate_limit_retries=0,
            max_attempts=1,
            repair=bool(item_input.get("repair_code")),
        )
    except ClassificationError as exc:
        raise AITaskItemError(exc.code, exc.message, disposition="fail") from exc
    if result["failed_unit_ids"]:
        raise _item_error(result.get("failure"))
    return result


def _on_item_success(session, view, item_input, output):
    try:
        parse_state.merge_unit_classification(
            session,
            view.rubric_id,
            view.input_snapshot["compilation_id"],
            item_input["units"],
            output,
            actor_id=view.owner_id,
        )
    except parse_state.ParseStateError as exc:
        raise AITaskItemError(exc.code, exc.message, disposition="fail") from exc


def _merge(view, outputs):
    return {
        "unit_count": len(view.scope.get("unit_ids") or []),
        "classified_count": sum(len(output.get("results") or []) for output in outputs),
        "rejected_count": sum(len(output.get("rejected") or []) for output in outputs),
        "unclassified_count": sum(len(output.get("unclassified_unit_ids") or []) for output in outputs),
    }


UNIT_CLASSIFICATION_HANDLER = register_handler(
    TaskHandler(
        kind=KIND,
        prompt_version=CLASSIFIER_PROMPT_VERSION,
        prepare=_prepare,
        run_item=_run_item,
        merge=_merge,
        on_item_success=_on_item_success,
        label="AI 归类",
        extra={
            "requires_real_model": True,
            "missing_model_problem": (
                "AI_CONNECTION_MISSING",
                "当前没有可用于分类的真实 AI 连接。",
                "请配置真实 AI 连接后重试。",
            ),
        },
    )
)


__all__ = ["KIND", "UNIT_CLASSIFICATION_HANDLER"]
