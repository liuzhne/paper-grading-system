"""Word → 原文单元（解析重构方案 §4 第 1 层）。只读取，不做任何语义判断。

按文档正文顺序登记段落、标题与表格单元格，并附标题路径；批注复用
``docx_comments.parse_comments`` 的 OOXML 解析，附锚定正文与所在章节。

``deep=True``（只用于模板文档）时补读文本框、内容控件、嵌套表格、修订插入和图片
（docs/Word评分规范抽取与对应方案.md 第 4 节）。已有单元的 ID、kind 和 heading_path 不变，
新内容和版式信息放进 ``view.structure``，不进 ``view.blocks`` 与 ``view.tables``，
否则表格编号和 Word/Excel 比对都会受影响。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from io import BytesIO

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from backend.app.services.rubric_import.docx_comments import parse_comments
from backend.app.services.rubric_import.sources import docx_deep
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
    # 以下只在 deep=True 时有值。
    structure: list[dict] = field(default_factory=list)
    read_report: dict | None = None
    doc_id: str | None = None


def _heading_level(paragraph: Paragraph, text: str, style: str | None = None) -> int | None:
    if style is None:
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


class _Reader:
    """一次读取的上下文。段落和表格的 unit_id 算法只有这一份，``deep`` 只决定多读什么。"""

    def __init__(self, document, ledger: SourceLedger, *, doc_id: str, doc_role: str, deep: bool):
        self.document = document
        self.ledger = ledger
        self.doc_id = doc_id
        self.doc_role = doc_role
        self.deep = deep
        self.related_parts = document.part.related_parts if deep else {}
        self.styles: dict = {}

    def register(self, unit_id: str, kind: str, text: str, context: dict) -> None:
        self.ledger.register(SourceUnit(unit_id, self.doc_id, self.doc_role, kind, text, context))

    def attachments(self, owner, owner_id: str, heading_path: list[str], context: dict | None = None):
        """``owner`` 里的图片和文本框登记成单元，返回 (图片单元 ID, 文本框块)。"""

        extra = {"heading_path": heading_path, **(context or {})}
        image_ids = []
        for index, image in enumerate(docx_deep.images_of(owner, self.related_parts)):
            unit_id = f"{owner_id}/img[{index}]"
            self.register(unit_id, "image", docx_deep.image_label(image), {**extra, "image": image})
            image_ids.append(unit_id)
        boxes = []
        for index, box in enumerate(docx_deep.text_boxes(owner)):
            if not box["text"]:
                continue
            unit_id = f"{owner_id}/txbx[{index}]"
            self.register(unit_id, "paragraph", box["text"], extra)
            boxes.append({"unit_id": unit_id, "text": box["text"], "comment_ids": box["comment_ids"]})
        return image_ids, boxes

    def paragraph_structure(self, element, paragraph, unit_id: str, text: str, heading_path: list[str], *, toc=False):
        comment_ids = docx_deep.comment_ids(element)
        image_ids, boxes = self.attachments(element, unit_id, heading_path)
        blocks = []
        if text:
            info = docx_deep.style_info(paragraph, self.styles)
            blocks.append(
                {
                    "type": "paragraph",
                    "unit_id": unit_id,
                    "text": text,
                    "style": info["name"],
                    "outline_level": docx_deep.outline_level(paragraph, info),
                    "numbering": docx_deep.numbering(paragraph, info),
                    "bold": docx_deep.is_bold(element, info),
                    "toc": toc,
                    "text_box": False,
                    "comment_ids": comment_ids,
                    "image_unit_ids": image_ids,
                }
            )
        else:
            # 纯图片段没有段落单元，批注跟第一张图走；连图片都没有的空段上的批注之后单独成条。
            for index, image_id in enumerate(image_ids):
                blocks.append(
                    {"type": "image", "unit_id": image_id, "comment_ids": comment_ids if index == 0 else []}
                )
        for box in boxes:
            blocks.append(
                {
                    "type": "paragraph",
                    "unit_id": box["unit_id"],
                    "text": box["text"],
                    "style": "",
                    "outline_level": None,
                    "numbering": None,
                    "bold": False,
                    "toc": False,
                    "text_box": True,
                    "comment_ids": box["comment_ids"],
                    "image_unit_ids": [],
                }
            )
        return blocks

    def table(self, element, table_id: str, table_ref, heading_path: list[str]):
        """读一张表，返回 (``view.tables`` 用的网格, ``view.structure`` 用的表格块)。"""

        table = Table(element, self.document)
        # 以 lxml 元素本身为键：字典持有引用，身份稳定；id() 会因代理对象回收而复用。
        seen: dict[object, str] = {}
        owner_row: dict[object, int] = {}
        grid = []
        rows = []
        for row_index, row in enumerate(table.rows):
            cells = []
            structure_cells = []
            for col_index, cell in enumerate(row.cells):
                key = cell._tc
                text = (docx_deep.cell_text(key) if self.deep else cell.text).strip()
                if key in seen:
                    cells.append((seen[key], text))
                    if owner_row[key] != row_index:
                        structure_cells.append(
                            {"unit_id": seen[key], "text": text, "shared": True, "col": col_index}
                        )
                    continue
                unit_id = f"{table_id}/r{row_index}/c{col_index}"
                location = {"table": table_ref, "row": row_index, "col": col_index}
                visited = key in owner_row
                if not visited:
                    owner_row[key] = row_index
                if text:
                    seen[key] = unit_id
                    self.register(unit_id, "table_cell", text, {"heading_path": heading_path, **location})
                    cells.append((unit_id, text))
                else:
                    cells.append((None, ""))
                if self.deep and not visited:
                    structure_cells.append(self.cell_structure(key, unit_id, text, heading_path, location))
            grid.append(cells)
            rows.append(structure_cells)
        return grid, {"type": "table", "table_id": table_id, "rows": rows}

    def deep_table(self, element, table_id: str, heading_path: list[str]) -> list[dict]:
        """只有深度读取才碰到的表（嵌套、内容控件里）。

        读不了就整张跳过，文字由读取报告提示：打开开关不能让关闭时能导入的文件导入失败。
        """

        if not docx_deep.table_readable(Table(element, self.document)):
            return []
        return [self.table(element, table_id, table_id, heading_path)[1]]

    def cell_structure(self, tc, cell_id: str, text: str, heading_path: list[str], location: dict) -> dict:
        comment_ids = []
        image_ids = []
        boxes = []
        nested = []
        paragraphs = tables = 0
        for child in docx_deep.block_children(tc):
            if child.tag == docx_deep.W_P:
                comment_ids.extend(docx_deep.comment_ids(child))
                owner_id = f"{cell_id}/p[{paragraphs}]"
                paragraphs += 1
                new_images, new_boxes = self.attachments(child, owner_id, heading_path, location)
                image_ids.extend(new_images)
                boxes.extend(new_boxes)
            else:
                nested.extend(self.deep_table(child, f"{cell_id}/tbl[{tables}]", heading_path))
                tables += 1
        return {
            "unit_id": cell_id if text else None,
            "text": text,
            "shared": False,
            "col": location["col"],
            "comment_ids": comment_ids,
            "image_unit_ids": image_ids,
            "text_boxes": boxes,
            "tables": nested,
        }

    def sdt_structure(self, element, prefix: str, heading_path: list[str]) -> list[dict]:
        if docx_deep.is_toc_gallery(element):
            return []
        blocks = []
        paragraphs = tables = controls = 0
        for child in docx_deep.sdt_blocks(element):
            if child.tag == docx_deep.W_P:
                unit_id = f"{prefix}/p[{paragraphs}]"
                paragraphs += 1
                paragraph = Paragraph(child, self.document)
                text = docx_deep.paragraph_text(child).strip()
                toc = docx_deep.is_toc_paragraph(docx_deep.style_info(paragraph, self.styles)["name"], text)
                if text:
                    self.register(unit_id, "paragraph", text, _unit_context(heading_path, toc))
                blocks.extend(self.paragraph_structure(child, paragraph, unit_id, text, heading_path, toc=toc))
            elif child.tag == docx_deep.W_TBL:
                blocks.extend(self.deep_table(child, f"{prefix}/tbl[{tables}]", heading_path))
                tables += 1
            elif child.tag == docx_deep.W_SDT:
                blocks.extend(self.sdt_structure(child, f"{prefix}/sdt[{controls}]", heading_path))
                controls += 1
        return blocks


def _unit_context(heading_path: list[str], toc: bool, **extra) -> dict:
    context = {"heading_path": heading_path, **extra}
    if toc:
        context["toc"] = True
    return context


def load_docx(data: bytes, ledger: SourceLedger, *, doc_id: str, doc_role: str, deep: bool = False) -> DocxView:
    try:
        document = Document(BytesIO(data))
    except Exception as exc:  # python-docx 对非法文件抛多种异常
        raise ValueError("无法读取 Word 文件，请确认为 .docx 格式") from exc

    view = DocxView()
    reader = _Reader(document, ledger, doc_id=doc_id, doc_role=doc_role, deep=deep)
    path: list[tuple[int, str]] = []
    paragraph_index = table_index = sdt_index = 0
    for element in document.element.body.iterchildren():
        if element.tag == f"{_W_NS}p":
            paragraph = Paragraph(element, document)
            text = (docx_deep.paragraph_text(element) if deep else paragraph.text).strip()
            unit_id = f"docx:p[{paragraph_index}]"
            paragraph_index += 1
            style = docx_deep.style_info(paragraph, reader.styles)["name"] if deep else None
            toc = deep and docx_deep.is_toc_paragraph(style, text)
            if text:
                level = _heading_level(paragraph, text, style)
                if level is not None:
                    path = [item for item in path if item[0] < level]
                    context = _unit_context([t for _, t in path], toc, level=level)
                    path.append((level, text))
                    kind = "heading"
                else:
                    context = _unit_context([t for _, t in path], toc)
                    kind = "paragraph"
                reader.register(unit_id, kind, text, context)
                if not toc:  # 目录行不能被当成评分项候选，否则“一、需求（10分）……1”会造成假冲突
                    view.blocks.append({"type": kind, "unit_id": unit_id, "text": text, "level": level})
            if deep:
                view.structure.extend(
                    reader.paragraph_structure(element, paragraph, unit_id, text, [t for _, t in path], toc=toc)
                )
        elif element.tag == f"{_W_NS}tbl":
            grid, structure = reader.table(
                element, f"docx:tbl[{table_index}]", table_index, [t for _, t in path]
            )
            view.tables.append(grid)
            view.blocks.append({"type": "table", "index": table_index})
            if deep:
                view.structure.append(structure)
            table_index += 1
        elif deep and element.tag == f"{_W_NS}sdt":
            view.structure.extend(reader.sdt_structure(element, f"docx:sdt[{sdt_index}]", [t for _, t in path]))
            sdt_index += 1

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
    if deep:
        view.doc_id = doc_id
        view.read_report = docx_deep.read_report(data, document.element.body, ledger.units(doc_id=doc_id))
    return view
