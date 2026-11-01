"""Word 正文的深度读取工具（docs/Word评分规范抽取与对应方案.md 第 4 节）。只读取，不做语义判断。

python-docx 的 ``paragraph.text`` 只看段落直属的 ``w:r`` 与 ``w:hyperlink``，读不到修订插入、
行内内容控件和文本框；Word 又会把文本框在 ``mc:Choice`` 和 ``mc:Fallback`` 里各写一份。
这里统一处理这些差异，并统计读取是否完整，以后再有读不到的结构也不会静默丢失。
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from io import BytesIO

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_M_T = "{http://schemas.openxmlformats.org/officeDocument/2006/math}t"
_A_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_V = "{urn:schemas-microsoft-com:vml}"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_C_CHART = "{http://schemas.openxmlformats.org/drawingml/2006/chart}chart"
_DGM_RELIDS = "{http://schemas.openxmlformats.org/drawingml/2006/diagram}relIds"

W_P = f"{_W}p"
W_TBL = f"{_W}tbl"
W_SDT = f"{_W}sdt"
_W_R = f"{_W}r"
_W_T = f"{_W}t"
_W_BR = f"{_W}br"
_W_TXBX = f"{_W}txbxContent"
_W_DEL = f"{_W}del"
_W_MOVE_FROM = f"{_W}moveFrom"
_W_COMMENT_START = f"{_W}commentRangeStart"
_W_SDT_CONTENT = f"{_W}sdtContent"
_W_SDT_PR = f"{_W}sdtPr"
_W_GALLERY = f"{_W}docPartGallery"
_W_OBJECT = f"{_W}object"

_RUN_TEXT = {f"{_W}tab": "\t", f"{_W}ptab": "\t", f"{_W}cr": "\n", f"{_W}noBreakHyphen": "-"}
_NOT_TEXT = frozenset({_W_DEL, _W_MOVE_FROM, _W_TXBX, _MC_FALLBACK})
_NOT_READ = frozenset({_W_DEL, _W_MOVE_FROM, _MC_FALLBACK})
_TOC_STYLE_RE = re.compile(r"^(toc|目录)(\s*\d+|\s*heading|\s*标题)?$", re.IGNORECASE)
_TOC_LEADER_RE = re.compile(r"(\.{3,}|…+|·{3,})\s*\d+\s*$")
_UNREAD_PARTS = (
    ("页眉", re.compile(r"^word/header\d*\.xml$")),
    ("页脚", re.compile(r"^word/footer\d*\.xml$")),
    ("脚注", re.compile(r"^word/footnotes\.xml$")),
    ("尾注", re.compile(r"^word/endnotes\.xml$")),
)
_ALT_TEXT_MAX = 200
_STYLE_DEPTH_MAX = 20


def _inside(node, stop, tags) -> bool:
    ancestor = node.getparent()
    while ancestor is not None and ancestor is not stop:
        if ancestor.tag in tags:
            return True
        ancestor = ancestor.getparent()
    return False


def paragraph_text(element) -> str:
    """与 ``paragraph.text`` 的转换规则相同，另外包含修订插入、行内内容控件和公式；
    排除已删除的文字和文本框（文本框单独成单元）。"""

    parts = []
    for node in element.iter(_W_T, _M_T, _W_BR, *_RUN_TEXT):
        if _inside(node, element, _NOT_TEXT):
            continue
        if node.tag in (_W_T, _M_T):
            parts.append(node.text or "")
        elif node.getparent().tag != _W_R:  # 段落属性里的 w:tabs/w:tab 是制表位定义，不是文字
            continue
        elif node.tag == _W_BR:
            parts.append("\n" if node.get(f"{_W}type", "textWrapping") == "textWrapping" else "")
        else:
            parts.append(_RUN_TEXT[node.tag])
    return "".join(parts)


def block_children(parent):
    """容器的段落和表格子元素，穿过块级内容控件。"""

    for child in parent.iterchildren():
        if child.tag in (W_P, W_TBL):
            yield child
        elif child.tag == W_SDT:
            content = child.find(_W_SDT_CONTENT)
            if content is not None:
                yield from block_children(content)


def cell_text(tc) -> str:
    return "\n".join(paragraph_text(child) for child in block_children(tc) if child.tag == W_P)


def comment_ids(element, *, in_text_box: bool = False) -> list[str]:
    """``element`` 内批注锚点的编号；``in_text_box`` 为假时不含文本框里的锚点。"""

    return [
        str(node.get(f"{_W}id"))
        for node in element.iter(_W_COMMENT_START)
        if node.get(f"{_W}id") is not None and (in_text_box or not _inside(node, element, {_W_TXBX}))
    ]


def text_boxes(owner) -> list[dict]:
    """``owner`` 内的文本框。Word 同时写 Choice 和 Fallback，只取前者，否则文字会出现两遍。"""

    boxes = []
    for box in owner.iter(_W_TXBX):
        if _inside(box, owner, {_MC_FALLBACK, _W_TXBX}):
            continue
        text = "\n".join(filter(None, (paragraph_text(item).strip() for item in box.iter(W_P))))
        boxes.append({"text": text, "comment_ids": comment_ids(box, in_text_box=True)})
    return boxes


def _alt_text(node, owner) -> str:
    ancestor = node.getparent()
    while ancestor is not None and ancestor is not owner:
        if ancestor.tag in (f"{_WP}inline", f"{_WP}anchor"):
            properties = ancestor.find(f"{_WP}docPr")
            return (properties.get("descr") or "").strip() if properties is not None else ""
        if ancestor.tag == f"{_V}shape":
            return (ancestor.get("alt") or "").strip()
        ancestor = ancestor.getparent()
    return ""


def images_of(owner, related_parts) -> list[dict]:
    images = []
    for node in owner.iter(_A_BLIP, f"{_V}imagedata"):
        if _inside(node, owner, {_MC_FALLBACK}):
            continue
        relation = node.get(f"{_R}embed") or node.get(f"{_R}link") or node.get(f"{_R}id")
        if not relation:
            continue
        part = related_parts.get(relation)
        image = {"alt": _alt_text(node, owner)[:_ALT_TEXT_MAX], "external": part is None}
        if part is not None:
            image.update(
                sha256=hashlib.sha256(part.blob).hexdigest(),
                content_type=part.content_type,
                size_bytes=len(part.blob),
                part_name=str(part.partname),
            )
        images.append(image)
    return images


def image_label(image: dict) -> str:
    return f"[图片：{image['alt']}]" if image.get("alt") else "[图片]"


def is_toc_gallery(sdt) -> bool:
    properties = sdt.find(_W_SDT_PR)
    gallery = properties.find(f".//{_W_GALLERY}") if properties is not None else None
    return gallery is not None and "table of contents" in (gallery.get(f"{_W}val") or "").lower()


def sdt_blocks(sdt):
    content = sdt.find(_W_SDT_CONTENT)
    return () if content is None else content.iterchildren()


def is_toc_paragraph(style_name: str, text: str) -> bool:
    """目录样式，或手写目录的“标题……页码”。这些段落里的“扣分”不是规则，不能造成阻断。"""

    return bool(_TOC_STYLE_RE.match((style_name or "").strip()) or _TOC_LEADER_RE.search(text))


def _is_on(node) -> bool:
    return node.get(f"{_W}val") not in ("0", "false", "off")


def _style_chain(element):
    """样式和它继承的样式。直接读 XML：python-docx 遇到损坏的 ``basedOn`` 或样式类型会报错。"""

    for _ in range(_STYLE_DEPTH_MAX):  # 损坏的样式表可能循环继承
        if element is None:
            return
        yield element
        based = element.find(f"{_W}basedOn")
        style_id = based.get(f"{_W}val") if based is not None else None
        if not style_id:
            return
        element = next(
            (item for item in element.getparent().iterchildren(f"{_W}style") if item.get(f"{_W}styleId") == style_id),
            None,
        )


def _chain_property(chain, group, tag):
    for element in chain:
        properties = element.find(f"{_W}{group}")
        node = properties.find(f"{_W}{tag}") if properties is not None else None
        if node is not None:
            return node
    return None


def _style_and_name(paragraph) -> tuple:
    try:
        style = paragraph.style
        return style, (style.name or "") if style is not None else ""
    except Exception:  # 损坏的 pStyle、样式名或样式类型，python-docx 抛多种异常；按没有样式处理
        return None, ""


def style_info(paragraph, cache: dict) -> dict:
    """段落样式的名称、加粗、大纲级别和编号，含继承。

    ``paragraph.style`` 很慢：默认样式每次都要遍历整张样式表，3000 段的模板能多花 3 秒。按样式 ID 缓存。
    损坏的引用单独一个键，不能和默认样式（``None``）混用。
    """

    properties = paragraph._p.pPr
    node = properties.find(f"{_W}pStyle") if properties is not None else None
    key = (node.get(f"{_W}val") or "") if node is not None else None
    info = cache.get(key)
    if info is None:
        style, name = _style_and_name(paragraph)
        chain = list(_style_chain(style.element if style is not None else None))
        bold = _chain_property(chain, "rPr", "b")
        info = {
            "name": name,
            "bold": bold is not None and _is_on(bold),
            "outlineLvl": _chain_property(chain, "pPr", "outlineLvl"),
            "numPr": _chain_property(chain, "pPr", "numPr"),
        }
        cache[key] = info
    return info


def _paragraph_property(paragraph, tag, info: dict):
    properties = paragraph._p.pPr
    node = properties.find(f"{_W}{tag}") if properties is not None else None
    return node if node is not None else info[tag]


def outline_level(paragraph, info: dict) -> int | None:
    """段落或样式继承的大纲级别，1 起算；9 表示正文，不算。"""

    node = _paragraph_property(paragraph, "outlineLvl", info)
    if node is None:
        return None
    try:
        value = int(node.get(f"{_W}val"))
    except (TypeError, ValueError):
        return None
    return value + 1 if 0 <= value < 9 else None


def numbering(paragraph, info: dict) -> list | None:
    """自动编号的 ``[numId, ilvl]``；``numId=0`` 表示段落取消了样式带来的编号。"""

    node = _paragraph_property(paragraph, "numPr", info)
    if node is None:
        return None
    number = node.find(f"{_W}numId")
    number_id = number.get(f"{_W}val") if number is not None else None
    if not number_id or number_id == "0":
        return None
    level = node.find(f"{_W}ilvl")
    try:
        return [number_id, int(level.get(f"{_W}val")) if level is not None else 0]
    except (TypeError, ValueError):
        return [number_id, 0]


def table_readable(table) -> bool:
    """python-docx 能否读出每一行。损坏的合并单元格或跨列值会让 ``row.cells`` 报错。"""

    try:
        return all(row.cells is not None for row in table.rows)
    except Exception:  # python-docx 抛多种异常
        return False


def is_bold(element, info: dict) -> bool:
    """整段有文字的 run 都加粗（直接加粗或样式继承）才算。"""

    seen = False
    for run in element.iter(_W_R):
        if _inside(run, element, _NOT_TEXT) or not paragraph_text(run).strip():
            continue
        properties = run.find(f"{_W}rPr")
        bold = properties.find(f"{_W}b") if properties is not None else None
        value = info["bold"] if bold is None else _is_on(bold)
        if not value:
            return False
        seen = True
    return seen


def _chars(text: str) -> Counter:
    return Counter(char for char in text if not char.isspace())


def read_report(data: bytes, body, units) -> dict:
    """比较 ``document.xml`` 里的文字和台账。页眉页脚脚注第一版不读，也在这里统计。"""

    not_read = set()
    for sdt in body.iter(W_SDT):
        if is_toc_gallery(sdt):
            not_read.update(sdt.iter(_W_T, _M_T))
    expected = Counter()
    for node in body.iter(_W_T, _M_T):
        if node not in not_read and not _inside(node, body, _NOT_READ):
            expected.update(_chars(node.text or ""))
    actual = Counter()
    for unit in units:
        if unit.kind not in ("comment", "image"):
            actual.update(_chars(unit.text))
    missing = expected - actual
    return {
        "body_chars": sum(expected.values()),
        "missing_chars": sum(missing.values()),
        "missing_sample": "".join(missing.elements())[:40],
        "unread_parts": _unread_part_chars(data),
        "unread_objects": sum(
            1 for node in body.iter(_C_CHART, _DGM_RELIDS, _W_OBJECT) if not _inside(node, body, {_MC_FALLBACK})
        ),
    }


def _unread_part_chars(data: bytes) -> dict:
    counts: dict[str, int] = {}
    with zipfile.ZipFile(BytesIO(data)) as archive:
        for name in archive.namelist():
            label = next((label for label, pattern in _UNREAD_PARTS if pattern.match(name)), None)
            if label is None:
                continue
            try:
                root = ET.fromstring(archive.read(name))
            except ET.ParseError:
                continue
            total = sum(len("".join((node.text or "").split())) for node in root.iter(_W_T))
            if total:
                counts[label] = counts.get(label, 0) + total
    return counts
