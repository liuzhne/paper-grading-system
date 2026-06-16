"""L2 批量分析（设计§7/§15.2）：批量内相对排名 + 评分漂移检测 + 上线抽样复核监控。

- 相对排名：批量内按终分排序，给出名次（并列同名次）与百分位。
- 漂移检测：用**AI 初评分 vs 人工复核终分**的系统性偏移作信号（reuse 现有复核数据，无需额外标注）。
  某评分项人均 `final - ai` 偏移超阈值且有足够调整样本 → 标记（AI 偏严/偏宽），提示校准。
- 抽样复核（§15.2 上线监控）：每批确定性抽取一部分论文做人工抽检（系统已标记需复核的必抽，其余
  按 hash 稳定抽样补足比例）；漂移监控在抽检覆盖率足够时才信任漂移信号。
"""

import hashlib
import math

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from backend.app.core.config import settings
from backend.app.db.models import GradingBatch
from backend.app.db.models import Paper
from backend.app.db.models import ReviewLog
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


def _stable_fraction(key):
    """把任意字符串确定性映射到 [0,1)，用于可复现抽样（设计哲学#5 幂等）。"""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0x100000000


def _scored_runs(batch):
    pairs = []
    for paper in batch.papers:
        run = _latest_run(paper)
        if run is not None:
            pairs.append((paper, run))
    return pairs


def review_sample(db: Session, batch_id: str, ratio: float = None, seed: str = ""):
    """上线抽样复核（§15.2）：选出本批需人工抽检的论文。

    系统已标记 `need_manual_review` 的必抽（must-review），其余按 hash 稳定抽样补足到 `ratio` 覆盖。
    确定性：同 batch+seed 反复调用结果一致（可复现），不依赖随机种子状态。
    """
    if ratio is None:
        ratio = settings.MONITORING_REVIEW_SAMPLE_RATIO
    ratio = max(0.0, min(float(ratio), 1.0))

    batch = db.scalar(
        select(GradingBatch)
        .where(GradingBatch.id == batch_id)
        .options(selectinload(GradingBatch.papers).selectinload(Paper.scoring_runs))
    )
    if batch is None:
        raise ValueError("batch not found")

    pairs = _scored_runs(batch)
    total = len(pairs)
    target = math.ceil(total * ratio) if total else 0

    selected = []
    flagged = [(paper, run) for paper, run in pairs if run.need_manual_review]
    for paper, run in flagged:
        selected.append(_sample_row(paper, run, "系统标记需复核"))

    remaining = [(paper, run) for paper, run in pairs if not run.need_manual_review]
    remaining.sort(key=lambda pr: _stable_fraction("%s:%s" % (seed, pr[1].id)))
    for paper, run in remaining:
        if len(selected) >= target:
            break
        selected.append(_sample_row(paper, run, "抽样复核"))

    return {
        "batch_id": batch_id,
        "ratio": ratio,
        "total": total,
        "target": target,
        "flagged_count": len(flagged),
        "sample_size": len(selected),
        "selected": selected,
    }


def _sample_row(paper, run, reason):
    return {
        "paper_id": paper.id,
        "run_id": run.id,
        "student_name": paper.student_name,
        "title": paper.title,
        "reason": reason,
    }


def drift_monitor(db: Session, batch_id: str):
    """漂移监控（§15.2）：在 `score_drift` 上叠加人工复核覆盖率 + 总体结论。

    覆盖率不足时漂移信号不可信（样本太少），提示先按 `review_sample` 抽检；覆盖率够且有评分项被标记
    → 提示校准；否则未见显著漂移。
    """
    drift = score_drift(db, batch_id)

    batch = db.scalar(
        select(GradingBatch)
        .where(GradingBatch.id == batch_id)
        .options(selectinload(GradingBatch.papers).selectinload(Paper.scoring_runs))
    )
    pairs = _scored_runs(batch)
    scored = len(pairs)
    # 人工复核信号取 ReviewLog（final_score 在评分时即回填，不能作复核标志）。
    run_ids = [run.id for _, run in pairs]
    reviewed_ids = set(
        db.scalars(select(ReviewLog.scoring_run_id).where(ReviewLog.scoring_run_id.in_(run_ids))).all()
    ) if run_ids else set()
    reviewed = sum(1 for _, run in pairs if run.id in reviewed_ids)
    coverage = round(reviewed / scored, 3) if scored else 0.0
    min_coverage = float(settings.MONITORING_MIN_REVIEW_COVERAGE)
    flagged_codes = [c["criterion_code"] for c in drift["criteria"] if c["flagged"]]

    if scored == 0:
        verdict = "无评分数据"
    elif coverage < min_coverage:
        verdict = "复核样本不足，漂移信号暂不可信（建议先按抽样复核）"
    elif flagged_codes:
        verdict = "检测到系统性漂移，建议校准"
    else:
        verdict = "未见显著漂移"

    return {
        "batch_id": batch_id,
        "scored_count": scored,
        "reviewed_count": reviewed,
        "review_coverage": coverage,
        "min_coverage": min_coverage,
        "threshold": drift["threshold"],
        "flagged_criteria": flagged_codes,
        "verdict": verdict,
        "criteria": drift["criteria"],
    }
