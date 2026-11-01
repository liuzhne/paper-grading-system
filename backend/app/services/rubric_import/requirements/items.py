"""组评分规范候选条目（docs/Word评分规范抽取与对应方案.md 第 6 节）。纯函数，不用 AI。

条目只存有序的单元 ID，不复制文字，显示和送模型时都从台账取，所以原文不可能被改写。
每个 Word 单元只能属于一处：某条的 ``unit_ids`` / ``comment_unit_ids`` / ``image_unit_ids``，
或者章节标题，或者表头，或者带理由的排除项（测试保证）。
"""

from __future__ import annotations

import hashlib
import re

from backend.app.services.rubric_import.classification.signals import is_noise
from backend.app.services.rubric_import.classification.signals import rule_signals
from backend.app.services.rubric_import.extraction.docx_extractor import is_total_statement
from backend.app.services.rubric_import.requirements.outline import is_lead
from backend.app.services.rubric_import.requirements.outline import marker

ITEMS_VERSION = 1
MAX_LIST_ENTRIES = 8  # 超过时每项单独成条，否则一条会同时对应太多评分项
REASON_TOC = "目录"
REASON_NOISE = "噪声"
REASON_TOTAL = "总分说明"

_CAPTION_RE = re.compile(r"^(图|Figure|Fig\.?)\s*\d", re.IGNORECASE)
_LIST_MARKERS = frozenset({"cn1", "cn2", "decimal", "paren", "circled", "letter", "bullet"})


def _has_rule_signal(text: str) -> bool:
    return bool(rule_signals(text.replace("需求", "")))  # “需求分析”里的“需”不是规范用语


def _is_list_entry(block: dict) -> bool:
    return bool(block["numbering"]) or marker(block["text"]) in _LIST_MARKERS


def _same_list(first: dict, block: dict) -> bool:
    if first["numbering"]:
        return bool(block["numbering"]) and block["numbering"][0] == first["numbering"][0]
    return not block["numbering"] and marker(block["text"]) == marker(first["text"])


def _header_row(rows: list[list[dict]]) -> list[dict] | None:
    """第一行作表头。第一行带分值（其实是数据）、表格只有一列、第一行就一个单元（标题行）时没有表头。"""

    if len(rows) < 2:
        return None
    first = rows[0]
    if len(first) < 2:
        return None
    for cell in first:
        if cell["image_unit_ids"] or cell["text_boxes"] or cell["tables"]:
            return None
        if "score" in rule_signals(cell["text"]):
            return None
    return first


