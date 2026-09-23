"""确定性表格抽取器（解析重构方案 §4 第 2 层）。

表格解析函数自 ``parser.py`` 迁入，行为保持不变；``extract_table`` 在此基础上
基于原文单元工作，并为每个单元登记状态（consumed / structural / ignored_by_rule），
未被认领的内容会进入覆盖率报告。
"""

import re
from dataclasses import dataclass, field

from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.services.rubric_import.sources.xlsx_adapter import SheetView

HEADER_ALIASES = {
    "code": ["编号", "指标编号", "评分项编号", "代码", "code", "criterion_code"],
    # 部分院校模板把稳定标识和满分合并在“打分项”文本中，例如
    # “指导教师成绩项1（20分）”。它不是评分项名称，需单独识别。
    "item_label": ["打分项", "成绩项"],
    "name": [
        "评分项",
        "评分指标",
        "评价内容",
        "指标",
        "评价项目",
        "项目",
        "name",
        "criterion",
    ],
    "max_score": ["分值", "满分", "分数", "最高分", "权重分", "max_score", "score", "points"],
    "weight": ["权重", "weight"],
    "description": [
        "说明",
        "评分说明",
        "评价标准",
        "评分标准",
        "标准说明",
        "具体要求",
        "描述",
        "description",
    ],
    "evidence_hints": ["依据", "证据", "证据提示", "章节依据", "相关章节", "关键词", "evidence_hints"],
    "deduction_rules": ["扣分规则", "扣分点", "扣分说明", "扣分原因", "deduction_rules"],
    "display_order": ["顺序", "排序", "display_order"],
    "criterion_type": ["类型", "判定类型", "评分类型", "判定方式", "type"],
    "applies_to": ["适用范围", "适用章节", "作用范围", "范围", "applies_to"],
    "rubric_levels": ["分档", "档位", "等级标准", "评分档次", "rubric_levels"],
    "dimension": ["维度", "评价维度", "评分维度", "所属维度", "dimension"],
    "sub_checks": ["子检查", "子项", "子检查项", "混合子项", "子项检查", "sub_checks"],
}


@dataclass
class ImportedCriterion:
    code: str
    name: str
    max_score: float
    weight: float | None
    description: str | None
    evidence_hints: list[str]
    deduction_rules: list[str]
    display_order: int
    criterion_type: str = "llm_judgment"
    scoring_mode: str = "llm_direct"
    applies_to: str = "global"
    rubric_levels: list = field(default_factory=list)
    sub_checks: list = field(default_factory=list)
    dimension: str | None = None
    deduction_rules_structured: list = field(default_factory=list)


def _find_header(rows, header_aliases=None):
    for index, row in enumerate(rows[:15]):
        normalized = [_normalize_header(cell) for cell in row]
        mapping = {}
        used_columns = set()
        for field, aliases in (header_aliases or HEADER_ALIASES).items():
            for alias in aliases:
                alias_norm = _normalize_header(alias)
                for col, value in enumerate(normalized):
                    if col in used_columns:
                        continue
                    if value and (value == alias_norm or alias_norm in value):
                        mapping[field] = col
                        used_columns.add(col)
                        break
                if field in mapping:
                    break
        if "name" in mapping and (
            "max_score" in mapping or "item_label" in mapping
        ):
            return index, mapping
    return None, {}


def _criterion_from_row(row, mapping, order):
    item_label = _value(row, mapping.get("item_label"))
    name = _value(row, mapping.get("name")) or _name_from_item_label(item_label) or item_label
    if not name or name in {"合计", "总分", "总计"}:
        return None
    max_score = _parse_score(_value(row, mapping.get("max_score")))
    if max_score is None:
        max_score = _parse_embedded_max_score(item_label)
    if max_score is None:
        return None

    code = (
        _value(row, mapping.get("code"))
        or _code_from_item_label(item_label)
        or "C%02d" % order
    )
    description = _value(row, mapping.get("description"))
    evidence_hints = _split_items(_value(row, mapping.get("evidence_hints")))
    deduction_rules = _split_items(_value(row, mapping.get("deduction_rules")))
    weight = _parse_score(_value(row, mapping.get("weight"))) if "weight" in mapping else None
    parsed_order = _parse_score(_value(row, mapping.get("display_order")))
    display_order = int(parsed_order if parsed_order is not None else order)
    criterion_type = _parse_type(_value(row, mapping.get("criterion_type")))
    applies_to = _parse_applies_to(_value(row, mapping.get("applies_to")))
    rubric_levels = _parse_bands(_value(row, mapping.get("rubric_levels")))
    scoring_mode = "banded" if rubric_levels else "llm_direct"
    dimension = _value(row, mapping.get("dimension"))
    sub_checks = _parse_sub_checks(_raw_value(row, mapping.get("sub_checks")))
    if sub_checks:
        criterion_type = "hybrid"  # 提供子检查即启用混合制（设计§2/§6.3）
    return ImportedCriterion(
        code=str(code),
        name=str(name),
        max_score=max_score,
        weight=weight,
        description=description,
        evidence_hints=evidence_hints,
        deduction_rules=deduction_rules,
        display_order=display_order,
        criterion_type=criterion_type,
        scoring_mode=scoring_mode,
        applies_to=applies_to,
        rubric_levels=rubric_levels,
        sub_checks=sub_checks,
        dimension=dimension,
    )


