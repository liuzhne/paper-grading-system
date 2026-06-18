"""评估指标（设计§15.1）。

自动作文/论文评分的标准一致性指标是 **QWK（二次加权 Kappa）**，衡量系统分与教师分的有序一致性；
辅以总分 MAE/RMSE 与"同档/相邻档"一致率。本模块为纯函数，便于严格单测与在 CI 中作回归门槛。
"""

import math


def grade_ordinal(total):
    """总分 → 等级序数（与 scoring.rules 的等级一致）：不及格0 / 及格1 / 中等2 / 良好3 / 优秀4。"""
    value = float(total)
    if value >= 90:
        return 4
    if value >= 80:
        return 3
    if value >= 70:
        return 2
    if value >= 60:
        return 1
    return 0


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


def grade_confusion(true_totals, pred_totals):
    """同档/相邻档混淆矩阵（行=人工等级，列=系统等级）。"""
    if len(true_totals) != len(pred_totals):
        raise ValueError("true_totals and pred_totals must have the same length")
    matrix = [[0] * NUM_GRADES for _ in range(NUM_GRADES)]
    for true_total, pred_total in zip(true_totals, pred_totals):
        matrix[grade_ordinal(true_total)][grade_ordinal(pred_total)] += 1
    return matrix


def exact_agreement(true_totals, pred_totals):
    """同档一致率。"""
    if len(true_totals) != len(pred_totals):
        raise ValueError("true_totals and pred_totals must have the same length")
    if not true_totals:
        return None
    same = sum(1 for t, p in zip(true_totals, pred_totals) if grade_ordinal(t) == grade_ordinal(p))
    return same / len(true_totals)


def adjacent_agreement(true_totals, pred_totals):
    """相邻档（差≤1档）一致率。"""
    if len(true_totals) != len(pred_totals):
        raise ValueError("true_totals and pred_totals must have the same length")
    if not true_totals:
        return None
    close = sum(1 for t, p in zip(true_totals, pred_totals) if abs(grade_ordinal(t) - grade_ordinal(p)) <= 1)
    return close / len(true_totals)
