import re
from dataclasses import dataclass, field
from io import BytesIO

from docx import Document
from openpyxl import load_workbook

from backend.app.services.document_parser.format_resolver import resolve_default_format
from backend.app.services.rubric_import.compiler import annotations_for_criterion
from backend.app.services.rubric_import.compiler import compile_criterion_rules
from backend.app.services.rubric_import.docx_comments import parse_comments
from backend.app.services.rubric_import.extraction.table_extractor import HEADER_ALIASES  # noqa: F401
from backend.app.services.rubric_import.extraction.table_extractor import ImportedCriterion  # noqa: F401
from backend.app.services.rubric_import.extraction.table_extractor import _criterion_from_row
from backend.app.services.rubric_import.extraction.table_extractor import _find_header
from backend.app.services.rubric_import.extraction.table_extractor import _normalize
from backend.app.services.rubric_import.extraction.table_extractor import _parse_sub_checks  # noqa: F401


TEMPLATE_HINTS = [
    "中文摘要",
    "英文摘要",
    "关键词",
    "目录",
    "绪论",
    "研究背景",
    "研究意义",
    "文献综述",
    "国内外研究现状",
    "相关工作",
    "研究方法",
    "实验设计",
    "数据来源",
    "结果分析",
    "讨论",
    "创新点",
    "结论",
    "参考文献",
    "致谢",
]


@dataclass
class RubricImport:
    total_score: float
    criteria: list[ImportedCriterion]
    template_summary: dict
    warnings: list[str]
    format_spec: dict = field(default_factory=dict)


def parse_rubric_files(rules_bytes: bytes, template_bytes: bytes | None = None, scorer=None):
    """CLI 离线评分使用的旧导入入口（Web 导入只走 ``pipeline.prepare_file_import``）。"""
    warnings = []
    template_summary = (
        parse_word_template(template_bytes)
        if template_bytes
        else {"section_titles": [], "hints": [], "paragraph_count": 0, "format_spec": {}, "annotations": []}
    )
    criteria, excel_warnings = parse_excel_rules(rules_bytes)
    warnings.extend(excel_warnings)
    enriched = [_enrich_with_template(item, template_summary) for item in criteria]
    # §5 编译：把扣分规则编译成结构化规则（Excel 显式优先，否则小模型归一化批注/自由文本）。
    annotations = template_summary.get("annotations", [])
    for criterion in enriched:
        criterion.deduction_rules_structured = compile_criterion_rules(
            criterion, annotations_for_criterion(criterion, annotations), scorer
        )
    total_score = round(sum(item.max_score for item in enriched), 2)
    return RubricImport(
        total_score=total_score,
        criteria=enriched,
        template_summary=template_summary,
        warnings=warnings,
        format_spec=template_summary.get("format_spec") or {},
    )


def parse_word_template(template_bytes):
    document = Document(BytesIO(template_bytes))
    paragraphs = []
    section_titles = []

    for paragraph in document.paragraphs:
        text = _normalize(paragraph.text)
        if not text:
            continue
        paragraphs.append(text)
        style_name = getattr(paragraph.style, "name", "") or ""
        if _looks_like_template_title(text, style_name):
            section_titles.append(text)

    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                text = _normalize(cell.text)
                if not text:
                    continue
                paragraphs.append(text)
                if _looks_like_template_title(text, ""):
                    section_titles.append(text)

    all_text = "\n".join(paragraphs)
    hints = [hint for hint in TEMPLATE_HINTS if hint in all_text]
    for title in section_titles:
        for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", title):
            if token not in hints and len(token) <= 12:
                hints.append(token)

    return {
        "section_titles": _dedupe(section_titles)[:30],
        "hints": _dedupe(hints)[:40],
        "paragraph_count": len(paragraphs),
        "format_spec": resolve_default_format(template_bytes),
        "annotations": parse_comments(template_bytes),
    }


