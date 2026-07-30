"""评估编排（设计§15）：把"系统预测"与"人工真值"配对，算总体 + 逐维度指标，并提供回归门禁。

- 数据解耦：evaluate() 只吃配对好的预测/真值，不关心分数怎么来的 —— 便于单测与离线复算。
- 回归门禁：assert_no_regression() 用于 CI，改 prompt/模型/Rubric 编译后 QWK 不得低于基线（设计§15.2）。
"""

import json
from dataclasses import dataclass
from dataclasses import field

from backend.app.eval import metrics


@dataclass
class EvalSample:
    """人工已评真值（ground truth）。"""

    key: str
    human_total: float
    human_items: dict = field(default_factory=dict)  # {criterion_code: score}


@dataclass
class EvalPrediction:
    """系统评分结果。"""

    key: str
    system_total: float
    system_items: dict = field(default_factory=dict)  # {criterion_code: score}


def load_dataset(path):
    """从 JSON 加载留出集：[{"key"/"paper_id", "human_total", "human_items": {...}}]。"""
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    samples = []
    for index, row in enumerate(raw):
        key = row.get("key") or row.get("paper_id") or row.get("id")
        if not key:
            raise ValueError("dataset row %d is missing key/paper_id/id" % index)
        try:
            sample = EvalSample(
                key=str(key),
                human_total=float(row["human_total"]),
                human_items={str(k): float(v) for k, v in (row.get("human_items") or {}).items()},
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("dataset row %d (key=%s) is invalid: %s" % (index, key, exc)) from exc
        samples.append(sample)
    return samples


def evaluate(predictions, truths):
    """配对后计算指标，返回可序列化报告。"""
    truth_by_key = {sample.key: sample for sample in truths}
    paired = [(pred, truth_by_key[pred.key]) for pred in predictions if pred.key in truth_by_key]

    true_totals = [truth.human_total for _, truth in paired]
    pred_totals = [pred.system_total for pred, _ in paired]

    true_grades = [metrics.grade_ordinal(t) for t in true_totals]
    pred_grades = [metrics.grade_ordinal(t) for t in pred_totals]

    return {
        "n": len(paired),
        "unmatched_predictions": [pred.key for pred in predictions if pred.key not in truth_by_key],
        "qwk": metrics.quadratic_weighted_kappa(true_grades, pred_grades, 0, metrics.NUM_GRADES - 1),
        "mae": metrics.mae(true_totals, pred_totals),
        "rmse": metrics.rmse(true_totals, pred_totals),
        "exact_grade_agreement": metrics.exact_agreement(true_totals, pred_totals),
        "adjacent_grade_agreement": metrics.adjacent_agreement(true_totals, pred_totals),
        "grade_confusion": metrics.grade_confusion(true_totals, pred_totals),
        "grade_labels": metrics.GRADE_LABELS,
        "per_criterion": _per_criterion_errors(paired),
    }


def _per_criterion_errors(paired):
    """逐维度（评分项）误差：定位哪些项系统性偏宽/偏严（设计§15.1）。"""
    buckets = {}
    for pred, truth in paired:
        for code, human_score in truth.human_items.items():
            if code in pred.system_items:
                buckets.setdefault(code, []).append((float(human_score), float(pred.system_items[code])))
    result = {}
    for code, pairs in buckets.items():
        true_scores = [t for t, _ in pairs]
        pred_scores = [p for _, p in pairs]
        bias = sum(p - t for t, p in pairs) / len(pairs)  # >0 偏宽，<0 偏严
        result[code] = {
            "n": len(pairs),
            "mae": metrics.mae(true_scores, pred_scores),
            "bias": bias,
        }
    return result


def baseline_from_report(report):
    """抽取普通实验的聚合基线；它明确不具备发布门禁资格。"""
    keys = ("qwk", "mae", "rmse", "exact_grade_agreement", "adjacent_grade_agreement")
    return {
        "schema": "paper-grading/evaluation-aggregate-baseline@1",
        "provenance": "aggregate_only_non_release",
        "reproducible": False,
        "gating_eligible": False,
        **{key: report.get(key) for key in keys},
    }


def assert_no_regression(report, baseline, qwk_drop_tol=0.02, mae_rise_tol=1.0):
    """回归门禁：返回问题列表，空列表=通过。用于 CI（设计§15.2）。"""
    issues = []
    base_qwk = baseline.get("qwk")
    if base_qwk is not None and report.get("qwk") is not None:
        if report["qwk"] < base_qwk - qwk_drop_tol:
            issues.append("QWK 回退：%.3f < 基线 %.3f - 容差 %.3f" % (report["qwk"], base_qwk, qwk_drop_tol))
    base_mae = baseline.get("mae")
    if base_mae is not None and report.get("mae") is not None:
        if report["mae"] > base_mae + mae_rise_tol:
            issues.append("MAE 上升：%.3f > 基线 %.3f + 容差 %.3f" % (report["mae"], base_mae, mae_rise_tol))
    return issues