class _Builder:
    def __init__(self, outline: dict, comments: list[dict]):
        self.section_by_unit = {node["unit_id"]: node["section_id"] for node in outline["nodes"]}
        self.comment_units = {str(item["comment_id"]): item["unit_id"] for item in comments}
        self.anchored: set[str] = set()
        self.items: list[dict] = []
        self.table_headers: list[str] = []
        self.excluded: list[dict] = []
        self.section: str | None = None
        self.lead: dict | None = None
        self.figure: dict | None = None
        self.figure_captioned = False

    def add(self, kind: str, unit_ids=(), *, context=(), comments=(), images=(), headers=None) -> dict:
        item = {
            "item_id": None,
            "kind": kind,
            "section_id": self.section,
            "unit_ids": list(unit_ids),
            "context_unit_ids": list(context),
            "comment_unit_ids": list(comments),
            "image_unit_ids": list(images),
            "prefill": None,
            "exclusion": None,
        }
        if headers is not None:
            item["header_unit_ids"] = list(headers)
        self.items.append(item)
        return item

    def comments_of(self, comment_ids) -> list[str]:
        units = []
        for comment_id in comment_ids or ():
            unit_id = self.comment_units.get(comment_id)
            if unit_id and unit_id not in self.anchored:
                self.anchored.add(unit_id)
                units.append(unit_id)
        return units

    def exclude(self, unit_id: str, reason: str) -> None:
        self.excluded.append({"unit_id": unit_id, "reason": reason})

    def reset(self) -> None:
        self.lead = None
        self.figure = None

    def paragraph(self, blocks: list[dict], index: int) -> int:
        """处理一段，返回下一个要处理的位置（列表会一次吃掉多段）。"""

        block = blocks[index]
        unit_id, text = block["unit_id"], block["text"]
        comments = self.comments_of(block["comment_ids"])
        images = block["image_unit_ids"]
        if block["toc"]:
            self.exclude(unit_id, REASON_TOC)
            self.reset()
            if comments:
                self.add("comment", comments=comments)
            for image_id in images:
                self.add("image", images=[image_id])
            return index + 1
        if unit_id in self.section_by_unit:
            self.section = self.section_by_unit[unit_id]
            self.reset()
            # 带规则信号或批注的标题也要有条目：“3.2 接口须统一编码”可能就是一条规范，
            # 带阻断信号的还会卡发布，没有条目用户就没有地方处理。
            if comments or images or _has_rule_signal(text):
                self.add("heading", [unit_id], comments=comments, images=images)
            return index + 1
        if not comments and not images:
            reason = REASON_NOISE if is_noise(text) else REASON_TOTAL if is_total_statement(text) else None
            if reason:
                self.exclude(unit_id, reason)
                return index + 1
        if block["text_box"]:
            self.add("paragraph", [unit_id], comments=comments)
            return index + 1
        last = self.items[-1] if self.items else None
        if (
            _CAPTION_RE.match(text)
            and self.figure is not None
            and self.figure is last
            and last["image_unit_ids"]
            and not self.figure_captioned
        ):
            last["kind"] = "figure"
            last["unit_ids"].append(unit_id)
            last["comment_unit_ids"].extend(comments)
            last["image_unit_ids"].extend(images)
            self.reset()
            return index + 1
        if self.lead is not None and _is_list_entry(block):
            return self._list(blocks, index, comments)
        item = self.add("paragraph", [unit_id], comments=comments, images=images)
        self.lead = item if is_lead(text) else None
        self.figure = item if self.lead is not None or _CAPTION_RE.match(text) else None
        self.figure_captioned = self.figure is not None and self.lead is None
        return index + 1

    def _list(self, blocks: list[dict], index: int, first_comments: list[str]) -> int:
        first = blocks[index]
        group = [first]
        cursor = index + 1
        while cursor < len(blocks):
            block = blocks[cursor]
            if (
                block["type"] != "paragraph"
                or block["text_box"]
                or block["toc"]
                or block["unit_id"] in self.section_by_unit
                or not _same_list(first, block)
            ):
                break
            group.append(block)
            cursor += 1
        lead = self.lead
        comments = [first_comments, *(self.comments_of(block["comment_ids"]) for block in group[1:])]
        if len(group) > MAX_LIST_ENTRIES:
            lead_unit = lead["unit_ids"][0]
            for block, block_comments in zip(group, comments):
                self.add("paragraph", [block["unit_id"]], context=[lead_unit], comments=block_comments,
                         images=block["image_unit_ids"])
        else:
            lead["kind"] = "list"
            for block, block_comments in zip(group, comments):
                lead["unit_ids"].append(block["unit_id"])
                lead["comment_unit_ids"].extend(block_comments)
                lead["image_unit_ids"].extend(block["image_unit_ids"])
        self.reset()
        return cursor

    def image(self, block: dict) -> None:
        comments = self.comments_of(block["comment_ids"])
        last = self.items[-1] if self.items else None
        self.lead = None
        if self.figure is not None and self.figure is last:
            last["kind"] = "figure"
            last["image_unit_ids"].append(block["unit_id"])
            last["comment_unit_ids"].extend(comments)
            if self.figure_captioned:
                self.figure = None
            return
        self.figure = self.add("image", images=[block["unit_id"]], comments=comments)
        self.figure_captioned = False

    def table(self, block: dict) -> None:
        self.reset()
        rows = block["rows"]
        header = _header_row(rows)
        header_ids = [cell["unit_id"] for cell in header or () if cell["unit_id"]]
        self.table_headers.extend(header_ids)
        if header is not None:
            comments = [unit for cell in header for unit in self.comments_of(cell.get("comment_ids"))]
            if comments:
                self.add("comment", comments=comments)
        for row in rows[1:] if header is not None else rows:
            self._row(row, header_ids)
        self.reset()

    def _row(self, row: list[dict], header_ids: list[str]) -> None:
        owned: list[str] = []
        context = list(header_ids)
        comments: list[str] = []
        images: list[str] = []
        nested: list[dict] = []
        all_noise = True
        for cell in row:
            if cell["shared"]:
                if cell["unit_id"] not in header_ids:
                    context.append(cell["unit_id"])
                continue
            if cell["unit_id"]:
                owned.append(cell["unit_id"])
                all_noise = all_noise and is_noise(cell["text"])
            for box in cell["text_boxes"]:
                owned.append(box["unit_id"])
                comments.extend(self.comments_of(box["comment_ids"]))
                all_noise = False
            comments.extend(self.comments_of(cell["comment_ids"]))
            images.extend(cell["image_unit_ids"])
            nested.extend(cell["tables"])
        if owned and all_noise and not comments and not images:
            for unit_id in owned:
                self.exclude(unit_id, REASON_NOISE)
        elif owned or images:
            self.add("table_row", owned, context=context, comments=comments, images=images, headers=header_ids)
        elif comments:
            self.add("comment", comments=comments)
        for table in nested:
            self.table(table)

    def finish(self) -> dict:
        self.section = None
        for unit_id in self.comment_units.values():
            if unit_id not in self.anchored:  # 定位不到的批注单独成条
                self.add("comment", comments=[unit_id])
        for item in self.items:
            item["item_id"] = _item_id(item)
        return {
            "version": ITEMS_VERSION,
            "items": self.items,
            "table_headers": self.table_headers,
            "excluded": self.excluded,
        }


