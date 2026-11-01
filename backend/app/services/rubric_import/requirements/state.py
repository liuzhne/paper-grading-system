"""章节和条目与台账的衔接（docs/Word评分规范抽取与对应方案.md 第 7、10 节）。

条目存在 ``raw_parse_output``，读写都收在这个模块，以后要改建表时只动这里。
"""

from __future__ import annotations

from copy import deepcopy

from backend.app.services.rubric_import.classification.signals import rule_signals
from backend.app.services.rubric_import.coverage import is_blocking
from backend.app.services.rubric_import.requirements.items import REASON_TOC
from backend.app.services.rubric_import.requirements.items import build_items
from backend.app.services.rubric_import.requirements.outline import build_outline
from backend.app.services.rubric_import.sources.docx_adapter import DocxView
from backend.app.services.rubric_import.sources.units import SourceLedger

_WORD_EVIDENCE_SUFFIX = ".word_evidence"
PAYLOAD_KEYS = ("outline", "requirement_items")


def _apply_ledger_marks(payload: dict, ledger: SourceLedger, keep_unclaimed) -> None:
    """目录标 structural；没有阻断信号的标题、表头和图片标 context。

    必须在 ``mark_remaining_blocks`` 之前调用。批注的台账状态不动（第 7 节）：改回 unclaimed
    会让每条批注都变成新的发布阻断。图片的文字只是占位和替代文字，不是正文，不能造成阻断。
    ``mark`` 不覆盖 consumed，Word 佐证的认领不受影响。
    """

    keep = set(keep_unclaimed)
    items = payload["requirement_items"]
    for entry in items["excluded"]:
        if entry["reason"] == REASON_TOC:
            ledger.mark(entry["unit_id"], "structural", reason=REASON_TOC)
    for node in payload["outline"]["nodes"]:
        if node["unit_id"] not in keep and not is_blocking(rule_signals(ledger.unit(node["unit_id"]).text)):
            ledger.mark(node["unit_id"], "context")
    for unit_id in items["table_headers"]:
        if unit_id not in keep:
            ledger.mark(unit_id, "context")
    for item in items["items"]:
        for unit_id in item["image_unit_ids"]:
            ledger.mark(unit_id, "context")


def _apply_prefill(items: dict, ledger: SourceLedger) -> None:
    """Word 表格行已按编号或名称匹配到 Excel 的（``T02.word_evidence``）预填，不送 AI，仍要人工确认。"""

    for item in items["items"]:
        codes = []
        for unit_id in item["unit_ids"]:
            for field_ref in ledger.status(unit_id).claimed_by:
                if field_ref.endswith(_WORD_EVIDENCE_SUFFIX):
                    code = field_ref[: -len(_WORD_EVIDENCE_SUFFIX)]
                    if code not in codes:
                        codes.append(code)
        item["prefill"] = {"criterion_codes": codes, "source": "word_evidence"} if codes else None


def assemble_requirements(view: DocxView, ledger: SourceLedger, *, keep_unclaimed=()) -> dict:
    outline = build_outline(view.structure)
    items = build_items(view.structure, outline, view.comments)
    items["read_report"] = deepcopy(view.read_report)
    payload = {"outline": outline, "requirement_items": items}
    _apply_ledger_marks(payload, ledger, keep_unclaimed)
    _apply_prefill(items, ledger)
    return payload


def reapply_requirements(raw: dict, ledger: SourceLedger, *, keep_unclaimed=()) -> dict:
    """按确认的结构重新解析时台账状态是新生成的，文件也不再读一遍：条目沿用，标记和预填重算。"""

    payload = {key: deepcopy(raw[key]) for key in PAYLOAD_KEYS}
    _apply_ledger_marks(payload, ledger, keep_unclaimed)
    _apply_prefill(payload["requirement_items"], ledger)
    return payload


def template_read_warnings(report: dict | None) -> list[dict]:
    if not report:
        return []
    parts = []
    if report["missing_chars"]:
        parts.append(f"正文有 {report['missing_chars']} 个字符没有读到（如“{report['missing_sample']}”）")
    unread = [f"{label} {count} 字" for label, count in report["unread_parts"].items()]
    if unread:
        parts.append("、".join(unread) + "未读取")
    if report["unread_objects"]:
        parts.append(f"{report['unread_objects']} 个图表或嵌入对象未读取")
    if not parts:
        return []
    message = "；".join(parts)
    return [
        {
            "code": "TEMPLATE_READ_INCOMPLETE",
            "message": f"Word 模板内容没有读全：{message}。这些内容不会进入评分规范，如有要求请手动录入。",
        }
    ]
