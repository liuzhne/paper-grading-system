"""评分项证据的展示投影（前端 v2 计划 §5-A）。

原始 ``ScoreItem.evidence`` 保留审计语义**不动**；这里是另一个只读投影，由
调用方显式索取。旧客户端与 golden 因此不受影响。

设计上最容易做错、后果也最严重的一条：

    **不能从 ``payload_hash`` 反推模型原句。**

Core 的证据引用只留 ``evidence_unit_id`` + ``payload_hash``，拿不回模型当初
写下的那句引文。能显示的只有对应证据单元的**上下文**，而且必须标成上下文。
把上下文冒充成引文，会让复核者以为自己核对的就是模型引用的那句话——这正是
「每个扣分都要带证据」想要防住的事情。

``location_status`` 取值：

``verified``
    引文在当前文本中可验证，可以作为引文显示并高亮。
``quote_not_found``
    锚点在，但引文对不上当前文本（多半是重新解析过）。仍可看块上下文。
``unit_context``
    只有单元 locator/哈希，显示证据单元上下文，不是引文。
``anchor_missing``
    锚点指向的块/单元已不存在。
``no_anchor``
    这条证据没有任何锚点。
``check_fact``
    非引用类证据（结构/格式发现），展示检查事实而非原文摘录。
"""

from __future__ import annotations

from sqlalchemy import select

from backend.app.db.models import PaperChunk


#: 视为「引用了原文」的证据类型；其余按检查事实展示。
QUOTE_EVIDENCE_TYPES = frozenset({"source_quote"})


def _compact(text):
    return " ".join(str(text or "").split())


def _blank(**overrides):
    base = {
        "source_kind": None,
        "snapshot_hash": None,
        "anchor_id": None,
        "evidence_type": None,
        "quote": None,
        "context_text": None,
        "section_title": None,
        "page_start": None,
        "page_end": None,
        "location_status": "no_anchor",
        "unlocatable_reason": None,
    }
    base.update(overrides)
    return base


def _legacy_view(session, run, entries):
    chunk_ids = [entry.get("chunk_id") for entry in entries if entry.get("chunk_id")]
    chunks = {}
    if chunk_ids:
        chunks = {
            chunk.id: chunk
            for chunk in session.scalars(
                select(PaperChunk).where(PaperChunk.id.in_(chunk_ids))
            ).all()
        }

    views = []
    for entry in entries:
        quote = entry.get("quote") or None
        chunk_id = entry.get("chunk_id")
        if not chunk_id:
            views.append(
                _blank(
                    source_kind="legacy_chunks",
                    evidence_type="source_quote",
                    quote=None,
                    context_text=quote,
                    location_status="no_anchor",
                    unlocatable_reason="这条证据没有记录可定位的锚点，无法回到原文。",
                )
            )
            continue

        chunk = chunks.get(chunk_id)
        if chunk is None:
            views.append(
                _blank(
                    source_kind="legacy_chunks",
                    anchor_id=chunk_id,
                    evidence_type="source_quote",
                    context_text=quote,
                    location_status="anchor_missing",
                    unlocatable_reason=(
                        "证据指向的文本块已不存在，可能是材料被重新解析过。"
                    ),
                )
            )
            continue

        matched = bool(quote) and _compact(quote) in _compact(chunk.text)
        views.append(
            _blank(
                source_kind="legacy_chunks",
                anchor_id=chunk.id,
                evidence_type="source_quote",
                # 对不上当前文本时不作为引文显示，避免高亮到错误位置。
                quote=quote if matched else None,
                context_text=chunk.text,
                section_title=chunk.section_title,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                location_status="verified" if matched else "quote_not_found",
                unlocatable_reason=(
                    None
                    if matched
                    else "引文与当前解析文本不一致，无法精确高亮；下方为所在块的上下文。"
                ),
            )
        )
    return views


def _core_view(run, entries):
    snapshot = run.document_snapshot
    payload = (snapshot.snapshot_payload if snapshot else None) or {}
    units = {
        unit.get("evidence_unit_id"): unit
        for unit in (payload.get("evidence_units") or [])
        if isinstance(unit, dict)
    }
    snapshot_hash = snapshot.snapshot_hash if snapshot else None

    views = []
    for entry in entries:
        evidence_type = entry.get("evidence_type")
        unit_id = entry.get("evidence_unit_id")

        if evidence_type not in QUOTE_EVIDENCE_TYPES:
            # 结构/格式类发现不是原文摘录，展示检查事实与审计定位信息。
            views.append(
                _blank(
                    source_kind="core_snapshot",
                    snapshot_hash=snapshot_hash,
                    anchor_id=unit_id,
                    evidence_type=evidence_type,
                    location_status="check_fact",
                )
            )
            continue

        unit = units.get(unit_id)
        if unit is None:
            views.append(
                _blank(
                    source_kind="core_snapshot",
                    snapshot_hash=snapshot_hash,
                    anchor_id=unit_id,
                    evidence_type=evidence_type,
                    location_status="anchor_missing",
                    unlocatable_reason=(
                        "冻结快照中找不到该证据单元，无法回到判分时的原文。"
                    ),
                )
            )
            continue

        section_path = unit.get("section_path") or []
        views.append(
            _blank(
                source_kind="core_snapshot",
                snapshot_hash=snapshot_hash,
                anchor_id=unit_id,
                evidence_type=evidence_type,
                # 只留哈希时拿不回模型原句：给上下文，且不冒充引文。
                quote=None,
                context_text=unit.get("normalized_text"),
                section_title=".".join(str(part) for part in section_path) or None,
                location_status="unit_context",
            )
        )
    return views


def build_evidence_view(session, run, item):
    """把一个评分项的证据投影成可展示、可定位、可解释的形状。"""
    entries = [entry for entry in (item.evidence or []) if isinstance(entry, dict)]
    if not entries:
        return []
    if getattr(run, "submission_id", None) is not None:
        return _core_view(run, entries)
    return _legacy_view(session, run, entries)


__all__ = ["build_evidence_view", "QUOTE_EVIDENCE_TYPES"]
