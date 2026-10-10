"""起草扣分细则（``rule_draft``）：一个评分项一个任务，一批一个条目。

拆批、单批调用、合并与校验沿用 ``ai_rule_drafter``（同步接口的同一套代码）；差别只在
于每批是一次独立的执行：429 不原地等、输出不合格的修正作为下一次执行、任一批失败
时已成功的批次保留，只重试失败的。
"""

from __future__ import annotations

from copy import deepcopy
from email.utils import parsedate_to_datetime
from datetime import datetime
from datetime import timezone

from pydantic import ValidationError

from backend.app.core.config import settings
from backend.app.schemas.rubric import RubricCriterionCreate
from backend.app.services.ai_tasks.errors import AITaskItemError
from backend.app.services.ai_tasks.errors import AITaskProblem
from backend.app.services.ai_tasks.handlers import PreparedTask
from backend.app.services.ai_tasks.handlers import TaskHandler
from backend.app.services.ai_tasks.handlers import register_handler
from backend.app.services.llm.errors import ProviderCallError
from backend.app.services.llm.rate_limit import CircuitOpenError
from backend.app.services.rubric_import import parse_state
from backend.app.services.rubric_import.ai_rule_drafter import AI_RULE_DRAFT_PROMPT_VERSION
from backend.app.services.rubric_import.ai_rule_drafter import AIRuleDraftValidationError
from backend.app.services.rubric_import.ai_rule_drafter import _draft_batches
from backend.app.services.rubric_import.ai_rule_drafter import _draft_deduction_rules_once
from backend.app.services.rubric_import.ai_rule_drafter import merge_draft_batches
from backend.app.services.rubric_import.compiler import analyze_rule_input
from backend.app.services.rubrics.draft_graph import read_execution_draft


KIND = "rule_draft"
# 输出不合格、可以带修正提示再生成一次的校验码（与同步起草的修正集合相同）。
REPAIRABLE_CODES = {
    "AI_DRAFT_OUTPUT_INVALID",
    "MUTEX_GROUP_MISSING",
    "AI_DRAFT_SOURCE_MISSING",
    "AI_DRAFT_SOURCE_INVALID",
    "DEDUCTION_POINTS_INVALID",
    "DEDUCTION_POINTS_EXCEED_MAX",
    "SEVERITY_RULES_NOT_MONOTONIC",
}
# 超时、5xx、网络中断：再执行 1 次。
TRANSIENT_PROVIDER_CODES = {
    "request_timeout",
    "provider_unavailable",
    "network_error",
    "capacity_unavailable",
    "unknown",
}


