"""批量采纳系统给分（前端 v2 计划 §5-B）。

设计稿的按钮写「全部采纳系统给分」，服务端**不做无界全批扫描**：调用方必须
提交用户预览过的有限集合。一次点击不该在服务端展开成对成百上千条记录的隐式
写入——那种操作出错时无从回溯，也没法让用户判断自己到底确认了什么。

三条硬约束：

1. **整次要么全写、要么全不写。** 任一项过期或不合格即冲突，不留下写了一半
   的状态。
2. **不覆盖已被人工修改的分数。** 采纳的语义是「确认系统给分」，不是「把别人
   改过的分改回去」。
3. **幂等。** 同键同载荷返回原回执；同键不同载荷是冲突——否则「重试」会悄悄
   变成「执行了另一件事」。
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from backend.app.db.models import ReviewCommandReceipt
from backend.app.db.models import ReviewLog
from backend.app.db.models import ScoreItem
from backend.app.services.batches import state as batch_state
from backend.app.services.batches.results import select_current_results


COMMAND = "review_queue.accept"

#: 单次最多接受的条目数。跨页操作需要重新选择。
MAX_ITEMS = 100


class AcceptError(Exception):
    """采纳请求不合格。"""


class AcceptConflict(AcceptError):
    """并发或前置条件失败；整次不写入。"""


def _payload_hash(batch_id, result_revision, items, reason):
    payload = {
        "batch_id": batch_id,
        "result_revision": result_revision,
        "reason": reason,
        "items": sorted(
            (entry["score_item_id"], int(entry["review_revision"])) for entry in items
        ),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def accept_items(
    session,
    batch,
    *,
    items,
    result_revision,
    idempotency_key,
    reason,
    actor_id,
    organization_id,
):
    if not items:
        raise AcceptError("采纳请求必须包含至少一个条目")
    if len(items) > MAX_ITEMS:
        raise AcceptError(
            "单次最多采纳 %d 项；跨页操作请重新选择。" % MAX_ITEMS
        )

    batch_state.guard_writable(batch)

    digest = _payload_hash(batch.id, result_revision, items, reason)
    existing = session.scalar(
        select(ReviewCommandReceipt).where(
            ReviewCommandReceipt.organization_id == organization_id,
            ReviewCommandReceipt.actor_id == actor_id,
            ReviewCommandReceipt.command == COMMAND,
            ReviewCommandReceipt.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.payload_hash != digest:
            raise AcceptConflict(
                "同一幂等键已用于另一组条目；请换一个键或重新发起。"
            )
        return {**existing.result, "replayed": True}

    # 结果集合必须与用户预览时一致，否则他确认的是另一批数据。
    selection = select_current_results(session, batch)
    if selection.revision != result_revision:
        raise AcceptConflict("批次结果已更新，请刷新后重新确认。")

    requested = {entry["score_item_id"]: int(entry["review_revision"]) for entry in items}
    rows = session.scalars(
        select(ScoreItem).where(ScoreItem.id.in_(requested))
    ).all()
    by_id = {row.id: row for row in rows}

    current_run_ids = set(selection.runs.values())
    for item_id, expected_revision in requested.items():
        item = by_id.get(item_id)
        if item is None:
            raise AcceptConflict("评分项 %s 不存在或已被移除。" % item_id)
        if item.scoring_run_id not in current_run_ids:
            raise AcceptConflict("评分项 %s 不属于当前结果集合。" % item_id)
        if not item.need_manual_review:
            raise AcceptConflict("评分项 %s 已不在待确认状态。" % item_id)
        if item.ai_score is None:
            raise AcceptConflict("评分项 %s 没有有效的系统给分，无法采纳。" % item_id)
        if (item.review_revision or 1) != expected_revision:
            raise AcceptConflict("评分项 %s 已被其他操作更新，请刷新后重试。" % item_id)
        if item.final_score is not None and float(item.final_score) != float(
            item.ai_score
        ):
            # 采纳不是「改回系统分」。已被人工调整过的项必须由人显式处理。
            raise AcceptConflict(
                "评分项 %s 已被人工修改，不能通过批量采纳覆盖。" % item_id
            )

    for item_id in requested:
        item = by_id[item_id]
        before = None if item.final_score is None else float(item.final_score)
        item.final_score = item.ai_score
        item.need_manual_review = False
        item.review_revision = (item.review_revision or 1) + 1
        session.add(item)
        session.add(
            ReviewLog(
                scoring_run_id=item.scoring_run_id,
                score_item_id=item.id,
                reviewer_id=actor_id,
                before_score=before,
                after_score=float(item.ai_score),
                reason=reason,
            )
        )

    result = {
        "batch_id": batch.id,
        "accepted_count": len(requested),
        "result_revision": selection.revision,
    }
    # 回执与 ReviewLog 同事务：否则崩溃会留下「已写日志但无回执」，重试就
    # 会重复写入。
    session.add(
        ReviewCommandReceipt(
            organization_id=organization_id,
            actor_id=actor_id,
            command=COMMAND,
            idempotency_key=idempotency_key,
            payload_hash=digest,
            result=result,
        )
    )
    return {**result, "replayed": False}


__all__ = ["accept_items", "AcceptError", "AcceptConflict", "MAX_ITEMS", "COMMAND"]
