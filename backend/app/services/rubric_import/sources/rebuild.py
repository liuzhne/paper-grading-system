"""从台账重建原文结构（解析重构方案阶段 6）。

导入时只保存文件哈希，不保存文件本身；台账里有全部非空单元及其位置，足以把
Excel 工作表与 Word 段落/表格重建出来，供“按确认后的结构重新解析”使用。
已知局限：Word 表格的合并跨度与空单元格不在台账中，重建后以空值表示。
"""

from __future__ import annotations

import re

from openpyxl.utils import range_boundaries

from backend.app.services.rubric_import.sources.docx_adapter import DocxView
from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.services.rubric_import.sources.xlsx_adapter import SheetView

_TABLE_CELL_RE = re.compile(r"^docx:tbl\[(\d+)\]/r(\d+)/c(\d+)$")
_PARAGRAPH_RE = re.compile(r"^docx:p\[(\d+)\]$")


def sheets_from_ledger(ledger: SourceLedger, *, doc_id: str) -> list[SheetView]:
    grids: dict[str, dict[tuple[int, int], tuple[str, str]]] = {}
    for unit in ledger.units(doc_id=doc_id):
        if unit.kind != "cell":
            continue
        context = unit.context
        cells = grids.setdefault(context["sheet"], {})
        if context.get("merged"):
            min_col, min_row, max_col, max_row = range_boundaries(context["merged"])
            for row in range(min_row, max_row + 1):
                for column in range(min_col, max_col + 1):
                    cells[(row, column)] = (unit.unit_id, unit.text)
        else:
            cells[(context["row"], context["col"])] = (unit.unit_id, unit.text)
    sheets = []
    for title, cells in grids.items():
        height = max(row for row, _ in cells)
        width = max(column for _, column in cells)
        view = SheetView(title=title)
        for row in range(1, height + 1):
            values = []
            for column in range(1, width + 1):
                entry = cells.get((row, column))
                values.append(entry[1] if entry else None)
                if entry:
                    view._units[(row - 1, column - 1)] = entry[0]
            view.rows.append(tuple(values))
        sheets.append(view)
    return sheets


def docx_view_from_ledger(ledger: SourceLedger, *, doc_id: str) -> DocxView:
    view = DocxView()
    ordered: list[tuple[int, int, dict]] = []
    tables: dict[int, dict[tuple[int, int], tuple[str, str]]] = {}
    for unit in ledger.units(doc_id=doc_id):
        paragraph = _PARAGRAPH_RE.match(unit.unit_id)
        cell = _TABLE_CELL_RE.match(unit.unit_id)
        if paragraph:
            block = {"type": unit.kind, "unit_id": unit.unit_id, "text": unit.text,
                     "level": unit.context.get("level")}
            ordered.append((int(paragraph.group(1)), 0, block))
        elif cell:
            table, row, column = (int(value) for value in cell.groups())
            tables.setdefault(table, {})[(row, column)] = (unit.unit_id, unit.text)
        elif unit.kind == "comment":
            view.comments.append(
                {
                    "unit_id": unit.unit_id,
                    "comment_id": unit.unit_id[len("docx:comment[") : -1],
                    "author": unit.context.get("author"),
                    "comment_text": unit.text,
                    "anchor_text": unit.context.get("anchor_text", ""),
                    "section_title": unit.context.get("section_title", ""),
                }
            )
    view.blocks = [block for _, _, block in sorted(ordered, key=lambda item: item[:2])]
    for index in sorted(tables):
        cells = tables[index]
        height = max(row for row, _ in cells) + 1
        width = max(column for _, column in cells) + 1
        view.tables.append(
            [[cells.get((row, column), (None, "")) for column in range(width)] for row in range(height)]
        )
    return view