def _cause_chain(exc):
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _retry_after_seconds(value):
    if value is None:
        return None
    text = str(value).strip()
    try:
        return max(0, int(float(text)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((when - datetime.now(timezone.utc)).total_seconds()))


def item_error(exc: AIRuleDraftValidationError) -> AITaskItemError:
    """起草错误 → 条目的处理方式（方案第 6.4 节）。"""

    message = "%s%s" % (exc.message, exc.user_action or "")
    if exc.code in REPAIRABLE_CODES:
        return AITaskItemError(exc.code, message, disposition="repair")
    if exc.code in {"AI_DRAFT_PROVIDER_REJECTED", "AI_DRAFT_CONNECTION_MISSING", "AI_DRAFT_OUTPUT_TRUNCATED"}:
        return AITaskItemError(exc.code, message, disposition="fail")
    if exc.code == "AI_DRAFT_TIME_BUDGET_EXCEEDED":
        return AITaskItemError(exc.code, message, disposition="retry")
    if exc.code == "AI_DRAFT_PROVIDER_ERROR":
        for cause in _cause_chain(exc):
            if isinstance(cause, CircuitOpenError):
                return AITaskItemError(
                    exc.code,
                    message,
                    disposition="defer",
                    retry_after_seconds=settings.PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
                )
            if isinstance(cause, ProviderCallError):
                code = cause.error.code
                if code == "rate_limited":
                    return AITaskItemError(
                        exc.code,
                        message,
                        disposition="defer",
                        retry_after_seconds=_retry_after_seconds(cause.error.retry_after),
                    )
                if code == "quota_exhausted":
                    # 等待不会恢复：不调低、不重试。
                    return AITaskItemError(exc.code, message, disposition="fail")
                if code in TRANSIENT_PROVIDER_CODES:
                    return AITaskItemError(exc.code, message, disposition="retry")
                return AITaskItemError(exc.code, message, disposition="fail")
        # 成功状态里的错误响应、未分类的服务异常：按超时/5xx 处理，再试一次。
        return AITaskItemError(exc.code, message, disposition="retry")
    return AITaskItemError(exc.code, message, disposition="fail")


def _prepare(session, rubric_id, params, _principal):
    raw = params.get("criterion")
    if not isinstance(raw, dict):
        raise AITaskProblem(422, "AI_TASK_PARAMS_INVALID", "缺少要起草的评分项。", "请刷新页面后重试。")
    try:
        criterion = RubricCriterionCreate.model_validate(raw)
    except ValidationError as exc:
        raise AITaskProblem(
            422, "AI_TASK_PARAMS_INVALID", "评分项内容不完整。", "请检查评分项后重试。",
            context={"errors": exc.errors(include_url=False)[:5]},
        ) from exc
    criterion_value = criterion.model_dump(mode="json")
    analysis = analyze_rule_input(
        criterion_value.get("deduction_rules") or [], criterion_code=criterion.code
    )
    # 人工归入该评分项的原文单元是规则材料（与同步接口相同）。
    assigned = parse_state.assigned_rule_sources(session, rubric_id, criterion.code)
    if assigned:
        if analysis["input_state"] == "absent" and criterion_value.get("description"):
            analysis["unresolved_segments"].append(
                {"text": criterion_value["description"], "source_refs": ["/criterion/description"]}
            )
        analysis["unresolved_segments"].extend(assigned)
        analysis["source_refs"].extend(ref for item in assigned for ref in item["source_refs"])
        analysis["needs_ai_draft"] = True
    version = (
        (read_execution_draft(session=session, rubric_id=rubric_id).get("active_compilation") or {})
        .get("version")
        or {}
    )
    business_profile_key = version.get("business_profile_key") or "thesis"
    scope = {"criterion_code": criterion.code}
    snapshot = {
        "criterion": criterion_value,
        "input_analysis": analysis,
        "business_profile_key": business_profile_key,
    }
    if not (analysis["needs_ai_draft"] or analysis["needs_severity_expansion"]):
        return PreparedTask(
            scope=scope,
            input_snapshot=snapshot,
            items=[],
            result={
                "criterion_code": criterion.code,
                "input_analysis": analysis,
                "status": "already_structured",
                "draft": None,
            },
        )
    batches = _draft_batches(criterion_value, deepcopy(analysis))
    return PreparedTask(
        scope=scope,
        input_snapshot=snapshot,
        items=[{"analysis": batch} for batch in batches],
    )


def _run_item(view, item_input, scorer):
    snapshot = view.input_snapshot
    try:
        return _draft_deduction_rules_once(
            criterion=snapshot["criterion"],
            input_analysis=item_input["analysis"],
            scorer=scorer,
            business_profile_key=snapshot.get("business_profile_key") or "thesis",
            repair_code=item_input.get("repair_code"),
            deadline=None,
            # 不在执行里按 Retry-After 原地等：条目带着最早可执行时间回到待处理。
            rate_limit_retries=0,
        )
    except AIRuleDraftValidationError as exc:
        raise item_error(exc) from exc


def _merge(view, outputs):
    snapshot = view.input_snapshot
    try:
        draft = merge_draft_batches(snapshot["criterion"], snapshot["input_analysis"], outputs)
    except AIRuleDraftValidationError as exc:
        raise AITaskItemError(exc.code, "%s%s" % (exc.message, exc.user_action or "")) from exc
    return {
        "criterion_code": snapshot["criterion"]["code"],
        "input_analysis": snapshot["input_analysis"],
        "status": "pending_confirmation",
        "draft": draft,
    }


RULE_DRAFT_HANDLER = register_handler(
    TaskHandler(
        kind=KIND,
        prompt_version=AI_RULE_DRAFT_PROMPT_VERSION,
        prepare=_prepare,
        run_item=_run_item,
        merge=_merge,
        label="AI 起草",
        extra={
            "requires_real_model": True,
            "missing_model_problem": (
                "AI_DRAFT_CONNECTION_MISSING",
                "当前没有可用于起草扣分细则的真实 AI 连接。",
                "请配置真实 AI 连接，或将评分项改为仅人工复核。",
            ),
        },
    )
)


__all__ = ["KIND", "RULE_DRAFT_HANDLER", "item_error"]
