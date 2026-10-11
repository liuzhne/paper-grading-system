"""抽取器 LLM 兜底的业务流程（解析重构方案 §8、阶段 6）。

- 导入前预检：E1 等识别失败时对上传文件做结构识别，不落库；用户确认后带
  ``structure_override`` 重新调用导入接口。
- 草稿结构建议：从台账重建原文，LLM 识别结构，按该结构重新解析得到差异；
  建议连同指纹存入 ``raw_model_output.structure_suggestions``，刷新不丢失。
- 两者的模型调用都由 AI 任务（``ai_tasks.structure_suggestion``）执行，这里只负责
  冻结输入、估算、试解析与落库。
- 合入 / 撤销：按确认后的结构重新解析生成新草稿（只增不删）；只允许在导入后
  尚未人工编辑的草稿上进行，否则重新解析会覆盖人工编辑。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db import models
from backend.app.services.rubric_import import pipeline
from backend.app.services.rubric_import.coverage import compute_coverage
from backend.app.services.rubric_import.extraction.docx_extractor import table_sheets
from backend.app.services.rubric_import.extraction.llm_structure import STRUCTURE_PROMPT_VERSION
from backend.app.services.rubric_import.extraction.llm_structure import StructureError
from backend.app.services.rubric_import.extraction.llm_structure import build_table_payload
from backend.app.services.rubric_import.extraction.structure_override import StructureOverrideError
from backend.app.services.rubric_import.extraction.structure_override import extract_with_override
from backend.app.services.rubric_import.extraction.table_extractor import extract_table
from backend.app.services.rubric_import.extraction.table_extractor import header_aliases_for
from backend.app.services.rubric_import.extraction.triggers import detect_triggers
from backend.app.services.rubric_import.parse_state import ParseStateError
from backend.app.services.rubric_import.parse_state import require_editable_ledger
from backend.app.services.rubric_import.sources.docx_adapter import load_docx
from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.services.rubric_import.sources.xlsx_adapter import load_xlsx
from backend.app.services.rubric_import.suggestions import diff_criteria
from backend.app.services.rubric_import.suggestions import merge_plan
from backend.app.services.rubric_import.suggestions import structure_fingerprint

_SERVICE_UNAVAILABLE = {"AI_CONNECTION_MISSING", "AI_PROVIDER_ERROR"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _structure_problem(exc: Exception) -> ParseStateError:
    code = getattr(exc, "code", "STRUCTURE_OUTPUT_INVALID")
    return ParseStateError(503 if code in _SERVICE_UNAVAILABLE else 422, code, getattr(exc, "message", str(exc)))


def sheets_from_ledger_mapping(mapping: dict):
    """从台账重建表格视图（Excel 工作表或 Word 表格）；AI 任务执行时据此校验模型输出。"""

    return pipeline._override_sheets(SourceLedger.from_mapping(mapping))


def import_structure_request(*, rules_bytes, template_bytes, business_profile_key: str = "thesis") -> dict:
    """导入前识别的冻结输入：上传文件只解析成台账（不落库），连同失败码与发送内容。"""

    if not rules_bytes and not template_bytes:
        raise ParseStateError(400, "FILE_REQUIRED", "请至少上传一份评分标准文件（Word 或 Excel）")
    ledger = SourceLedger()
    try:
        if rules_bytes:
            sheets = load_xlsx(rules_bytes, ledger, doc_id="excel", doc_role="rules")
            failure_code = "E1"
        else:
            sheets = table_sheets(load_docx(template_bytes, ledger, doc_id="word", doc_role="rules"))
            failure_code = "E7"
    except ValueError as exc:
        raise ParseStateError(400, "FILE_UNREADABLE", str(exc)) from exc
    if not sheets:
        raise ParseStateError(422, "NO_TABLE", "文档中没有可以识别结构的表格。")
    scratch = SourceLedger.from_mapping(ledger.to_mapping())
    known_mapping: dict = {}
    try:
        extraction = extract_table(sheets, scratch, header_aliases=header_aliases_for(business_profile_key))
        codes = [item["code"] for item in detect_triggers(extraction, scratch, compute_coverage(scratch))]
        known_mapping = {name: column + 1 for name, column in extraction.mapping.items()}
    except ValueError:
        codes = [failure_code]
    try:
        request = build_table_payload(sheets, failure_codes=codes, known_mapping=known_mapping)
    except StructureError as exc:
        raise _structure_problem(exc) from exc
    return {"ledger": ledger.to_mapping(), "failure_codes": codes, "request": request}


def estimate_import_structure(*, rules_bytes, template_bytes, business_profile_key: str = "thesis") -> dict:
    prepared = import_structure_request(
        rules_bytes=rules_bytes, template_bytes=template_bytes, business_profile_key=business_profile_key
    )
    return {"failure_codes": prepared["failure_codes"], "estimate": prepared["request"]["estimate"]}


def import_structure_preview(ledger_mapping: dict, result: dict, *, failure_codes) -> dict:
    """按模型给出的结构试解析上传的表格，列出将导入的评分项（不落库）。"""

    sheets = sheets_from_ledger_mapping(ledger_mapping)
    preview = extract_with_override(sheets, SourceLedger.from_mapping(ledger_mapping), result["override"])
    return {
        **result,
        "failure_codes": list(failure_codes),
        "preview": [
            {"name": row.criterion.name, "max_score": row.criterion.max_score, "row_number": row.row_number}
            for row in preview.records
        ],
    }


def _file_import_origin(raw: dict) -> bool:
    """当前编译记录由文件导入或结构合入产生（而非人工重编译）。"""

    return "sheet_name" in raw and not raw.get("source_kind")


def _require_reparse_source(session: Session, rubric_id: str):
    compilation = require_editable_ledger(session, rubric_id)
    if not _file_import_origin(compilation.raw_parse_output or {}):
        raise ParseStateError(
            409, "STRUCTURE_REPARSE_AFTER_EDIT",
            "草稿在导入后已经人工编辑，按结构重新解析会覆盖这些编辑；请重新导入文件后再使用结构识别。",
        )
    return compilation


def current_criteria(session: Session, rubric_id: str) -> list[dict]:
    rows = session.scalars(
        select(models.RubricCriterion)
        .where(models.RubricCriterion.rubric_id == rubric_id)
        .order_by(models.RubricCriterion.display_order, models.RubricCriterion.code)
    ).all()
    return [
        {"code": c.code, "name": c.name, "max_score": float(c.max_score), "description": c.description,
         "deduction_rules": list(c.deduction_rules or [])}
        for c in rows
    ]


def _criteria_with_rows(session, rubric_id, raw, *, sheet=None):
    criteria = current_criteria(session, rubric_id)
    # SourceRule codes are authoritative only for simple one-row criteria.
    sources = raw.get("source_rules") or []
    for criterion in criteria:
        matches = [s for s in sources if s.get("source_rule_code") == criterion["code"]
                   and (sheet is None or s.get("sheet_name") == sheet)]
        if len(matches) == 1:
            criterion["row_number"] = matches[0].get("row_number")
    return criteria


def _artifacts(compilation) -> list[dict]:
    return [
        {"artifact_type": a.artifact_type, "file_name": a.file_name, "file_hash": a.file_hash,
         "file_size_bytes": a.file_size_bytes, "token": a.artifact_type}
        for a in sorted(compilation.artifacts, key=lambda item: item.artifact_type)
    ]


def _next_version(session: Session, rubric_id: str, base: str) -> str:
    used = set(session.scalars(select(models.RubricVersion.version).where(models.RubricVersion.rubric_id == rubric_id)))
    ordinal = 2
    while f"{base}-draft.{ordinal}" in used:
        ordinal += 1
    return f"{base}-draft.{ordinal}"


def _command(session: Session, rubric_id: str, compilation, *, model=None) -> dict:
    rubric = session.get(models.Rubric, rubric_id)
    version = session.scalars(
        select(models.RubricVersion).where(models.RubricVersion.compilation_id == compilation.id)
    ).first()
    base = (version.version if version else rubric.version).split("-draft.")[0]
    return {
        "schema_version": pipeline.IMPORT_SCHEMA_VERSION,
        "source_kind": "file_import",
        "rubric": {
            "name": rubric.name,
            "version": base,
            "description": rubric.description,
            "business_profile_key": version.business_profile_key if version else "thesis",
            "workflow_profile": version.workflow_profile if version else "template_driven",
        },
        "files": {},
        "compiler": {
            "parser_version": compilation.parser_version,
            "compiler_version": compilation.compiler_version,
            "prompt_version": STRUCTURE_PROMPT_VERSION,
            "model_provider": (model or {}).get("provider"),
            "model_name": (model or {}).get("model_name"),
            "sampling_params": {},
        },
        "version": {"hash_scheme": "rubric-content-v2", "version": _next_version(session, rubric_id, base)},
        "draft_recompile": {"mode": pipeline.STRUCTURE_REPARSE_MODE, "supersedes_compilation_id": compilation.id},
    }


def draft_structure_request(session: Session, rubric_id: str) -> dict:
    """草稿结构建议的冻结输入：从台账重建原文，连同失败码与发送内容。"""

    compilation = _require_reparse_source(session, rubric_id)
    raw = compilation.raw_parse_output
    sheets = sheets_from_ledger_mapping(raw["source_ledger"])
    extraction = raw.get("extraction") or {}
    codes = [item["code"] for item in raw.get("triggers") or []]
    extra = [item["row_number"] for item in extraction.get("dropped_rows") or []]
    if extraction.get("total_row"):
        extra.append(extraction["total_row"]["row_number"])
    options = {"failure_codes": codes, "known_mapping": extraction.get("mapping") or {},
               "extra_rows": {extraction.get("sheet_title"): extra}}
    try:
        request = build_table_payload(sheets, **options)
    except StructureError as exc:
        raise _structure_problem(exc) from exc
    return {"compilation_id": compilation.id, "ledger": deepcopy(raw["source_ledger"]),
            "failure_codes": codes, "request": request}


def estimate_draft_structure(session: Session, rubric_id: str) -> dict:
    prepared = draft_structure_request(session, rubric_id)
    return {"failure_codes": prepared["failure_codes"], "estimate": prepared["request"]["estimate"]}


def store_structure_suggestion(session: Session, rubric_id: str, result: dict, *, compilation_id: str,
                               actor_id: str | None) -> dict:
    """按模型给出的结构重新解析，算出与当前草稿的差异并带指纹存入草稿。

    识别期间草稿被换掉（重新导入、合入或人工编辑）时拒绝：结构里的行列编号对应的是
    旧台账。``StructureOverrideError`` 原样抛出，由 AI 任务按“输出不合格”处理。
    """

    compilation = _require_reparse_source(session, rubric_id)
    if compilation.id != compilation_id:
        raise ParseStateError(
            409, "STRUCTURE_SOURCE_CHANGED", "识别期间草稿已变化（重新导入或合入了结构），请重新识别。"
        )
    raw = compilation.raw_parse_output
    reparsed = pipeline.prepare_structure_reparse(
        command=_command(session, rubric_id, compilation, model=result["model"]), raw_parse_output=raw,
        artifacts=_artifacts(compilation), structure_override=result["override"],
    ).to_mapping()
    rows = {item["name"]: item["row_number"]
            for item in reparsed["compilation"]["raw_parse_output"]["extraction"]["records"]}
    proposed = [{**item, "row_number": rows.get(item["name"])} for item in reparsed["criteria"]]
    current = _criteria_with_rows(session, rubric_id, raw, sheet=result["override"].get("sheet"))
    items = diff_criteria(current, proposed)
    stored = {
        **deepcopy(result),
        "items": items,
        "fingerprint": structure_fingerprint(raw["source_ledger"], current, result["override"]),
        "previous_override": deepcopy(raw.get("structure_override")),
        "status": "pending",
        "requested_by": actor_id,
        "created_at": _now(),
    }
    compilation.raw_model_output = {**deepcopy(compilation.raw_model_output or {}), "structure_suggestions": stored}
    session.flush()
    return {**deepcopy(stored), "stale": False, "plan": merge_plan(items)}


def structure_suggestion_view(session: Session, rubric_id: str, compilation) -> dict | None:
    stored = (compilation.raw_model_output or {}).get("structure_suggestions")
    if not stored:
        return None
    raw = compilation.raw_parse_output or {}
    stale = stored.get("status") == "pending" and (
        structure_fingerprint(raw.get("source_ledger"), current_criteria(session, rubric_id), stored.get("override"))
        != stored.get("fingerprint")
    )
    return {**deepcopy(stored), "stale": stale}


def prepare_merge(session: Session, rubric_id: str, *, fingerprint: str, confirm, exclude):
    compilation = _require_reparse_source(session, rubric_id)
    raw = compilation.raw_parse_output
    stored = (compilation.raw_model_output or {}).get("structure_suggestions")
    if not stored or stored.get("status") != "pending":
        raise ParseStateError(409, "SUGGESTION_NOT_PENDING", "没有待合入的结构建议，请重新识别。")
    expected = structure_fingerprint(raw["source_ledger"], current_criteria(session, rubric_id), stored["override"])
    if fingerprint != stored["fingerprint"] or expected != stored["fingerprint"]:
        raise ParseStateError(409, "SUGGESTION_STALE", "结构建议已过期（评分项或文件已变化），请重新识别。")
    plan = merge_plan(stored["items"], confirm=set(confirm), exclude=set(exclude))
    if plan["blocked"]:
        raise ParseStateError(
            409, "SUGGESTION_BLOCKED",
            "以下差异需要处理后才能合入：" + "、".join(plan["blocked"])
            + "（移除已有评分项的结构无法合入，请调整结构或重新导入；冲突项需排除）",
        )
    override = deepcopy(stored["override"])
    override.setdefault("row_types", {})
    for row in plan["exclude_rows"]:
        override["row_types"][str(row)] = "note"
    # Keep existing identifiers even if the structure extractor emits new codes.
    # Name changes remain individually confirmable through keep_values.
    identities = [{"row_number": c["row_number"], "field": "code", "value": c["code"]}
                  for c in _criteria_with_rows(session, rubric_id, raw, sheet=override.get("sheet"))
                  if c.get("row_number") is not None]
    try:
        prepared = pipeline.prepare_structure_reparse(
            command=_command(session, rubric_id, compilation, model=stored.get("model")), raw_parse_output=raw,
            artifacts=_artifacts(compilation), structure_override=override, keep_values=[*identities, *plan["keep_values"]],
        )
    except StructureOverrideError as exc:
        raise _structure_problem(exc) from exc
    return prepared, {"plan": plan, "fingerprint": stored["fingerprint"], "previous_override": stored.get("previous_override")}


def prepare_undo(session: Session, rubric_id: str):
    compilation = _require_reparse_source(session, rubric_id)
    stored = (compilation.raw_model_output or {}).get("structure_suggestions")
    if not stored or stored.get("status") != "merged":
        raise ParseStateError(409, "NOTHING_TO_UNDO", "没有可以撤销的结构合入。")
    prepared = pipeline.prepare_structure_reparse(
        command=_command(session, rubric_id, compilation), raw_parse_output=compilation.raw_parse_output,
        artifacts=_artifacts(compilation), structure_override=stored.get("previous_override"),
    )
    incoming = {item["code"] for item in prepared["criteria"]}
    if not {c["code"] for c in current_criteria(session, rubric_id)} <= incoming:
        raise ParseStateError(
            409, "UNDO_WOULD_REMOVE_CRITERIA",
            "该次合入新增了评分项，撤销需要删除它们，而已有评分项不能删除；请重新导入文件。",
        )
    return prepared, stored


def record_structure_event(session: Session, rubric_id: str, *, action: str, status: str, actor_id: str,
                           reason: str, details: dict) -> None:
    """合入 / 撤销后，在新编译记录上标记建议状态并追加审计事件。"""

    from backend.app.services.rubric_import.parse_state import current_compilation

    compilation = current_compilation(session, rubric_id)
    output = deepcopy(compilation.raw_model_output or {})
    if output.get("structure_suggestions"):
        output["structure_suggestions"] = {**output["structure_suggestions"], "status": status,
                                           f"{status}_at": _now()}
    compilation.raw_model_output = output
    compilation.human_changes = [
        *deepcopy(list(compilation.human_changes or [])),
        {"action": action, "actor_id": actor_id, "reason": reason, "occurred_at": _now(), **deepcopy(details)},
    ]
    session.flush()
