"""Word 评分项抽取器（解析重构方案 §4 第 2 层、阶段 4）。

优先把 Word 表格当作工作表交给 ``extract_table``（复用同一套表头/行解析）；
没有可用表格时，按“名称（N分）：说明”格式识别段落。标题记为上下文，
“总分 N 分”一类说明记为结构性内容，命中规则信号的其余段落保持 unclaimed。
"""

from __future__ import annotations

import re

from backend.app.services.rubric_import.coverage import unit_signals
from backend.app.services.rubric_import.extraction.table_extractor import ExtractedRow
from backend.app.services.rubric_import.extraction.table_extractor import ImportedCriterion
from backend.app.services.rubric_import.extraction.table_extractor import TableExtraction
from backend.app.services.rubric_import.extraction.table_extractor import extract_table
from backend.app.services.rubric_import.sources.docx_adapter import DocxView
from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.services.rubric_import.sources.units import SourceUnit
from backend.app.services.rubric_import.sources.xlsx_adapter import SheetView

_PARAGRAPH_CRITERION_RE = re.compile(
    r"^(?:(?P<code>[A-Za-z]+\d+)|[一二三四五六七八九十\d]+)?[\s、.．)）]*"
    r"(?P<name>[^（(：:，,]{1,30}?)\s*[（(]\s*(?P<score>\d+(?:\.\d+)?)\s*分\s*[)）]\s*[：:，,]?\s*(?P<desc>.*)$"
)
_TOTAL_STATEMENT_RE = re.compile(r"^(?:本表)?(?:总分|满分|合计)\s*[:：为]?\s*\d+(?:\.\d+)?\s*分")
NO_CRITERIA_MESSAGE = "Word 未解析到有效评分项，请确认包含评分项名称和分值，或使用 AI 识别文档结构。"
PARAGRAPH_HEADERS = ["编号", "评分项", "分值", "评分说明"]


def table_sheets(view: DocxView) -> list[SheetView]:
    sheets = []
    for index, grid in enumerate(view.tables):
        sheet = SheetView(title=f"表格{index + 1}")
        for row_index, cells in enumerate(grid):
            sheet.rows.append(tuple(text if text else None for _, text in cells))
            for col_index, (unit_id, _) in enumerate(cells):
                if unit_id:
                    sheet._units[(row_index, col_index)] = unit_id
        sheets.append(sheet)
    return sheets


def _paragraph_extraction(view: DocxView, ledger: SourceLedger) -> TableExtraction | None:
    result = TableExtraction(sheet_title="正文", header_index=-1, mapping={}, headers=list(PARAGRAPH_HEADERS))
    for block in view.blocks:
        if block["type"] != "paragraph":
            continue
        match = _PARAGRAPH_CRITERION_RE.match(block["text"])
        if not match:
            continue
        order = len(result.records) + 1
        criterion = ImportedCriterion(
            code=match.group("code") or "C%02d" % order,
            name=match.group("name").strip(),
            max_score=float(match.group("score")),
            weight=None,
            description=match.group("desc").strip() or None,
            evidence_hints=[],
            deduction_rules=[],
            display_order=order,
        )
        values = (criterion.code, criterion.name, criterion.max_score, criterion.description)
        unit_id = block["unit_id"]
        result.records.append(ExtractedRow(order, values, [unit_id] * 4, criterion))
        ledger.claim(unit_id, f"{criterion.code}.paragraph")
    return result if result.records else None


def mark_remaining_blocks(view: DocxView, ledger: SourceLedger, *, profile_terms=(), keep_unclaimed=()) -> None:
    """标题 → context；“总分 N 分” → structural；无规则信号的正文与表格单元 → context。"""

    keep = set(keep_unclaimed)
    doc_id = _doc_id(view, ledger)
    for unit in ledger.units(doc_id=doc_id):
        if unit.kind == "comment" or unit.unit_id in keep:
            continue
        if ledger.status(unit.unit_id).status != "unclaimed":
            continue
        if unit.kind == "heading":
            ledger.mark(unit.unit_id, "context")
        elif _TOTAL_STATEMENT_RE.match(unit.text):
            ledger.mark(unit.unit_id, "structural", reason="总分说明")
        elif not unit_signals(unit, profile_terms):
            ledger.mark(unit.unit_id, "context")


def _doc_id(view: DocxView, ledger: SourceLedger) -> str | None:
    unit_id = next((b["unit_id"] for b in view.blocks if b.get("unit_id")), None)
    if unit_id is None:
        for grid in view.tables:
            for cells in grid:
                unit_id = next((u for u, _ in cells if u), None)
                if unit_id:
                    break
            if unit_id:
                break
    return ledger.unit(unit_id).doc_id if unit_id else None


def extract_docx_rules(view: DocxView, ledger: SourceLedger, *, header_aliases=None, profile_terms=()) -> TableExtraction:
    """Word 作为规则文档：表格优先，其次段落；都没有时抛 ValueError（E7）。"""

    extraction = None
    sheets = table_sheets(view)
    if sheets:
        try:
            extraction = extract_table(sheets, ledger, header_aliases=header_aliases)
        except ValueError:
            extraction = None
    if extraction is None:
        extraction = _paragraph_extraction(view, ledger)
    if extraction is None:
        raise ValueError(NO_CRITERIA_MESSAGE)
    mark_remaining_blocks(view, ledger, profile_terms=profile_terms)
    return extraction


def candidate_criteria(view: DocxView, *, header_aliases=None) -> list[ExtractedRow]:
    """双文件时从 Word 模板中找评分项候选，用于与 Excel 比对；不改变主台账。"""

    scratch = SourceLedger()
    for sheet in table_sheets(view):
        for row_index in range(len(sheet.rows)):
            for unit_id in sheet.row_units(row_index):
                if unit_id and not scratch.has(unit_id):
                    scratch.register(SourceUnit(unit_id, "word", "template", "table_cell", "x", {}))
    for block in view.blocks:
        if block.get("unit_id") and not scratch.has(block["unit_id"]):
            scratch.register(SourceUnit(block["unit_id"], "word", "template", "paragraph", "x", {}))
    try:
        return extract_docx_rules(view, scratch, header_aliases=header_aliases).records
    except ValueError:
        return []
