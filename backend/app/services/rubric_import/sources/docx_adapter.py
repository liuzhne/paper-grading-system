"""Word → 原文单元（解析重构方案 §4 第 1 层）。只读取，不做任何语义判断。

按文档正文顺序登记段落、标题与表格单元格，并附标题路径；批注复用
``docx_comments.parse_comments`` 的 OOXML 解析，附锚定正文与所在章节。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from io import BytesIO

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from backend.app.services.rubric_import.docx_comments import parse_comments
from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.services.rubric_import.sources.units import SourceUnit

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEADING_RE = re.compile(r"^(第[一二三四五六七八九十\d]+[章节]|\d+(\.\d+)*\s|摘要|引言|绪论|结论|总结|参考文献|致谢)")
_STYLE_LEVEL_RE = re.compile(r"(?:Heading|标题)\s*(\d+)")


@dataclass
class DocxView:
    blocks: list[dict] = field(default_factory=list)
    tables: list[list[list[tuple[str | None, str]]]] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)


def _heading_level(paragraph: Paragraph, text: str) -> int | None:
    style = getattr(paragraph.style, "name", "") or ""
    if style == "Title":
        return 0
    match = _STYLE_LEVEL_RE.search(style)
    if match:
        return int(match.group(1))
    if len(text) <= 40 and _HEADING_RE.match(text):
        if re.match(r"^第.+节", text):
            return 3
        numbered = re.match(r"^(\d+(?:\.\d+)*)\s", text)
        if numbered:
            return numbered.group(1).count(".") + 2
        return 2
    return None


def load_docx(data: bytes, ledger: SourceLedger, *, doc_id: str, doc_role: str) -> DocxView:
    try:
        document = Document(BytesIO(data))
    except Exception as exc:  # python-docx 对非法文件抛多种异常
        raise ValueError("无法读取 Word 文件，请确认为 .docx 格式") from exc

    view = DocxView()
    path: list[tuple[int, str]] = []
    paragraph_index = table_index = 0
    for element in document.element.body.iterchildren():
        if element.tag == f"{_W_NS}p":
            paragraph = Paragraph(element, document)
            text = paragraph.text.strip()
            unit_id = f"docx:p[{paragraph_index}]"
            paragraph_index += 1
            if not text:
                continue
            level = _heading_level(paragraph, text)
            if level is not None:
                path = [item for item in path if item[0] < level]
                context = {"heading_path": [t for _, t in path], "level": level}
                path.append((level, text))
                kind = "heading"
            else:
                context = {"heading_path": [t for _, t in path]}
                kind = "paragraph"
            ledger.register(SourceUnit(unit_id, doc_id, doc_role, kind, text, context))
            view.blocks.append({"type": kind, "unit_id": unit_id, "text": text, "level": level})
        elif element.tag == f"{_W_NS}tbl":
            table = Table(element, document)
            # 以 lxml 元素本身为键：字典持有引用，身份稳定；id() 会因代理对象回收而复用。
            seen: dict[object, str] = {}
            grid = []
            for row_index, row in enumerate(table.rows):
                cells = []
                for col_index, cell in enumerate(row.cells):
                    key = cell._tc
                    text = cell.text.strip()
                    if key in seen:
                        cells.append((seen[key], text))
                        continue
                    unit_id = f"docx:tbl[{table_index}]/r{row_index}/c{col_index}"
                    if text:
                        seen[key] = unit_id
                        ledger.register(
                            SourceUnit(unit_id, doc_id, doc_role, "table_cell", text,
                                       {"heading_path": [t for _, t in path], "table": table_index,
                                        "row": row_index, "col": col_index})
                        )
                        cells.append((unit_id, text))
                    else:
                        cells.append((None, ""))
                grid.append(cells)
            view.tables.append(grid)
            view.blocks.append({"type": "table", "index": table_index})
            table_index += 1

    for comment in parse_comments(data):
        text = str(comment.get("comment_text") or "").strip()
        if not text:
            continue
        unit_id = f"docx:comment[{comment.get('comment_id')}]"
        context = {
            "anchor_text": comment.get("anchor_text") or "",
            "section_title": comment.get("section_title") or "",
            "author": comment.get("author"),
        }
        ledger.register(SourceUnit(unit_id, doc_id, doc_role, "comment", text, context))
        view.comments.append({"unit_id": unit_id, **comment})
    return view
