"""按 run 读取的只读正文投影（前端 v2 计划 §5-A）。

放在 API/adapter 层：**不改 Core 的不可变快照、已有证据或评分计算**，只把
它们投影成评分工作区中间栏能渲染的形状。

两条来源的身份强度不同，界面必须能区分：

* ``core_snapshot`` —— 运行绑定的 :class:`DocumentSnapshot`。不可变，
  ``evidence_unit_id`` 是硬锚点，能确凿地回答「当初判分时看到的就是这段」。
  但快照里**没有页码**，因此页码留空而不是编造。
* ``legacy_chunks`` —— 当前的 :class:`PaperChunk`。这是**可变的解析结果**，
  重新解析后可能与判分当时不同。有真实页码（PDF），但必须标注 provenance，
  否则用户会以为自己在核对冻结证据。

查不到冻结快照时返回 ``unavailable`` 并给出原因，**不静默退回最新解析文本**。
"""

from __future__ import annotations

from sqlalchemy import select

from backend.app.db.models import PaperChunk


#: 单页最大块数，避免整篇大文档一次性返回。
DEFAULT_LIMIT = 40
MAX_LIMIT = 200


def _core_blocks(snapshot):
    payload = snapshot.snapshot_payload or {}
    units = {
        unit.get("evidence_unit_id"): unit
        for unit in (payload.get("evidence_units") or [])
        if isinstance(unit, dict)
    }
    blocks = []
    for section in payload.get("sections") or []:
        if not isinstance(section, dict):
            continue
        heading = section.get("heading")
        unit_ids = section.get("evidence_unit_ids") or []
        if not unit_ids:
            # 没有证据单元的章节仍要显示，否则正文会缺段。
            blocks.append(
                {
                    "block_id": "section:%s" % (section.get("section_ordinal"),),
                    "evidence_unit_id": None,
                    "chunk_id": None,
                    "section_title": heading,
                    "text": section.get("normalized_text") or "",
                    "page_start": None,
                    "page_end": None,
                }
            )
            continue
        for unit_id in unit_ids:
            unit = units.get(unit_id) or {}
            blocks.append(
                {
                    "block_id": "unit:%s" % (unit_id,),
                    "evidence_unit_id": unit_id,
                    "chunk_id": None,
                    "section_title": heading,
                    "text": unit.get("normalized_text") or "",
                    # Core 快照不携带页码来源，留空而不是编造一个。
                    "page_start": None,
                    "page_end": None,
                }
            )
    return blocks


def _legacy_blocks(session, paper_id):
    chunks = session.scalars(
        select(PaperChunk)
        .where(PaperChunk.paper_id == paper_id)
        .order_by(PaperChunk.page_start, PaperChunk.id)
    ).all()
    return [
        {
            "block_id": "chunk:%s" % (chunk.id,),
            "evidence_unit_id": None,
            "chunk_id": chunk.id,
            "section_title": chunk.section_title,
            "text": chunk.text or "",
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
        }
        for chunk in chunks
    ]


def _anchor_index(blocks, anchor_id):
    """锚点所在块的下标；找不到返回 None。"""
    if not anchor_id:
        return None
    for index, block in enumerate(blocks):
        if anchor_id in (
            block["block_id"],
            block["evidence_unit_id"],
            block["chunk_id"],
        ):
            return index
    return None


def build_document_view(
    session, run, *, limit=DEFAULT_LIMIT, cursor=None, anchor_id=None
):
    """把一次评分运行绑定的正文投影成有界分页的块序列。

    :param anchor_id: 证据锚点（``evidence_unit_id`` / ``chunk_id`` / ``block_id``）。
        提供时直接返回它所在的那一页，避免用户从头翻。
    """
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))

    snapshot = run.document_snapshot
    if run.submission_id is not None:
        # 正式 Core：只认运行绑定的冻结快照。
        if snapshot is None:
            return {
                "run_id": run.id,
                "source_kind": "unavailable",
                "frozen": False,
                "text_provenance": "unavailable",
                "document_snapshot_hash": None,
                "unavailable_reason": (
                    "该评分运行没有绑定冻结文档快照，无法还原判分时的正文；"
                    "不退回最新解析文本以免与当初的证据不一致。"
                ),
                "blocks": [],
                "next_cursor": None,
                "anchor_found": False,
            }
        blocks = _core_blocks(snapshot)
        meta = {
            "source_kind": "core_snapshot",
            "frozen": True,
            "text_provenance": "frozen_snapshot",
            "document_snapshot_hash": snapshot.snapshot_hash,
            "unavailable_reason": None,
        }
    else:
        blocks = _legacy_blocks(session, run.paper_id)
        meta = {
            "source_kind": "legacy_chunks" if blocks else "unavailable",
            "frozen": False,
            "text_provenance": "current_parse" if blocks else "unavailable",
            "document_snapshot_hash": None,
            "unavailable_reason": (
                None
                if blocks
                else "该材料没有可用的解析文本，请重新解析后再查看。"
            ),
        }

    anchor_at = _anchor_index(blocks, anchor_id)
    if anchor_at is not None:
        start = (anchor_at // limit) * limit
    else:
        start = int(cursor) if cursor is not None else 0
        start = max(0, min(start, len(blocks)))

    page = blocks[start : start + limit]
    next_cursor = start + limit if start + limit < len(blocks) else None

    return {
        "run_id": run.id,
        **meta,
        "blocks": page,
        "next_cursor": None if next_cursor is None else str(next_cursor),
        "anchor_found": anchor_at is not None,
    }


__all__ = ["build_document_view", "DEFAULT_LIMIT", "MAX_LIMIT"]
