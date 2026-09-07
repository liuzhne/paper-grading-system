"""复核统计与审计时间线（前端 v2 计划 §5-B）。

三处统计口径的取舍，都是为了让数字能被信任：

1. **已确认数按当前评分项算，不按 ReviewLog 条数。** 同一项被改两次仍然只是
   一项已确认；按日志累加会让「已确认 18 / 24」轻易超过 24。
2. **采纳与人工调整分开计。** 两者对复核者的意义完全不同。
3. **平均调整幅度只纳入有 AI 分的人工调整项，并公开分母。** 把差值为 0 的
   采纳项混进来，会把平均值稀释成一个没有意义的数字；不公开分母，读者也无法
   判断这个平均值有多少代表性。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.db.models import ReviewLog
from backend.app.db.models import ScoreItem
from backend.app.services.batches.results import select_current_results
from backend.app.services.batches.review_queue import OPEN_TASK_STATUSES


DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _as_float(value):
    return None if value is None else float(value)


def build_review_stats(session, batch):
    selection = select_current_results(session, batch)
    run_ids = list(selection.runs.values())

    items = []
    if run_ids:
        items = session.scalars(
            select(ScoreItem).where(ScoreItem.scoring_run_id.in_(run_ids))
        ).all()

    ordinary_pending = sum(1 for item in items if item.need_manual_review)
    confirmed = [item for item in items if not item.need_manual_review]

    accepted = 0
    adjustments = []
    for item in confirmed:
        ai = _as_float(item.ai_score)
        final = _as_float(item.final_score)
        if ai is None or final is None:
            # 没有 AI 分就谈不上「调整幅度」。
            continue
        if final == ai:
            accepted += 1
        else:
            adjustments.append(final - ai)

    from backend.app.db.models import ManualReviewTask

    blocking_open = 0
    if run_ids:
        blocking_open = len(
            session.scalars(
                select(ManualReviewTask).where(
                    ManualReviewTask.scoring_run_id.in_(run_ids),
                    ManualReviewTask.status.in_(OPEN_TASK_STATUSES),
                )
            ).all()
        )

    return {
        "batch_id": batch.id,
        "result_revision": selection.revision,
        "total_items": len(items),
        "ordinary_pending": ordinary_pending,
        "blocking_open": blocking_open,
        "confirmed_items": len(confirmed),
        "accepted_items": accepted,
        "adjusted_items": len(adjustments),
        # 分母公开：读者据此判断这个平均值有多少代表性。
        "adjustment_sample_size": len(adjustments),
        "average_adjustment": (
            round(sum(adjustments) / len(adjustments), 3) if adjustments else None
        ),
    }


def build_review_timeline(session, batch, *, limit=DEFAULT_LIMIT, cursor=None):
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    selection = select_current_results(session, batch)
    run_ids = list(selection.runs.values())
    if not run_ids:
        return {
            "batch_id": batch.id,
            "entries": [],
            "next_cursor": None,
        }

    start = int(cursor) if cursor is not None else 0
    rows = session.scalars(
        select(ReviewLog)
        .where(ReviewLog.scoring_run_id.in_(run_ids))
        .order_by(ReviewLog.created_at.desc(), ReviewLog.id.desc())
        .offset(start)
        .limit(limit + 1)
    ).all()

    has_more = len(rows) > limit
    page = rows[:limit]

    # ReviewLog 没有到 ScoreItem 的关系，单独取一次以补全评分项标识。
    item_ids = {log.score_item_id for log in page if log.score_item_id}
    items_by_id = {}
    if item_ids:
        items_by_id = {
            item.id: item
            for item in session.scalars(
                select(ScoreItem)
                .where(ScoreItem.id.in_(item_ids))
                .options(selectinload(ScoreItem.criterion))
            ).all()
        }

    return {
        "batch_id": batch.id,
        "entries": [
            {
                "id": log.id,
                "scoring_run_id": log.scoring_run_id,
                "score_item_id": log.score_item_id,
                "criterion_code": (
                    items_by_id[log.score_item_id].criterion_code
                    if log.score_item_id in items_by_id
                    else None
                ),
                "criterion_name": (
                    items_by_id[log.score_item_id].criterion_name
                    if log.score_item_id in items_by_id
                    else None
                ),
                "reviewer_id": log.reviewer_id,
                "before_score": _as_float(log.before_score),
                "after_score": _as_float(log.after_score),
                "reason": log.reason,
                "resolution_type": log.resolution_type,
                "created_at": log.created_at,
            }
            for log in page
        ],
        "next_cursor": str(start + limit) if has_more else None,
    }


__all__ = ["build_review_stats", "build_review_timeline", "DEFAULT_LIMIT", "MAX_LIMIT"]
