"""表格结构识别（``structure_suggestion``）：一次模型调用，一个条目。

两种目标：
- ``draft``：已导入的草稿。条目成功时按识别出的结构重新解析，算出与当前草稿的差异，
  带指纹存进 ``raw_model_output.structure_suggestions``（与同步接口相同的落库）。
- ``import``：导入前（E1/E7 识别失败，还没有评分标准）。上传文件在建任务时只解析成
  台账，任务没有 ``rubric_id``，按建任务的用户可见；结果（结构与将导入的评分项）
  只在任务里，用户确认后带 ``structure_override`` 调用导入接口。

输出不合格（JSON 无效、行列编号越界、按该结构解析不出评分项）时带错误码修正一次，
与同步识别的两次尝试相同。
"""

from __future__ import annotations

from backend.app.services.ai_tasks.errors import AITaskItemError
from backend.app.services.ai_tasks.errors import AITaskProblem
from backend.app.services.ai_tasks.handlers import PreparedTask
from backend.app.services.ai_tasks.handlers import TaskHandler
from backend.app.services.ai_tasks.handlers import register_handler
from backend.app.services.ai_tasks.provider_errors import provider_item_error
from backend.app.services.llm.errors import ProviderJSONOutputError
from backend.app.services.rubric_import import structure_state
from backend.app.services.rubric_import.extraction.llm_structure import PROVIDER_HINTS
from backend.app.services.rubric_import.extraction.llm_structure import STRUCTURE_PROMPT_VERSION
from backend.app.services.rubric_import.extraction.llm_structure import recognize_once
from backend.app.services.rubric_import.extraction.structure_override import StructureOverrideError
from backend.app.services.rubric_import.parse_state import ParseStateError


KIND = "structure_suggestion"
INVALID_MESSAGE = "模型两次输出的结构都未通过校验，请改为人工确认结构。"


def _problem(exc: ParseStateError) -> AITaskProblem:
    return AITaskProblem(exc.status, exc.code, exc.message, "请刷新后重试。")


def _prepare(session, rubric_id, params, _principal):
    if rubric_id is None:
        # 导入前识别：``upload`` 由导入前识别接口在服务端解析上传文件得到，不接受客户端传入。
        upload = params.get("upload")
        if not isinstance(upload, dict):
            raise AITaskProblem(422, "AI_TASK_PARAMS_INVALID", "缺少要识别的表格。", "请重新上传文件。")
        snapshot = {"target": "import", "ledger": upload["ledger"], "failure_codes": upload["failure_codes"]}
        request = upload["request"]
    else:
        if (params.get("target") or "draft") != "draft":
            raise AITaskProblem(422, "AI_TASK_PARAMS_INVALID", "识别目标无效。", "请刷新页面后重试。")
        try:
            prepared = structure_state.draft_structure_request(session, rubric_id)
        except ParseStateError as exc:
            raise _problem(exc) from exc
        snapshot = {
            "target": "draft",
            "compilation_id": prepared["compilation_id"],
            "ledger": prepared["ledger"],
            "failure_codes": prepared["failure_codes"],
        }
        request = prepared["request"]
    return PreparedTask(
        scope={"target": snapshot["target"]},
        input_snapshot=snapshot,
        items=[{"label": snapshot["target"], "request": request}],
    )


def _invalid(code) -> AITaskItemError:
    return AITaskItemError(code, INVALID_MESSAGE, disposition="repair")


def _run_item(view, item_input, scorer):
    snapshot = view.input_snapshot
    sheets = structure_state.sheets_from_ledger_mapping(snapshot["ledger"])
    try:
        result = recognize_once(sheets, scorer, item_input["request"], repair_code=item_input.get("repair_code"))
    except ProviderJSONOutputError as exc:
        if exc.reason == "output_truncated":
            raise AITaskItemError(
                "STRUCTURE_OUTPUT_TRUNCATED",
                "模型输出达到长度上限，结构 JSON 未生成完整；请提高连接的输出 Token 上限或缩小表格范围后重试。",
            ) from exc
        if exc.reason == "refused":
            raise AITaskItemError("STRUCTURE_REFUSED", "模型拒绝识别这份表格（安全策略），请改为人工确认结构。") from exc
        if exc.reason == "error_envelope":
            raise AITaskItemError(
                "AI_PROVIDER_ERROR", "AI 接口在成功状态中返回错误，请测试当前连接后重试。", disposition="retry"
            ) from exc
        raise _invalid("STRUCTURE_OUTPUT_INVALID") from exc
    except StructureOverrideError as exc:
        raise _invalid(exc.code) from exc
    except Exception as exc:
        raise provider_item_error(
            exc, code="AI_PROVIDER_ERROR", message="AI 请求失败，请测试当前连接后重试。", hints=PROVIDER_HINTS
        ) from exc
    if snapshot["target"] == "import":
        try:
            return structure_state.import_structure_preview(
                snapshot["ledger"], result, failure_codes=snapshot["failure_codes"]
            )
        except StructureOverrideError as exc:
            raise _invalid(exc.code) from exc
    return {**result, "failure_codes": list(snapshot["failure_codes"])}


def _on_item_success(session, view, _item_input, output):
    if view.input_snapshot["target"] != "draft":
        return
    try:
        structure_state.store_structure_suggestion(
            session,
            view.rubric_id,
            output,
            compilation_id=view.input_snapshot["compilation_id"],
            actor_id=view.owner_id,
        )
    except StructureOverrideError as exc:
        raise _invalid(exc.code) from exc
    except ParseStateError as exc:
        raise AITaskItemError(exc.code, exc.message) from exc


def _merge(view, outputs):
    return {"target": view.input_snapshot["target"], **outputs[0]}


STRUCTURE_SUGGESTION_HANDLER = register_handler(
    TaskHandler(
        kind=KIND,
        prompt_version=STRUCTURE_PROMPT_VERSION,
        prepare=_prepare,
        run_item=_run_item,
        merge=_merge,
        on_item_success=_on_item_success,
        label="AI 识别表格结构",
        extra={
            "requires_real_model": True,
            "missing_model_problem": (
                "AI_CONNECTION_MISSING",
                "当前没有可用于识别结构的真实 AI 连接。",
                "请配置真实 AI 连接后重试，或改为人工确认结构。",
            ),
        },
    )
)


__all__ = ["KIND", "STRUCTURE_SUGGESTION_HANDLER"]
