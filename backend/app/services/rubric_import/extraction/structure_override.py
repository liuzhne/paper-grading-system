"""按用户确认的结构确定性抽取（解析重构方案 §6.5、阶段 6）。

LLM 结构识别只产出“结构”（表头行、列→字段映射、行类型），不产出任何文字；
用户确认后由这里按结构重新抽取，文字一律取自原文单元。
"""

from __future__ import annotations

from backend.app.services.rubric_import.extraction.table_extractor import IGNORED_SHEET_REASON
from backend.app.services.rubric_import.extraction.table_extractor import ExtractedRow
from backend.app.services.rubric_import.extraction.table_extractor import TableExtraction
from backend.app.services.rubric_import.extraction.table_extractor import _claim_sheet
from backend.app.services.rubric_import.extraction.table_extractor import _criterion_from_row
from backend.app.services.rubric_import.extraction.table_extractor import _is_full_row_note
from backend.app.services.rubric_import.extraction.table_extractor import _normalize
from backend.app.services.rubric_import.extraction.table_extractor import _parse_score
from backend.app.services.rubric_import.extraction.table_extractor import _sheet_units
from backend.app.services.rubric_import.extraction.table_extractor import _value
from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.services.rubric_import.sources.xlsx_adapter import SheetView

FIELDS = (
    "code", "item_label", "name", "dimension", "max_score", "weight", "description", "deduction_rules",
    "evidence_hints", "rubric_levels", "applies_to", "criterion_type", "sub_checks", "display_order",
)
ROW_TYPES = ("criterion", "dimension", "global_rule", "note", "total", "empty")
_DROP_REASONS = {"global_rule": "用户确认：全局规则", "note": "用户确认：说明行"}
MIN_NUMERIC_RATIO = 0.5


class StructureOverrideError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_override(value, sheets: list[SheetView]) -> dict:
    """校验并规范化结构覆盖：工作表存在、表头行在范围内、字段合法且一列一字段、
    满分列多为数字、行类型合法。列号与行号均为 1 基。"""

    if not isinstance(value, dict):
        raise StructureOverrideError("OVERRIDE_INVALID", "结构确认内容格式不正确。")
    sheet = next((item for item in sheets if item.title == value.get("sheet")), None)
    if sheet is None:
        raise StructureOverrideError("SHEET_NOT_FOUND", "确认的工作表或表格不存在。")
    header_row = value.get("header_row")
    if not isinstance(header_row, int) or not 1 <= header_row <= len(sheet.rows):
        raise StructureOverrideError("HEADER_ROW_INVALID", "表头行不在表格范围内。")
    mapping = value.get("column_mapping")
    if not isinstance(mapping, dict):
        raise StructureOverrideError("OVERRIDE_INVALID", "缺少列与字段的对应关系。")
    width = max((len(row) for row in sheet.rows), default=0)
    columns = {}
    for field_name, column in mapping.items():
        if field_name not in FIELDS:
            raise StructureOverrideError("FIELD_INVALID", f"不支持的字段：{field_name}")
        if not isinstance(column, int) or not 1 <= column <= width:
            raise StructureOverrideError("COLUMN_OUT_OF_RANGE", f"字段 {field_name} 的列号超出表格范围。")
        if column - 1 in columns.values():
            raise StructureOverrideError("COLUMN_REUSED", "同一列不能对应多个字段。")
        columns[field_name] = column - 1
    if "name" not in columns and "item_label" not in columns:
        raise StructureOverrideError("NAME_COLUMN_REQUIRED", "必须指定评分项名称列。")
    if "max_score" not in columns and "item_label" not in columns:
        raise StructureOverrideError("SCORE_COLUMN_REQUIRED", "必须指定满分列。")
    if "max_score" in columns:
        samples = [
            _value(row, columns["max_score"]) for row in sheet.rows[header_row:]
            if _value(row, columns["max_score"])
        ]
        numeric = sum(1 for item in samples if _parse_score(item) is not None and _is_numeric_text(item))
        if samples and numeric / len(samples) < MIN_NUMERIC_RATIO:
            raise StructureOverrideError("SCORE_COLUMN_NOT_NUMERIC", "满分列的内容大多不是数字。")
    row_types = {}
    for row_number, row_type in (value.get("row_types") or {}).items():
        if row_type not in ROW_TYPES:
            raise StructureOverrideError("ROW_TYPE_INVALID", f"不支持的行类型：{row_type}")
        row_types[int(row_number)] = row_type
    return {"sheet": sheet.title, "header_row": header_row, "column_mapping": {k: v + 1 for k, v in columns.items()},
            "row_types": {str(k): v for k, v in sorted(row_types.items())}}


def _is_numeric_text(value) -> bool:
    text = _normalize(value).replace("分", "").strip()
    try:
        float(text)
    except ValueError:
        return False
    return True


def extract_with_override(sheets: list[SheetView], ledger: SourceLedger, override: dict) -> TableExtraction:
    sheet = next(item for item in sheets if item.title == override["sheet"])
    header_index = override["header_row"] - 1
    mapping = {name: column - 1 for name, column in override["column_mapping"].items()}
    row_types = {int(key): value for key, value in (override.get("row_types") or {}).items()}
    result = TableExtraction(
        sheet_title=sheet.title,
        header_index=header_index,
        mapping=mapping,
        headers=[_normalize(value) for value in sheet.rows[header_index]],
    )
    for offset, values in enumerate(sheet.rows[header_index + 1 :], start=header_index + 1):
        row_number = offset + 1
        units = sheet.row_units(offset)
        present = [unit for unit in dict.fromkeys(units) if unit]
        if not present:
            continue
        row_type = row_types.get(row_number)
        if row_type is None:
            if _is_full_row_note(units, mapping):
                result.dropped_rows.append({"row_number": row_number, "unit_ids": present, "reason": "整行合并的说明行"})
                continue
            row_type = "criterion"
        if row_type == "total":
            result.total_row = {"row_number": row_number,
                                "declared_total": _parse_score(_value(values, mapping.get("max_score")))}
            continue
        if row_type in ("dimension", "empty"):
            for unit_id in present:
                ledger.mark(unit_id, "context" if row_type == "dimension" else "structural",
                            reason="用户确认：空行" if row_type == "empty" else None)
            continue
        if row_type in _DROP_REASONS:
            result.dropped_rows.append({"row_number": row_number, "unit_ids": present, "reason": _DROP_REASONS[row_type]})
            continue
        criterion = _criterion_from_row(values, mapping, len(result.records) + 1)
        if criterion is None:
            result.dropped_rows.append({"row_number": row_number, "unit_ids": present, "reason": "未找到满分"})
            continue
        result.records.append(ExtractedRow(row_number, tuple(values), units, criterion))
    if not result.records:
        raise StructureOverrideError("NO_CRITERIA", "按确认的结构没有识别到评分项。")
    _claim_sheet(result, sheet, ledger)
    for other in sheets:
        if other.title == sheet.title:
            continue
        result.ignored_sheets.append({"title": other.title, "reason": IGNORED_SHEET_REASON})
        for unit_id in _sheet_units(other):
            ledger.mark(unit_id, "ignored_by_rule", reason=IGNORED_SHEET_REASON)
    return result
