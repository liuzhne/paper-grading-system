"""批次级复核队列（前端 v2 计划 §5-B）。

统一队列同时承载两类工作，界面不能把它们混为一谈：

``blocking``
    ``ManualReviewTask`` 的 open/claimed 任务。它代表**结论本身还不成立**
    ——规则执行被阻断、provider 失败、Core 判定 invalid。必须领取后凭冻结
    证据解决，未清空前该 run 不应出现完整总分与等级。

``ordinary``
    ``need_manual_review`` 的评分项。有 AI 分，采纳或改分即可。

阻塞排在前面：先解决「算不算数」，再讨论「给几分」。

队列范围经结果选择器（§5-B），与工作台 KPI、复核统计、导出预检同源。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.db.models import ManualReviewTask
from backend.app.db.models import Paper
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun
from backend.app.services.batches.results import select_current_results


DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: 仍占用复核注意力的任务状态。
OPEN_TASK_STATUSES = ("open", "claimed")


def _as_float(value):
    return None if value is None else float(value)


def _ordinary_entries(session, run_ids, papers_by_run):
    if not run_ids:
        return []
    items = session.scalars(
        select(ScoreItem)
        .where(
            ScoreItem.scoring_run_id.in_(run_ids),
            ScoreItem.need_manual_review.is_(True),
        )
        .options(selectinload(ScoreItem.criterion))
        .order_by(ScoreItem.scoring_run_id, ScoreItem.created_at, ScoreItem.id)
    ).all()

    entries = []
    for item in items:
        paper = papers_by_run.get(item.scoring_run_id)
        entries.append(
            {
                "queue_type": "ordinary",
                "score_item_id": item.id,
                "scoring_run_id": item.scoring_run_id,
                "task_id": None,
                "task_version": None,
                "paper_id": paper.id if paper else None,
                "student_id": paper.student_id if paper else None,
                "student_name": paper.student_name if paper else None,
                "paper_title": paper.title if paper else None,
                "criterion_code": item.criterion_code,
                "criterion_name": item.criterion_name,
                "ai_score": _as_float(item.ai_score),
                "final_score": _as_float(item.final_score),
                "max_score": _as_float(item.max_score),
                # Core 持久化不写 confidence。保持 None——显示成 0 会让人
                # 以为模型毫无把握，那是完全不同的结论。
                "confidence": _as_float(item.confidence),
                "review_reasons": item.review_reasons or [],
                "review_reason": item.review_reason,
                "review_revision": item.review_revision or 1,
                # 有有效 AI 分才谈得上「采纳系统给分」。
                "acceptable": item.ai_score is not None,
                "trigger_code": None,
                "trigger_message": None,
                "assigned_reviewer_id": None,
            }
        )
    return entries


def _blocking_entries(session, run_ids, papers_by_run):
    if not run_ids:
        return []
    tasks = session.scalars(
        select(ManualReviewTask)
        .where(
            ManualReviewTask.scoring_run_id.in_(run_ids),
            ManualReviewTask.status.in_(OPEN_TASK_STATUSES),
        )
        .order_by(
            ManualReviewTask.priority,
            ManualReviewTask.created_at,
            ManualReviewTask.id,
        )
    ).all()

    entries = []
    for task in tasks:
        paper = papers_by_run.get(task.scoring_run_id)
        entries.append(
            {
                "queue_type": "blocking",
                "score_item_id": task.score_item_id,
                "scoring_run_id": task.scoring_run_id,
                "task_id": task.id,
                "task_version": task.version,
                "paper_id": paper.id if paper else None,
                "student_id": paper.student_id if paper else None,
                "student_name": paper.student_name if paper else None,
                "paper_title": paper.title if paper else None,
                "criterion_code": task.criterion_code,
                "criterion_name": None,
                "ai_score": None,
                "final_score": None,
                "max_score": None,
                "confidence": None,
                "review_reasons": [
                    {
                        "source": "core_issue",
                        "code": task.trigger_code,
                        "message": task.trigger_message,
                        "rule_code": task.rule_code,
                    }
                ],
                "review_reason": task.trigger_message,
                "review_revision": task.version,
                # 阻塞任务不能被批量采纳：必须领取后凭冻结证据解决。
                "acceptable": False,
                "trigger_code": task.trigger_code,
                "trigger_message": task.trigger_message,
                "assigned_reviewer_id": task.assigned_reviewer_id,
            }
        )
    return entries


def build_review_queue(session, batch, *, limit=DEFAULT_LIMIT, cursor=None):
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    selection = select_current_results(session, batch)
    run_ids = list(selection.runs.values())

    papers_by_run = {}
    if run_ids:
        rows = session.execute(
            select(ScoringRun.id, Paper)
            .join(Paper, Paper.id == ScoringRun.paper_id)
            .where(ScoringRun.id.in_(run_ids))
        ).all()
        papers_by_run = {run_id: paper for run_id, paper in rows}

    blocking = _blocking_entries(session, run_ids, papers_by_run)
    ordinary = _ordinary_entries(session, run_ids, papers_by_run)
    # 先解决「算不算数」，再讨论「给几分」。
    entries = blocking + ordinary

    start = int(cursor) if cursor is not None else 0
    start = max(0, min(start, len(entries)))
    page = entries[start : start + limit]
    next_cursor = start + limit if start + limit < len(entries) else None

    return {
        "batch_id": batch.id,
        "result_revision": selection.revision,
        "ordinary_pending": len(ordinary),
        "blocking_open": len(blocking),
        "entries": page,
        "next_cursor": None if next_cursor is None else str(next_cursor),
    }


__all__ = ["build_review_queue", "DEFAULT_LIMIT", "MAX_LIMIT", "OPEN_TASK_STATUSES"]
