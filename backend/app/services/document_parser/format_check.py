"""格式比对（设计§9）：被评论文有效格式 ↔ 模板期望格式规格（FormatSpec）。

三态处理（关键）：
- 模板未规定该项（expected=None）→ 不检查；
- 论文该项无法确定（actual=None，unknown 第三态）→ **不判错**，仅 info 提示人工核对；
- 两者皆有且不一致 → warning。

产出结构化"格式发现"，进报告/评分运行；是否据此扣分由评分项/人工决定（人在回路）。
"""

FIELDS = [
    ("body_font_ascii", "西文字体"),
    ("body_font_east_asian", "中文字体"),
    ("body_font_size_pt", "字号(磅)"),
    ("line_spacing", "行距(倍)"),
]
NUMERIC_FIELDS = {"body_font_size_pt", "line_spacing"}


def compare_format(actual, expected):
    actual = actual or {}
    expected = expected or {}
    findings = []
    for key, label in FIELDS:
        want = expected.get(key)
        if want is None:
            continue  # 模板未规定该项 → 不检查
        got = actual.get(key)
        if got is None:
            findings.append(
                _finding("format_unknown", "info", "无法确定%s（模板要求：%s），建议人工核对" % (label, _fmt(want)), key, want, None)
            )
            continue
        if _mismatch(key, got, want):
            findings.append(
                _finding("format_mismatch", "warning", "%s 不符：应为 %s，实际 %s" % (label, _fmt(want), _fmt(got)), key, want, got)
            )
    return findings


def _mismatch(key, got, want):
    if key in NUMERIC_FIELDS:
        try:
            return abs(float(got) - float(want)) > 0.01
        except (TypeError, ValueError):
            return str(got) != str(want)
    return str(got).strip() != str(want).strip()


def _fmt(value):
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def _finding(kind, severity, message, field, expected, actual):
    return {"kind": kind, "severity": severity, "message": message, "field": field, "expected": expected, "actual": actual}