def _parse_embedded_max_score(value):
    if not value:
        return None
    match = re.search(r"[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]", str(value))
    return float(match.group(1)) if match else None


_ITEM_SCORE_SUFFIX = re.compile(r"\s*[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)]\s*$")


def _name_from_item_label(value):
    """Use an explicit item label as a stable name without duplicating its score."""

    if not value:
        return None
    return _ITEM_SCORE_SUFFIX.sub("", str(value)).strip() or None


def _code_from_item_label(value):
    if not value:
        return None
    match = re.search(r"指导教师(?:成绩|评分)项\s*(\d+)", str(value))
    return "T%02d" % int(match.group(1)) if match else None


def _value(row, index):
    if index is None or index >= len(row):
        return None
    value = row[index]
    if value is None:
        return None
    return _normalize(value)


def _raw_value(row, index):
    """取原始单元格文本，**保留换行**（子检查按行分隔，不能被空白折叠）。"""
    if index is None or index >= len(row):
        return None
    value = row[index]
    return None if value is None else str(value)


def _parse_type(value):
    if not value:
        return "llm_judgment"
    text = str(value)
    if any(token in text for token in ["确定", "自动", "规则", "deterministic"]):
        return "deterministic"
    if any(token in text for token in ["混合", "hybrid"]):
        return "hybrid"
    return "llm_judgment"


def _parse_sub_checks(value):
    """解析混合制子检查列。每行一个子检查，字段以 `|`（或 `｜`）分隔：`名称 | 类型 | 分值`。

    类型缺省为语义判断（llm_judgment），含"确定/规则/自动/deterministic"则为确定性子检查（不调 LLM）。
    产出引擎可消费的 `{kind,name,criteria,max_points}` 列表（见 engine._make_sub_criterion）。
    """
    if not value:
        return []
    items = []
    for line in re.split(r"[\n;；]+", str(value)):
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in re.split(r"[|｜]", line)]
        name = parts[0] if parts else ""
        if not name:
            continue
        kind = _parse_sub_kind(parts[1]) if len(parts) > 1 else "llm_judgment"
        max_points = _parse_score(parts[2]) if len(parts) > 2 else 0.0
        items.append(
            {"kind": kind, "name": name, "criteria": name, "max_points": float(max_points or 0)}
        )
    return items


def _parse_sub_kind(value):
    if value and any(token in str(value) for token in ["确定", "规则", "自动", "deterministic", "det"]):
        return "deterministic"
    return "llm_judgment"


def _parse_applies_to(value):
    if not value:
        return "global"
    text = _normalize(value)
    if text in {"全局", "整体", "全文", "通用", "global"}:
        return "global"
    return text


def _parse_bands(value):
    if not value:
        return []
    text = str(value)
    pairs = re.findall(r"([一-鿿A-Za-z]+)\s*[:：]?\s*(\d+(?:\.\d+)?)", text)
    if pairs:
        return [{"label": label, "points": float(points)} for label, points in pairs]
    return [{"raw": _normalize(text)}]


