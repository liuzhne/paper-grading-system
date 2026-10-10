"""规则审查（``rule_review``）：每个评分项一个条目，两个以上评分项时再加一次跨项审查。

前置检查、评分项包、问题校验沿用 ``rubric_import.review``；差别只在于每个评分项是
一次独立的执行：429 不原地等，输出缺 issues 数组时带修正提示再执行一次，仍不合格
就把这一项记为“审查失败”继续（与同步审查相同，其余评分项的结果照常保留）。全部
条目结束后合并、编号，写进当前草稿的 ``raw_model_output.rule_review``。
"""

from __future__ import annotations

from backend.app.services.ai_tasks.errors import AITaskItemError
from backend.app.services.ai_tasks.errors import AITaskProblem
from backend.app.services.ai_tasks.handlers import PreparedTask
from backend.app.services.ai_tasks.handlers import TaskHandler
from backend.app.services.ai_tasks.handlers import register_handler
from backend.app.services.ai_tasks.provider_errors import provider_item_error
from backend.app.services.llm.errors import ProviderJSONOutputError
from backend.app.services.rubric_import import review
from backend.app.services.rubric_import import review_state
from backend.app.services.rubric_import.parse_state import ParseStateError


KIND = "rule_review"
SCOPES = ("priority", "all")
CROSS = "__cross__"
_PROVIDER_HINTS = {
    "authentication_failed": "AI 连接鉴权失败，请检查 API Key 与地域是否匹配。",
    "permission_denied": "AI 连接没有调用权限，请检查模型授权。",
    "quota_exhausted": "AI 连接的额度已用完（余额不足或配额耗尽），请充值或更换连接后重试。",
    "request_timeout": "AI 请求超时，请稍后重试失败的评分项。",
    "model_or_endpoint_not_found": "AI 模型或接口地址不存在，请检查连接配置。",
    "invalid_request": "AI 接口拒绝了请求参数，请检查模型与协议兼容性。",
    "rate_limited": "AI 调用受到限流，稍后自动继续。",
}


def _prepare(session, rubric_id, params, _principal):
    scope = params.get("scope") or "priority"
    if scope not in SCOPES:
        raise AITaskProblem(422, "AI_TASK_PARAMS_INVALID", "审查范围无效。", "请刷新页面后重试。")
    try:
        prepared = review_state.prepare_rule_review(session, rubric_id, scope=scope)
    except ParseStateError as exc:
        raise AITaskProblem(exc.status, exc.code, exc.message, "请刷新后重试。") from exc
    bundles = prepared.pop("bundles")
    items = [
        {"part": "criterion", "label": bundle["criterion"]["code"], "bundle": bundle} for bundle in bundles
    ]
    if len(bundles) >= 2:
        items.append({"part": "cross", "label": CROSS, "summary": review.cross_summary(bundles)})
    return PreparedTask(scope={"scope": scope}, input_snapshot=prepared, items=items)


def _output_error(exc: ProviderJSONOutputError) -> AITaskItemError | None:
    """模型答了但不能用：截断与拒答不会因为再问一次而变好；其它按“缺 issues”处理。"""

    if exc.reason == "output_truncated":
        return AITaskItemError(
            "REVIEW_OUTPUT_TRUNCATED",
            "模型输出达到长度上限，审查结果不完整；请提高连接的输出 Token 上限后重试失败的评分项。",
        )
    if exc.reason == "refused":
        return AITaskItemError("REVIEW_REFUSED", "模型拒绝审查这项内容（安全策略），可调整后重试。")
    if exc.reason == "error_envelope":
        return AITaskItemError(
            "AI_PROVIDER_ERROR", "AI 接口在成功状态中返回错误，请测试当前连接后重试。", disposition="retry"
        )
    return None


def _run_item(view, item_input, scorer):
    repair = bool(item_input.get("repair_code"))
    cross = item_input.get("part") == "cross"
    instructions = review.CROSS_INSTRUCTIONS if cross else review.REVIEW_INSTRUCTIONS
    payload = {"criteria": item_input["summary"]} if cross else item_input["bundle"]
    try:
        issues = review.call_once(scorer, instructions, payload, repair=repair)
    except ProviderJSONOutputError as exc:
        error = _output_error(exc)
        if error is not None:
            raise error from exc
        issues = None
    except Exception as exc:
        raise provider_item_error(
            exc, code="AI_PROVIDER_ERROR", message="AI 服务暂时不可用，请稍后重试失败的评分项。",
            hints=_PROVIDER_HINTS,
        ) from exc
    code = CROSS if cross else item_input["bundle"]["criterion"]["code"]
    if issues is None:
        if not repair:
            raise AITaskItemError(
                "REVIEW_OUTPUT_INVALID", "模型两次输出都缺少 issues 数组。", disposition="repair"
            )
        # 修正后仍不合格：记为这一项审查失败，其余评分项照常（与同步审查相同）。
        return {"code": code, "findings": [], "discarded": [], "failed": True, "model": review.scorer_model(scorer)}
    if cross:
        findings, discarded = review.validate_cross_issues(item_input["summary"], issues)
    else:
        findings, discarded = review.validate_criterion_issues(item_input["bundle"], issues)
    return {"code": code, "findings": findings, "discarded": discarded, "failed": False,
            "model": review.scorer_model(scorer)}


def _merge(view, outputs):
    snapshot = view.input_snapshot
    findings = [finding for output in outputs for finding in output.get("findings") or []]
    return {
        "prompt_version": review.REVIEW_PROMPT_VERSION,
        "model": next((output["model"] for output in outputs if output.get("model")), {}),
        "findings": review.number_findings(findings),
        "discarded": [entry for output in outputs for entry in output.get("discarded") or []],
        "failed": [output["code"] for output in outputs if output.get("failed")],
        "scope": snapshot["scope"],
        "criteria_codes": snapshot["criteria_codes"],
        "prechecks": snapshot["prechecks"],
        "fingerprint": snapshot["fingerprint"],
    }


def _on_task_success(session, view, result):
    try:
        review_state.store_rule_review(session, view.rubric_id, result, actor_id=view.owner_id)
    except ParseStateError as exc:
        raise AITaskItemError(exc.code, exc.message) from exc


RULE_REVIEW_HANDLER = register_handler(
    TaskHandler(
        kind=KIND,
        prompt_version=review.REVIEW_PROMPT_VERSION,
        prepare=_prepare,
        run_item=_run_item,
        merge=_merge,
        on_task_success=_on_task_success,
        label="规则审查",
        extra={
            "requires_real_model": True,
            "missing_model_problem": (
                "AI_CONNECTION_MISSING",
                "当前没有可用于规则审查的真实 AI 连接。",
                "请配置真实 AI 连接后重试。",
            ),
        },
    )
)


__all__ = ["KIND", "RULE_REVIEW_HANDLER"]
