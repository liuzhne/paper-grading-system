"""评分耗时估计（前端 v2 计划 决策 12、§5-D、§11）。

§11 把口径与冷启动列为未决，但把未决时的行为定死了：「**无可靠样本显示暂无
估计，不编造分钟数**」。所以这里只做一件事——样本够就给区间，不够就明说没有。

§5-D 的硬约束：耗时是估计值，**不能用于评分租约或超时判定**。返回的是区间而不是
一个精确秒数，正是因为精确数字会被当成承诺；``display_only`` 把这条写进合同，
而不是只写在文档里。
"""

from __future__ import annotations

from sqlalchemy import select

from backend.app.db.models import BatchScoringJob
from backend.app.db.models import GradingBatch


#: 少于这个数量的样本算出来的均值不比猜测可靠，标成「估计」会让人当真。
MIN_SAMPLES = 3

#: 取最近这么多次任务。更早的样本跨了模型与 prompt 版本，混在一起没有意义。
SAMPLE_WINDOW = 20

#: 区间宽度。给一个精确数字会被当成承诺。
SPREAD = 0.35

_FINISHED = ("completed", "completed_with_errors")


def _no_estimate(sample_count, message):
    return {
        "available": False,
        "sample_count": sample_count,
        "seconds_per_item": None,
        "low_seconds": None,
        "high_seconds": None,
        "display_only": True,
        "message": message,
    }


def estimate_scoring_duration(session, *, organization_id, item_count):
    """按历史已完成任务的每份材料耗时给一个区间。

    只统计**已结束**的任务：还在跑的没有结束时间，把它算进去等于用一个未知数
    做分母。
    """
    if not item_count:
        return _no_estimate(0, "还没有待评材料，暂无估计。")

    query = (
        select(BatchScoringJob)
        .where(
            BatchScoringJob.status.in_(_FINISHED),
            BatchScoringJob.started_at.is_not(None),
            BatchScoringJob.finished_at.is_not(None),
            BatchScoringJob.total_items > 0,
        )
        .order_by(BatchScoringJob.finished_at.desc())
        .limit(SAMPLE_WINDOW)
    )
    if organization_id is not None:
        # 跨组织的耗时不可比：材料长度、标准与连接都不一样。
        query = query.join(
            GradingBatch, GradingBatch.id == BatchScoringJob.grading_batch_id
        ).where(GradingBatch.organization_id == organization_id)

    per_item = []
    for job in session.scalars(query).all():
        elapsed = (job.finished_at - job.started_at).total_seconds()
        if elapsed > 0:
            per_item.append(elapsed / job.total_items)

    if len(per_item) < MIN_SAMPLES:
        return _no_estimate(
            len(per_item),
            "历史样本不足（%d 次），暂无估计。" % len(per_item),
        )

    average = sum(per_item) / len(per_item)
    total = average * item_count
    return {
        "available": True,
        "sample_count": len(per_item),
        "seconds_per_item": average,
        "low_seconds": total * (1 - SPREAD),
        "high_seconds": total * (1 + SPREAD),
        # §5-D：只进展示，不参与租约与超时判定。
        "display_only": True,
        "message": None,
    }


__all__ = ["estimate_scoring_duration", "MIN_SAMPLES", "SAMPLE_WINDOW"]
