from types import SimpleNamespace

from backend.app.services.checkers.findings_checker import findings_for_dimension
from backend.app.services.checkers.findings_checker import is_findings_enabled
from backend.app.services.checkers.findings_checker import score_from_findings


def _crit(dimension="格式", rules=None, ctype="deterministic", code="F1", max_score=10):
    return SimpleNamespace(
        id="c-" + code,
        code=code,
        name="格式",
        max_score=max_score,
        criterion_type=ctype,
        dimension=dimension,
        deduction_rules_structured=rules or [],
    )


def test_is_findings_enabled_requires_type_dimension_and_rules():
    assert is_findings_enabled(_crit(rules=[{"match": "字号", "points": 2}]))
    assert not is_findings_enabled(_crit(rules=[]))  # 无结构化规则
    assert not is_findings_enabled(_crit(dimension=None, rules=[{"match": "x", "points": 1}]))  # 无维度
    assert not is_findings_enabled(_crit(rules=[{"match": "x", "points": 1}], ctype="llm_judgment"))  # 非确定性


def test_findings_for_dimension_maps_by_category():
    findings = [
        {"kind": "format_mismatch", "severity": "warning", "message": "字号 不符"},
        {"kind": "citation_missing_entry", "severity": "warning", "message": "引用[2]无对应条目"},
    ]
    assert len(findings_for_dimension("格式", findings)) == 1
    assert len(findings_for_dimension("规范性", findings)) == 1


def test_score_from_findings_deducts_warning_by_rule_only():
    criterion = _crit(dimension="格式", rules=[{"match": "字号", "points": 2}])
    findings = [
        {"kind": "format_mismatch", "severity": "warning", "message": "字号(磅) 不符：应为14，实际12", "location": "全文"},
        {"kind": "format_unknown", "severity": "info", "message": "无法确定行距"},  # info 不判错
    ]
    out = score_from_findings(criterion, findings, criterion.deduction_rules_structured)
    assert out["score"] == 8.0
    assert len(out["deduction_items"]) == 1
    assert out["deduction_items"][0]["rule_ref"] == "F1"
    assert out["deduction_items"][0]["points"] == 2.0
    assert findings[0]["deducted_by"] == "F1"  # warning 已标记扣分
    assert "deducted_by" not in findings[1]  # info 未扣


def test_score_from_findings_no_rule_match_keeps_full_score():
    criterion = _crit(dimension="格式", rules=[{"match": "页边距", "points": 2}])
    findings = [{"kind": "format_mismatch", "severity": "warning", "message": "字号 不符"}]
    out = score_from_findings(criterion, findings, criterion.deduction_rules_structured)
    assert out["score"] == 10.0  # 维度相关但无规则匹配 → 不扣
    assert out["deduction_items"] == []
