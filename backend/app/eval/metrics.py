"""评估指标（设计§15.1）。

自动作文/论文评分的标准一致性指标是 **QWK（二次加权 Kappa）**，衡量系统分与教师分的有序一致性；
辅以总分 MAE/RMSE 与"同档/相邻档"一致率。本模块为纯函数，便于严格单测与在 CI 中作回归门槛。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from decimal import ROUND_HALF_UP
import math

from backend.app.services.scoring.core.policy import ScoringPolicy
from backend.app.services.scoring.core.policy import compile_scoring_policy


@dataclass(frozen=True, slots=True)
class EvaluationGradeBand:
    label: str
    minimum: str


@dataclass(frozen=True, slots=True)
class EvaluationGradeScale:
    """Profile-neutral grade mapping used by QWK and agreement metrics."""

    total_score: str
    basis: str
    bands: tuple[EvaluationGradeBand, ...]
    rounding_mode: str | None = None
    rounding_digits: int | None = None

    @classmethod
    def from_policy(cls, value):
        if isinstance(value, ScoringPolicy):
            policy = value
        elif isinstance(value, Mapping):
            aggregation = value.get("aggregation")
            if not isinstance(aggregation, Mapping):
                raise ValueError("scoring policy aggregation is required")
            policy = compile_scoring_policy(
                value,
                total_score=aggregation.get("total_score"),
            )
        else:
            raise TypeError("scoring policy must be a mapping or ScoringPolicy")
        return cls(
            total_score=_decimal_text(policy.aggregation.total_score),
            basis=policy.grade_scale.basis,
            bands=tuple(
                EvaluationGradeBand(
                    label=band.label,
                    minimum=_decimal_text(band.minimum),
                )
                for band in policy.grade_scale.bands
            ),
            rounding_mode=policy.rounding.mode,
            rounding_digits=policy.rounding.digits,
        )

    @property
    def labels(self):
        return tuple(band.label for band in reversed(self.bands))

    def normalize(self, value):
        result = Decimal(str(value))
        if self.rounding_digits is None:
            return result
        if self.rounding_mode != "half_up":
            raise ValueError("evaluation grade scale only supports half_up rounding")
        quantum = Decimal("1").scaleb(-self.rounding_digits)
        return result.quantize(quantum, rounding=ROUND_HALF_UP)

    def ordinal(self, value):
        total = self.normalize(value)
        comparable = total
        if self.basis == "percentage":
            comparable = total / Decimal(self.total_score) * Decimal("100")
        for index, band in enumerate(self.bands):
            if comparable >= Decimal(band.minimum):
                return len(self.bands) - index - 1
        return 0

    def to_mapping(self):
        return {
            "basis": self.basis,
            "bands": [
                {"label": band.label, "minimum": band.minimum}
                for band in self.bands
            ],
        }


def _decimal_text(value):
    decimal = Decimal(str(value))
    if decimal == 0:
        return "0"
    return format(decimal.normalize(), "f")


DEFAULT_GRADE_SCALE = EvaluationGradeScale(
    total_score="100",
    basis="raw_score",
    bands=(
        EvaluationGradeBand("优秀", "90"),
        EvaluationGradeBand("良好", "80"),
        EvaluationGradeBand("中等", "70"),
        EvaluationGradeBand("及格", "60"),
        EvaluationGradeBand("不及格", "0"),
    ),
)


def grade_ordinal(total, grade_scale=None):
    """总分 → 等级序数（与 scoring.rules 的等级一致）：不及格0 / 及格1 / 中等2 / 良好3 / 优秀4。"""
    return (grade_scale or DEFAULT_GRADE_SCALE).ordinal(total)


GRADE_LABELS = ["不及格", "及格", "中等", "良好", "优秀"]
NUM_GRADES = len(GRADE_LABELS)


def quadratic_weighted_kappa(y_true, y_pred, min_rating=None, max_rating=None):
    """二次加权 Kappa。y_true/y_pred 为整数序数评分（如等级 0..4）。
    返回 -1..1；None 表示样本为空。完全一致=1，等于随机=0。"""
    if len(y_true) != len(y_pred):
        raise ValueError("y_true and y_pred must have the same length")
    if not y_true:
        return None

    true = [int(round(v)) for v in y_true]
    pred = [int(round(v)) for v in y_pred]
    if min_rating is None:
        min_rating = min(min(true), min(pred))
    if max_rating is None:
        max_rating = max(max(true), max(pred))
    num = max_rating - min_rating + 1
    if num <= 1:
        # 只有单一评级，无方差：退化为完全一致。
        return 1.0

    observed = [[0] * num for _ in range(num)]
    hist_true = [0] * num
    hist_pred = [0] * num
    for a, b in zip(true, pred):
        observed[a - min_rating][b - min_rating] += 1
        hist_true[a - min_rating] += 1
        hist_pred[b - min_rating] += 1

    n = len(true)
    numerator = 0.0
    denominator = 0.0
    for i in range(num):
        for j in range(num):
            weight = ((i - j) ** 2) / ((num - 1) ** 2)
            expected = hist_true[i] * hist_pred[j] / n
            numerator += weight * observed[i][j]
            denominator += weight * expected
    if denominator == 0:
        return 1.0
    return 1.0 - numerator / denominator


def mae(y_true, y_pred):
    if len(y_true) != len(y_pred):
        raise ValueError("y_true and y_pred must have the same length")
    if not y_true:
        return None
    return sum(abs(float(a) - float(b)) for a, b in zip(y_true, y_pred)) / len(y_true)


def rmse(y_true, y_pred):
    if len(y_true) != len(y_pred):
        raise ValueError("y_true and y_pred must have the same length")
    if not y_true:
        return None
    return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(y_true, y_pred)) / len(y_true))


def grade_confusion(true_totals, pred_totals, grade_scale=None):
    """同档/相邻档混淆矩阵（行=人工等级，列=系统等级）。"""
    if len(true_totals) != len(pred_totals):
        raise ValueError("true_totals and pred_totals must have the same length")
    scale = grade_scale or DEFAULT_GRADE_SCALE
    size = len(scale.labels)
    matrix = [[0] * size for _ in range(size)]
    for true_total, pred_total in zip(true_totals, pred_totals):
        matrix[grade_ordinal(true_total, scale)][grade_ordinal(pred_total, scale)] += 1
    return matrix


def exact_agreement(true_totals, pred_totals, grade_scale=None):
    """同档一致率。"""
    if len(true_totals) != len(pred_totals):
        raise ValueError("true_totals and pred_totals must have the same length")
    if not true_totals:
        return None
    scale = grade_scale or DEFAULT_GRADE_SCALE
    same = sum(
        1
        for t, p in zip(true_totals, pred_totals)
        if grade_ordinal(t, scale) == grade_ordinal(p, scale)
    )
    return same / len(true_totals)


def adjacent_agreement(true_totals, pred_totals, grade_scale=None):
    """相邻档（差≤1档）一致率。"""
    if len(true_totals) != len(pred_totals):
        raise ValueError("true_totals and pred_totals must have the same length")
    if not true_totals:
        return None
    scale = grade_scale or DEFAULT_GRADE_SCALE
    close = sum(
        1
        for t, p in zip(true_totals, pred_totals)
        if abs(grade_ordinal(t, scale) - grade_ordinal(p, scale)) <= 1
    )
    return close / len(true_totals)
