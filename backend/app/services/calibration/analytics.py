"""L2 批量分析（设计§7）：批量内相对排名 + 评分漂移检测。

- 相对排名：批量内按终分排序，给出名次（并列同名次）与百分位。
- 漂移检测：用**AI 初评分 vs 人工复核终分**的系统性偏移作信号（reuse 现有复核数据，无需额外标注）。
  某评分项人均 `final - ai` 偏移超阈值且有足够调整样本 → 标记（AI 偏严/偏宽），提示校准。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from backend.app.core.config import settings
from backend.app.db.models import GradingBatch
from backend.app.db.models import Paper
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun

DRIFT_MIN_ADJUSTED = 2


def _latest_run(paper):
    runs = [run for run in paper.scoring_runs if run.ai_total_score is not None or run.final_total_score is not None]
    return max(runs, key=lambda run: run.created_at) if runs else None


def batch_ranking(db: Session, batch_id: str):
    batch = db.scalar(
        select(GradingBatch)
        .where(GradingBatch.id == batch_id)
        .options(selectinload(GradingBatch.papers).selectinload(Paper.scoring_runs))
    )
    if batch is None:
        raise ValueError("batch not found")

    scored = []
    unscored = []
    for paper in batch.papers:
        run = _latest_run(paper)
        if run is None:
            unscored.append(paper.id)
            continue
        total = run.final_total_score if run.final_total_score is not None else run.ai_total_score
        scored.append({"paper_id": paper.id, "student_name": paper.student_name, "title": paper.title, "total": round(float(total or 0), 2)})

    scored.sort(key=lambda row: row["total"], reverse=True)
    count = len(scored)
    rank = 0
    previous = None
    for index, row in enumerate(scored):
        if previous is None or row["total"] != previous:
            rank = index + 1  # 标准竞赛并列名次
            previous = row["total"]
        row["rank"] = rank
        row["percentile"] = round(100 * (count - index) / count, 1) if count else None

    return {
        "batch_id": batch_id,
        "scored_count": count,
        "unscored_count": len(unscored),
        "ranking": scored,
        "unscored_paper_ids": unscored,
    }


def score_drift(db: Session, batch_id: str):
    batch = db.scalar(
        select(GradingBatch)
        .where(GradingBatch.id == batch_id)
        .options(
            selectinload(GradingBatch.papers)
            .selectinload(Paper.scoring_runs)
            .selectinload(ScoringRun.items)
            .selectinload(ScoreItem.criterion)
        )
    )
    if batch is None:
        raise ValueError("batch not found")

    buckets = {}
    for paper in batch.papers:
        run = _latest_run(paper)
        if run is None:
            continue
        for item in run.items:
            code = item.criterion.code if item.criterion else item.criterion_id
            bucket = buckets.setdefault(
                code,
                {"criterion_code": code, "criterion_name": item.criterion.name if item.criterion else "", "ai": [], "final": []},
            )
            ai_score = float(item.ai_score)
            final_score = float(item.final_score if item.final_score is not None else item.ai_score)
            bucket["ai"].append(ai_score)
            bucket["final"].append(final_score)

    threshold = float(settings.SCORING_DRIFT_BIAS_THRESHOLD)
    criteria = []
    for bucket in buckets.values():
        count = len(bucket["ai"])
        deltas = [f - a for a, f in zip(bucket["ai"], bucket["final"])]
        adjusted = sum(1 for delta in deltas if abs(delta) > 0.001)
        bias = round(sum(deltas) / count, 3) if count else 0.0
        flagged = abs(bias) >= threshold and adjusted >= DRIFT_MIN_ADJUSTED
        if abs(bias) < 0.001:
            direction = "无明显偏移"
        elif bias > 0:
            direction = "AI 偏严（人工普遍上调）"
        else:
            direction = "AI 偏宽（人工普遍下调）"
        criteria.append(
            {
                "criterion_code": bucket["criterion_code"],
                "criterion_name": bucket["criterion_name"],
                "n": count,
                "n_adjusted": adjusted,
                "mean_ai": round(sum(bucket["ai"]) / count, 2) if count else 0.0,
                "mean_final": round(sum(bucket["final"]) / count, 2) if count else 0.0,
                "bias": bias,
                "flagged": flagged,
                "direction": direction,
            }
        )

    criteria.sort(key=lambda row: abs(row["bias"]), reverse=True)
    return {"batch_id": batch_id, "threshold": threshold, "criteria": criteria}