def parse_excel_rules(rules_bytes):
    workbook = load_workbook(BytesIO(rules_bytes), data_only=True)
    warnings = []
    criteria = []

    for sheet in workbook.worksheets:
        rows = _rows_with_merged_values(sheet)
        header_index, mapping = _find_header(rows)
        if header_index is None:
            warnings.append("工作表 %s 未识别到评分规则表头，已跳过。" % sheet.title)
            continue
        for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
            criterion = _criterion_from_row(row, mapping, len(criteria) + 1)
            if criterion is None:
                continue
            criteria.append(criterion)
        if criteria:
            break

    if not criteria:
        raise ValueError("Excel 未解析到有效评分项，请确认包含评分项名称和分值列。")
    return criteria, warnings


def _rows_with_merged_values(sheet):
    """Return worksheet rows while expanding merged-cell top-left values.

    ``openpyxl`` exposes the non-anchor cells in a merged range as ``None``.
    Evaluation templates commonly merge a category name across several score
    items, so leaving those cells empty silently drops all but the first item.
    """

    merged_values = {}
    for merged_range in sheet.merged_cells.ranges:
        value = sheet.cell(merged_range.min_row, merged_range.min_col).value
        for row in range(merged_range.min_row, merged_range.max_row + 1):
            for column in range(merged_range.min_col, merged_range.max_col + 1):
                merged_values[(row, column)] = value
    return [
        tuple(
            merged_values.get((cell.row, cell.column), cell.value)
            for cell in row
        )
        for row in sheet.iter_rows()
    ]


def _enrich_with_template(criterion, template_summary):
    hints = list(criterion.evidence_hints)
    matched = _match_template_hints(criterion, template_summary.get("hints", []))
    for hint in matched:
        if hint not in hints:
            hints.append(hint)

    if matched:
        template_note = "Word 模板解析提示：建议重点查看 %s。" % "、".join(matched[:6])
        description = "%s\n%s" % (criterion.description, template_note) if criterion.description else template_note
    else:
        description = criterion.description

    return ImportedCriterion(
        code=criterion.code,
        name=criterion.name,
        max_score=criterion.max_score,
        weight=criterion.weight,
        description=description,
        evidence_hints=hints,
        deduction_rules=criterion.deduction_rules,
        display_order=criterion.display_order,
        criterion_type=criterion.criterion_type,
        scoring_mode=criterion.scoring_mode,
        applies_to=criterion.applies_to,
        rubric_levels=criterion.rubric_levels,
        sub_checks=criterion.sub_checks,
        dimension=criterion.dimension,
        deduction_rules_structured=criterion.deduction_rules_structured,
    )


def _match_template_hints(criterion, template_hints):
    text = "%s %s %s" % (criterion.name, criterion.description or "", " ".join(criterion.evidence_hints))
    matched = []
    for hint in template_hints:
        if hint in text:
            matched.append(hint)
    rules = [
        ("文献", ["文献综述", "国内外研究现状", "相关工作"]),
        ("方法", ["研究方法", "实验设计", "数据来源"]),
        ("创新", ["创新点"]),
        ("写作", ["中文摘要", "英文摘要", "关键词", "目录", "结论"]),
        ("规范", ["中文摘要", "英文摘要", "关键词", "目录", "结论"]),
        ("参考文献", ["参考文献"]),
        ("选题", ["绪论", "研究背景", "研究意义"]),
        ("意义", ["绪论", "研究背景", "研究意义"]),
        ("论证", ["结果分析", "讨论", "结论"]),
        ("分析", ["结果分析", "讨论"]),
    ]
    for keyword, hints in rules:
        if keyword in text:
            for hint in hints:
                if hint in template_hints and hint not in matched:
                    matched.append(hint)
    return matched[:8]


def _looks_like_template_title(text, style_name):
    if "Heading" in style_name or "标题" in style_name:
        return True
    if len(text) > 40:
        return False
    if re.match(r"^(第[一二三四五六七八九十\d]+[章节]|[一二三四五六七八九十\d]+[、.．])", text):
        return True
    return any(hint == text or hint in text for hint in TEMPLATE_HINTS)


def _dedupe(items):
    result = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result