def _item_id(item: dict) -> str:
    raw = "|".join([item["kind"], *item["unit_ids"], *item["comment_unit_ids"], *item["image_unit_ids"]])
    return "R-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]


def build_items(blocks: list[dict], outline: dict, comments: list[dict]) -> dict:
    builder = _Builder(outline, comments)
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block["type"] == "paragraph":
            index = builder.paragraph(blocks, index)
            continue
        if block["type"] == "table":
            builder.table(block)
        else:
            builder.image(block)
        index += 1
    return builder.finish()


def _render_row(item: dict, ledger) -> str:
    header_ids = item.get("header_unit_ids") or []
    labels = {ledger.unit(unit_id).context.get("col"): ledger.unit(unit_id).text for unit_id in header_ids}
    values = [ledger.unit(unit_id) for unit_id in item["context_unit_ids"] if unit_id not in header_ids]
    values.extend(ledger.unit(unit_id) for unit_id in item["unit_ids"])
    values.sort(key=lambda unit: unit.context.get("col", 0))
    parts = []
    for unit in values:
        label = labels.get(unit.context.get("col")) if unit.kind == "table_cell" else None
        parts.append(f"{label}：{unit.text}" if label else unit.text)
    return "；".join(parts)


def render_item(item: dict, ledger) -> str:
    """条目的文字形式，页面和起草共用。只加结构标记，不改原文。"""

    lines: list[str] = []
    if item["kind"] == "table_row":
        row = _render_row(item, ledger)
        if row:
            lines.append(row)
    else:
        lines.extend(f"（上文：{ledger.unit(unit_id).text}）" for unit_id in item["context_unit_ids"])
        lines.extend(ledger.unit(unit_id).text for unit_id in item["unit_ids"])
    lines.extend(ledger.unit(unit_id).text for unit_id in item["image_unit_ids"])
    for unit_id in item["comment_unit_ids"]:
        unit = ledger.unit(unit_id)
        anchor = str(unit.context.get("anchor_text") or "") if not item["unit_ids"] else ""
        lines.append(f"（批注：{unit.text}；标注的原文：{anchor}）" if anchor else f"（批注：{unit.text}）")
    return "\n".join(lines)
