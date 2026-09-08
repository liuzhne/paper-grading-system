"""分数分布（前端 v2 计划 §6，阶段 1/6）。

§11 把**分桶策略**与**「上一批次」的定义**列为未决：rubric 满分可变，非论文
Profile 的量纲也不同，固定档位会把两个不可比的批次画进同一张图。未决时的处理
办法是「不展示跨标准均分差，原始有效终分与缺结果数仍可显示」，本模块只做这
一部分——`bucketing` 显式返回 ``None``，让前端能区分「还没定」和「算出来是空」。

取分范围来自 :func:`select_current_results`，与 KPI、复核队列、导出预检同源；
各自去查一遍会让同一个批次在四个地方显示四个数。
"""

from __future__ import annotations

from sqlalchemy import select

from backend.app.db.models import ScoringRun
from backend.app.services.batches.results import SCORED_STATUSES
from backend.app.services.batches.results import select_current_results
from backend.app.services.batches.review_stats import build_review_stats


def _total(run):
    """终分优先，缺失时回落 AI 分。

    与 ``calibration/analytics`` 同口径。两者都为空才算这份材料没有结果——把
    有结论但未落终分的 run 算成缺结果，会让分布和 KPI 对不上。
    """
    if run.final_total_score is not None:
        return float(run.final_total_score)
    if run.ai_total_score is not None:
        return float(run.ai_total_score)
    return None


def build_score_distribution(session, batch):
    selection = select_current_results(session, batch)
    stats = build_review_stats(session, batch)

    run_ids = [
        run_id
        for paper_id, run_id in selection.runs.items()
        if selection.run_statuses.get(paper_id) in SCORED_STATUSES
    ]
    runs = (
        session.scalars(select(ScoringRun).where(ScoringRun.id.in_(run_ids))).all()
        if run_ids
        else []
    )

    scores = [total for total in (_total(run) for run in runs) if total is not None]
    scores.sort()

    rubric = getattr(batch, "rubric", None)
    return {
        "batch_id": batch.id,
        "result_revision": selection.revision,
        # 满分随 rubric 变，前端不能假设 100；画图要用它做纵轴上界。
        "max_score": float(rubric.total_score) if rubric is not None else None,
        "scores": scores,
        "scored_count": len(scores),
        # 缺结果单独计数：并进分布会把平均分拉低成一个假数字。
        "without_results": selection.total_count - len(scores),
        # 阻塞项的总分尚不成立。不单独报出来，读者会把一条完整的曲线当成这批
        # 的最终形态，而它其实还会变。
        "blocking_open": stats["blocking_open"],
        "average": (sum(scores) / len(scores)) if scores else None,
        # 分桶策略未定（§11）。给 None 而不是省略，前端才能把「未定」如实显示，
        # 而不是当成一次失败的请求。
        "bucketing": None,
    }


__all__ = ["build_score_distribution"]
