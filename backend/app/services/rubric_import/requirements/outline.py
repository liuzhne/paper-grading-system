"""识别 Word 模板的章节（docs/Word评分规范抽取与对应方案.md 第 5 节）。纯函数，不用 AI。

按“标题样式 > 大纲级别 > 文字序号 > 整段加粗”判断，先命中的算数。结果单独存放，
不改单元的 ``kind`` 和 ``heading_path``：``_paragraph_extraction`` 要把“一、需求分析（10分）：说明”
当成段落抽评分项，改成标题会破坏现有抽取。章节错了只影响上下文和显示，不影响原文和门禁。
"""

from __future__ import annotations

import re

from backend.app.services.rubric_import.classification.signals import rule_signals
from backend.app.services.rubric_import.sources.docx_adapter import _STYLE_LEVEL_RE

OUTLINE_VERSION = 1
MAX_TEXT_HEADING = 40
MAX_BOLD_HEADING = 30

_CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百\d]+(章|篇|部分|节)")
_KEYWORD_RE = re.compile(r"^(摘要|引言|绪论|结论|总结|参考文献|致谢)$")
_CN_L1_RE = re.compile(r"^[一二三四五六七八九十]+[、．.]")
_CN_L2_RE = re.compile(r"^[（(][一二三四五六七八九十]+[)）]")
_MULTI_RE = re.compile(r"^(\d+(?:\.\d+)+)(?![\d.])")
_DECIMAL_SPACE_RE = re.compile(r"^\d+\s+(?![个天日年月周人次项条页份小分秒])\S")  # “5 个工作日内回复”不是标题
_DECIMAL_RE = re.compile(r"^\d+[、．.)）](?!\d)")
_PAREN_DECIMAL_RE = re.compile(r"^[（(]\d+[)）]")
_CIRCLED_RE = re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩]")
_LETTER_RE = re.compile(r"^[a-zA-Z][、.)）]")
_BULLET_RE = re.compile(r"^[•·▪◦\-–—]\s*\S")
_LEAD_RE = re.compile(r"[：:]$|如下|以下|下列")
_END_PUNCT_RE = re.compile(r"[。；;！!？?，,：:]$")

_MARKERS = (
    ("cn1", _CN_L1_RE),
    ("cn2", _CN_L2_RE),
    ("multi", _MULTI_RE),
    ("decimal", _DECIMAL_RE),
    ("paren", _PAREN_DECIMAL_RE),
    ("circled", _CIRCLED_RE),
    ("letter", _LETTER_RE),
    ("bullet", _BULLET_RE),
)


def marker(text: str) -> str | None:
    for name, pattern in _MARKERS:
        if pattern.match(text):
            return name
    return None


def is_lead(text: str) -> bool:
    return bool(_LEAD_RE.search(text))


def _text_level(text: str) -> int | None:
    """不依赖上下文的文字序号。"""

    chapter = _CHAPTER_RE.match(text)
    if chapter:
        return 2 if chapter.group(1) == "节" else 1
    kind = marker(text)
    if kind == "cn1":
        return 1
    if kind == "multi":
        return _MULTI_RE.match(text).group(1).count(".") + 1
    if _DECIMAL_SPACE_RE.match(text):
        return 1
    return None


def _heading(block: dict, previous: dict | None, following: dict | None) -> tuple[int, str] | None:
    text, style = block["text"], block["style"]
    if style in ("Title", "标题"):
        return 0, "style"
    match = _STYLE_LEVEL_RE.search(style)
    if match:
        return int(match.group(1)), "style"
    if block["outline_level"] is not None:
        return block["outline_level"], "outline"
    # 下面都是猜的，所以不能吞掉规则：带分值的留作正文，门禁才看得到。
    if block["numbering"] or len(text) > MAX_TEXT_HEADING or _END_PUNCT_RE.search(text):
        return None
    if "score" in rule_signals(text):
        return None
    level = _text_level(text)
    if level is not None:
        return level, "text_number"
    if _KEYWORD_RE.match(text):
        return 1, "keyword"
    after_lead = previous is not None and is_lead(previous["text"])
    kind = marker(text)
    if kind == "cn2":
        return None if after_lead or _same_marker(previous, kind) else (2, "text_number")
    if kind == "decimal":
        if after_lead or following is None:
            return None
        if _same_marker(previous, kind) or _same_marker(following, kind):
            return None
        return 3, "text_number"
    if kind is None and block["bold"] and len(text) <= MAX_BOLD_HEADING and following is not None:
        if following["type"] == "table" or not following["bold"]:
            return 4, "bold"
    return None


def _same_marker(block: dict | None, kind: str) -> bool:
    return block is not None and block["type"] == "paragraph" and marker(block["text"]) == kind


def _is_body_paragraph(block: dict) -> bool:
    return block["type"] == "paragraph" and not block["toc"] and not block["text_box"]


def _following(blocks: list[dict], index: int) -> dict | None:
    for position in range(index + 1, len(blocks)):
        block = blocks[position]
        if block["type"] == "table" or _is_body_paragraph(block):
            return block
    return None


def build_outline(blocks: list[dict]) -> dict:
    nodes: list[dict] = []
    stack: list[dict] = []
    previous = None
    for index, block in enumerate(blocks):
        if block["type"] == "table":
            previous = None
            continue
        if not _is_body_paragraph(block):
            continue
        heading = _heading(block, previous, _following(blocks, index))
        if heading is None:
            previous = block
            continue
        level, source = heading
        while stack and stack[-1]["level"] >= level:
            stack.pop()
        node = {
            "section_id": f"S{len(nodes) + 1}",
            "parent_id": stack[-1]["section_id"] if stack else None,
            "level": level,
            "title": block["text"],
            "unit_id": block["unit_id"],
            "source": source,
        }
        nodes.append(node)
        stack.append(node)
        previous = None
    return {"version": OUTLINE_VERSION, "nodes": nodes}


def section_path(outline: dict, section_id: str | None) -> list[str]:
    by_id = {node["section_id"]: node for node in outline.get("nodes") or []}
    path: list[str] = []
    while section_id in by_id:
        path.insert(0, by_id[section_id]["title"])
        section_id = by_id[section_id]["parent_id"]
    return path
