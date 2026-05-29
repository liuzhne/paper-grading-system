from decimal import Decimal


GRADE_RULES = [
    (90, 100, "优秀"),
    (80, 89.999, "良好"),
    (70, 79.999, "中等"),
    (60, 69.999, "及格"),
    (0, 59.999, "不及格"),
]


def as_float(value):
    if isinstance(value, Decimal):
        return float(value)
    if value is None:
        return 0.0
    return float(value)


def calculate_total_score(items, total_score=100):
    total = 0.0
    for item in items:
        score = as_float(item.final_score if item.final_score is not None else item.ai_score)
        max_score = as_float(item.max_score)
        if score < 0 or score > max_score:
            raise ValueError("score out of range for item %s" % getattr(item, "id", "unknown"))
        total += score
    return round(min(total, as_float(total_score)), 2)


def match_grade(total):
    for lower, upper, label in GRADE_RULES:
        if lower <= total <= upper:
            return label
    return "未评级"


def is_near_grade_boundary(total, tolerance=2):
    return any(abs(total - boundary) <= tolerance for boundary in [60, 70, 80, 90])


def need_manual_review(total, items, parse_quality=None):
    if total < 60 or is_near_grade_boundary(total):
        return True
    if parse_quality is not None and as_float(parse_quality) < 0.65:
        return True
    for item in items:
        if not item.evidence_sufficient:
            return True
        if item.need_manual_review:
            return True
        if item.confidence is not None and as_float(item.confidence) < 0.65:
            return True
    return False

