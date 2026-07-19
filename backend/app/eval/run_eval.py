"""离线评估入口（设计§15）：对留出集中的论文用系统评分，与人工真值比对，产出报告。

前提：数据集里的 key 指向库中已存在、已解析的 paper_id。
报告落 storage/eval/eval_report_*.json；传入 baseline 即做回归门禁（CI 用）。

示例：
    from backend.app.db.session import SessionLocal
    with SessionLocal() as db:
        report = run_evaluation(db, "backend/app/eval/sample_dataset.json")
"""

import json
from datetime import datetime
from datetime import timezone

from backend.app.core.config import settings
from backend.app.eval.runner import EvalPrediction
from backend.app.eval.runner import assert_no_regression
from backend.app.eval.runner import evaluate
from backend.app.eval.runner import load_dataset
from backend.app.services.scoring.engine import score_paper


def run_evaluation(db, dataset_path, scorer=None, baseline=None):
    samples = load_dataset(dataset_path)
    predictions = []
    errors = []
    completed_runs = 0
    review_required_runs = 0
    blocked_runs = 0
    for sample in samples:
        try:
            run = score_paper(db, sample.key, scorer=scorer)
        except Exception as exc:  # 单篇失败不应中断整批评估
            errors.append({"key": sample.key, "error": str(exc)})
            continue
        completed_runs += 1
        review_required_runs += int(bool(run.need_manual_review))
        if run.final_total_score is None:
            blocked_runs += 1
            errors.append(
                {
                    "key": sample.key,
                    "error": "自动总分因 invalid/blocked 评分项为空；该样本未进入指标",
                }
            )
            continue
        predictions.append(
            EvalPrediction(
                key=sample.key,
                system_total=float(run.final_total_score),
                system_items=_items_by_code(run),
            )
        )

    report = evaluate(predictions, samples)
    report["errors"] = errors
    report["completed_runs"] = completed_runs
    report["review_rate"] = (
        review_required_runs / completed_runs if completed_runs else None
    )
    report["blocked_rate"] = blocked_runs / completed_runs if completed_runs else None
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    if baseline is not None:
        report["regression_issues"] = assert_no_regression(report, baseline)
    report["report_path"] = str(_write_report(report))
    return report


def _items_by_code(run):
    result = {}
    for item in run.items:
        code = getattr(item.criterion, "code", None) if item.criterion else None
        score = item.final_score if item.final_score is not None else item.ai_score
        if code and score is not None:  # 两个分都缺失则跳过，避免 float(None) 崩溃
            result[code] = float(score)
    return result


def _write_report(report):
    out_dir = settings.STORAGE_ROOT / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ("eval_report_%s.json" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    return path
