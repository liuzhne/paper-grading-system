"""一次评分运行判分时看到的冻结正文（docs/评分证据与理由改造方案.md G1）。

Core 评分有两种绑定方式：

* v2 提交：运行绑定一行 :class:`DocumentSnapshot`（``run.document_snapshot``）。
* 论文：运行只有 ``paper_id``，冻结正文按 ``document_snapshot_ref`` 存在
  :class:`LocalDocumentSnapshotStore` 里。

以前证据视图和正文视图只认第一种，论文走 Core 评分时被当成旧评分路径，
证据全部显示“无锚点”。两者都没有的才是旧评分路径（``legacy``），由调用方
读 :class:`PaperChunk`。冻结正文读不到时如实返回原因，**不退回最新解析文本**。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging


logger = logging.getLogger(__name__)

NOT_BOUND_REASON = (
    "该评分运行没有绑定冻结文档快照，无法还原判分时的正文；"
    "不退回最新解析文本以免与当初的证据不一致。"
)
UNREADABLE_REASON = (
    "判分时冻结的正文读不到（快照缺失或损坏），无法还原判分时的正文；"
    "不退回最新解析文本以免与当初的证据不一致。"
)


@dataclass(frozen=True)
class FrozenDocument:
    #: ``core``：Core 评分（冻结正文可能读不到）；``legacy``：旧评分路径。
    kind: str
    payload: dict | None = None
    snapshot_hash: str | None = None
    unavailable_reason: str | None = None

    @property
    def available(self) -> bool:
        return self.payload is not None

    def units(self) -> dict:
        """``evidence_unit_id`` → 证据单元。"""

        return {
            unit.get("evidence_unit_id"): unit
            for unit in ((self.payload or {}).get("evidence_units") or [])
            if isinstance(unit, dict)
        }


LEGACY = FrozenDocument(kind="legacy")


def resolve_frozen_document(run) -> FrozenDocument:
    if getattr(run, "submission_id", None) is not None:
        snapshot = run.document_snapshot
        if snapshot is None:
            return FrozenDocument(kind="core", unavailable_reason=NOT_BOUND_REASON)
        return FrozenDocument(
            kind="core",
            payload=snapshot.snapshot_payload or {},
            snapshot_hash=snapshot.snapshot_hash,
        )
    ref = getattr(run, "document_snapshot_ref", None)
    if not ref:
        return LEGACY
    from backend.app.services.scoring.adapters.persistence import (
        LocalDocumentSnapshotStore,
    )

    try:
        payload = LocalDocumentSnapshotStore().resolve(ref=ref)
    except Exception as exc:  # noqa: BLE001 - 读不到就如实说明，不中断页面
        logger.warning(
            "frozen_document_unreadable run_id=%s exception_type=%s",
            getattr(run, "id", None),
            type(exc).__name__,
        )
        return FrozenDocument(kind="core", unavailable_reason=UNREADABLE_REASON)
    return FrozenDocument(
        kind="core",
        payload=payload,
        snapshot_hash=payload.get("document_snapshot_hash"),
    )


__all__ = ["FrozenDocument", "LEGACY", "resolve_frozen_document"]