def _parse_score(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    return float(match.group(0))


def _split_items(value):
    if not value:
        return []
    parts = re.split(r"[\n\r;；、]+", str(value))
    return [part.strip() for part in parts if part and part.strip()]


def _normalize(value):
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def _normalize_header(value):
    """Normalize headers more aggressively than cell content.

    Internal spaces in headers such as ``具  体  要  求`` are layout-only and
    must not prevent alias matching.
    """

    return re.sub(r"\s+", "", _normalize(value)).lower()


# 各 Profile 在默认别名之上追加的表头写法；论文 Profile 使用默认别名，行为不变。
PROFILE_HEADER_ALIASES: dict[str, dict[str, list[str]]] = {}


def header_aliases_for(business_profile_key: str | None) -> dict[str, list[str]]:
    extra = PROFILE_HEADER_ALIASES.get(str(business_profile_key or ""), {})
    return {
        field_name: [*aliases, *[alias for alias in extra.get(field_name, []) if alias not in aliases]]
        for field_name, aliases in HEADER_ALIASES.items()
    }


_TOTAL_NAMES = {"合计", "总分", "总计"}
IGNORED_SHEET_REASON = "仅解析第一张识别到评分项的工作表"
NO_HEADER_REASON = "未识别到评分规则表头"


@dataclass
class ExtractedRow:
    row_number: int
    values: tuple
    unit_ids: list
    criterion: ImportedCriterion


@dataclass
class TableExtraction:
    sheet_title: str
    header_index: int
    mapping: dict
    headers: list
    records: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    unmapped_columns: list = field(default_factory=list)
    dropped_rows: list = field(default_factory=list)
    total_row: dict | None = None
    ignored_sheets: list = field(default_factory=list)
    structure_issues: list = field(default_factory=list)


def _extract_rows(sheet: SheetView, header_index: int, mapping: dict) -> TableExtraction:
    result = TableExtraction(
        sheet_title=sheet.title,
        header_index=header_index,
        mapping=mapping,
        headers=[_normalize(value) for value in sheet.rows[header_index]],
    )
    for offset, values in enumerate(sheet.rows[header_index + 1 :], start=header_index + 1):
        row_number = offset + 1
        units = sheet.row_units(offset)
        if _is_full_row_note(units, mapping):
            result.dropped_rows.append(
                {"row_number": row_number, "unit_ids": [u for u in dict.fromkeys(units) if u], "reason": "整行合并的说明行"}
            )
            continue
        criterion = _criterion_from_row(values, mapping, len(result.records) + 1)
        if criterion is not None:
            result.records.append(ExtractedRow(row_number, tuple(values), units, criterion))
            continue
        if not any(units):
            continue
        name = _value(values, mapping.get("name")) or _value(values, mapping.get("item_label"))
        if name in _TOTAL_NAMES or any(_normalize(value) in _TOTAL_NAMES for value in values if value is not None):
            result.total_row = {
                "row_number": row_number,
                "declared_total": _parse_score(_value(values, mapping.get("max_score"))),
            }
            continue
        result.dropped_rows.append(
            {
                "row_number": row_number,
                "unit_ids": [unit for unit in dict.fromkeys(units) if unit],
                "reason": "未找到评分项名称" if not name else "未找到满分",
            }
        )
    return result


def _shared_name_groups(extraction: TableExtraction) -> list[tuple[str, list[ExtractedRow]]]:
    """Return criterion rows whose names point at the same source cell.

    A repeated string in independent cells may be a legitimate duplicate name.  A
    repeated *unit id* means that the workbook author merged one cell across rows,
    which is a much stronger hierarchy signal.
    """

    name_column = extraction.mapping.get("name")
    if name_column is None:
        return []
    grouped: dict[str, list[ExtractedRow]] = {}
    for row in extraction.records:
        if name_column >= len(row.unit_ids):
            continue
        unit_id = row.unit_ids[name_column]
        if unit_id:
            grouped.setdefault(unit_id, []).append(row)
    return [(unit_id, rows) for unit_id, rows in grouped.items() if len(rows) > 1]


def _rows_have_independent_items(rows: list[ExtractedRow], mapping: dict) -> bool:
    item_column = mapping.get("item_label")
    if item_column is None:
        return False
    labels = [_value(row.values, item_column) for row in rows]
    if any(not label for label in labels) or len(set(labels)) != len(rows):
        return False
    codes = [row.criterion.code for row in rows]
    if len(set(codes)) != len(rows):
        return False
    # Without an explicit code column, the item labels themselves must carry stable
    # identifiers; otherwise replacing the only name column would invent identity.
    if "code" not in mapping:
        derived = [_code_from_item_label(label) for label in labels]
        if any(not code for code in derived) or len(set(derived)) != len(rows):
            return False
    descriptions = {row.criterion.description for row in rows if row.criterion.description}
    scores = {row.criterion.max_score for row in rows}
    return len(descriptions) > 1 or len(scores) > 1


def _hierarchy_mapping(extraction: TableExtraction) -> dict:
    """Reinterpret a merged parent name as ``dimension`` when it is unambiguous."""

    mapping = dict(extraction.mapping)
    shared = _shared_name_groups(extraction)
    suspicious = [
        (unit_id, rows)
        for unit_id, rows in shared
        if len({row.criterion.code for row in rows}) == len(rows)
        and (
            len({row.criterion.description for row in rows if row.criterion.description}) > 1
            or len({row.criterion.max_score for row in rows}) > 1
        )
    ]
    if not suspicious:
        return mapping

    details = {
        "code": "MERGED_NAME_AMBIGUOUS",
        "unit_ids": [unit_id for unit_id, _ in suspicious],
        "row_numbers": [row.row_number for _, rows in suspicious for row in rows],
        "criterion_codes": [row.criterion.code for _, rows in suspicious for row in rows],
    }
    if "dimension" in mapping:
        details["reason"] = "已经存在维度列，无法自动重新解释合并名称列。"
        extraction.structure_issues.append(details)
        return mapping
    if not all(_rows_have_independent_items(rows, mapping) for _, rows in suspicious):
        details["reason"] = "合并名称疑似父维度，但没有可作为独立评分项名称的稳定列。"
        extraction.structure_issues.append(details)
        return mapping

    mapping["dimension"] = mapping.pop("name")
    return mapping


def _extract_sheet(sheet: SheetView, header_aliases):
    header_index, mapping = _find_header(sheet.rows, header_aliases)
    if header_index is None:
        return None
    result = _extract_rows(sheet, header_index, mapping)
    revised_mapping = _hierarchy_mapping(result)
    if revised_mapping != mapping:
        result = _extract_rows(sheet, header_index, revised_mapping)
        result.warnings.append("检测到纵向合并的父级评价项目，已按评分维度解析。")
    return result


def _is_full_row_note(units, mapping) -> bool:
    """一个合并单元格同时占据名称列与分值列：这是表尾“注：……”一类说明行，
    不是评分项（否则会把说明文字当名称、把其中的数字当满分）。"""

    distinct = {unit for unit in units if unit}
    if len(distinct) != 1:
        return False
    columns = [mapping.get(key) for key in ("name", "item_label", "max_score") if mapping.get(key) is not None]
    return len(columns) >= 2 and all(column < len(units) and units[column] for column in columns)


def _claim_sheet(result: TableExtraction, sheet: SheetView, ledger: SourceLedger) -> None:
    fields_by_column = {column: name for name, column in result.mapping.items()}
    for unit_id in sheet.row_units(result.header_index):
        if unit_id:
            ledger.mark(unit_id, "structural", reason="表头")
    for row in result.records:
        for column, unit_id in enumerate(row.unit_ids):
            if unit_id and column in fields_by_column:
                ledger.claim(unit_id, f"{row.criterion.code}.{fields_by_column[column]}")
    if result.total_row:
        for unit_id in sheet.row_units(result.total_row["row_number"] - 1):
            if unit_id:
                ledger.mark(unit_id, "structural", reason="合计行")
    data_rows = len(result.records) + len(result.dropped_rows)
    for column, header in enumerate(result.headers):
        if not header or column in fields_by_column:
            continue
        rows = [row.unit_ids for row in result.records] + [
            sheet.row_units(item["row_number"] - 1) for item in result.dropped_rows
        ]
        non_empty = sum(1 for units in rows if column < len(units) and units[column])
        result.unmapped_columns.append(
            {"column": column + 1, "header": header, "non_empty": non_empty, "data_rows": data_rows}
        )


def extract_table(sheets: list[SheetView], ledger: SourceLedger, *, header_aliases=None) -> TableExtraction:
    """在第一张识别到评分项的工作表上抽取评分项，并登记全部工作表单元的状态。

    选表与告警语义与原 ``pipeline._excel_rows`` 一致：没有表头或没有评分项的工作表
    记告警并跳过，其后的工作表不再解析（登记为 ignored_by_rule，由 E8 提示）。
    """

    warnings: list[str] = []
    chosen = None
    for index, sheet in enumerate(sheets):
        result = _extract_sheet(sheet, header_aliases)
        if result is not None and result.records:
            chosen = (index, sheet, result)
            break
        warnings.append(f"工作表 {sheet.title} 未识别到评分规则表头，已跳过。")
        for unit in _sheet_units(sheet):
            ledger.mark(unit, "ignored_by_rule", reason=NO_HEADER_REASON)
    if chosen is None:
        raise ValueError("Excel 未解析到有效评分项，请确认包含评分项名称和分值列。")
    index, sheet, result = chosen
    result.warnings = [*warnings, *result.warnings]
    _claim_sheet(result, sheet, ledger)
    for later in sheets[index + 1 :]:
        result.ignored_sheets.append({"title": later.title, "reason": IGNORED_SHEET_REASON})
        for unit in _sheet_units(later):
            ledger.mark(unit, "ignored_by_rule", reason=IGNORED_SHEET_REASON)
    return result


def _sheet_units(sheet: SheetView) -> list[str]:
    units = []
    for row_index in range(len(sheet.rows)):
        units.extend(unit for unit in sheet.row_units(row_index) if unit)
    return list(dict.fromkeys(units))
