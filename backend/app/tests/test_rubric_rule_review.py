"""阶段 6.5：规则审查——代码前置检查、审查对象选择、LLM 审查与引用核验。"""

import pytest

from backend.app.services.rubric_import.review import REVIEW_PROMPT_VERSION
from backend.app.services.rubric_import.review import ReviewError
from backend.app.services.rubric_import.review import build_bundles
from backend.app.services.rubric_import.review import precheck
from backend.app.services.rubric_import.review import review_fingerprint
from backend.app.services.rubric_import.review import review_rules
from backend.app.tests.test_rubric_unit_classifier import FakeScorer


def _criterion(code, max_score, texts, rules, name=None):
    return {"code": code, "name": name or code, "max_score": max_score, "deduction_rules": texts,
            "deduction_rules_structured": rules}


CRITERIA = [
    _criterion("C01", 30, ["格式错误、图表不清、引用不规范各扣2分"],
               [{"match": "格式错误", "points": 2, "source_refs": ["/criteria/C01/deduction_rules/0"]}]),
    _criterion("C02", 10, ["文献少于20篇扣2分", "意义笼统扣1-3分"],
               [{"match": "文献少于20篇", "points": 2}, {"match": "意义笼统", "points": 3},
                {"match": "文献少于20篇", "points": 1}]),
    _criterion("C03", 5, ["超长扣8分"], [{"match": "超", "points": 8}]),
    _criterion("C04", 20, ["格式错误扣1分"], [{"trigger": "格式错误", "points": 1, "source": "ai_inferred"}]),
]


def _codes(issues):
    return {(i["code"], i.get("criterion_code")) for i in issues}


def test_precheck_finds_deterministic_problems():
    issues = precheck(CRITERIA, total_score=70)
    assert _codes(issues) == {
        ("TOTAL_MISMATCH", None),                # 30+10+5+20 = 65 ≠ 70
        ("MULTI_JUDGEMENT_SINGLE_RULE", "C01"),  # “各扣”只拆出一条
        ("DUPLICATE_MATCH", "C02"),
        ("POINTS_EXCEED_MAX", "C03"),
        ("MATCH_TOO_SHORT", "C03"),
    }


def test_precheck_reports_source_numbers_no_rule_uses():
    issues = precheck([_criterion("C09", 10, ["每处扣0.5分，最多扣5分"], [{"match": "每处", "points": 0.5}])],
                      total_score=10)
    assert ("SOURCE_NUMBER_UNUSED", "C09") in _codes(issues)
    capped = precheck([_criterion("C09", 10, ["每处扣0.5分，最多扣5分"],
                                  [{"match": "每处", "points": 0.5, "group_cap_points": 5}])], total_score=10)
    assert ("SOURCE_NUMBER_UNUSED", "C09") not in _codes(capped)


def test_bundles_use_stable_ids_and_priority_selection():
    bundles = build_bundles(CRITERIA, scope="priority")
    by_code = {b["criterion"]["code"]: b for b in bundles}
    # C01 多判断、C02 区间分值与多规则、C04 AI 规则且 match 与 C01 共用 → 优先；C03 不是
    assert set(by_code) == {"C01", "C02", "C04"}
    assert by_code["C02"]["sources"] == [{"id": "S1", "text": "文献少于20篇扣2分"}, {"id": "S2", "text": "意义笼统扣1-3分"}]
    assert by_code["C02"]["rules"][0] == {"id": "R1", "match": "文献少于20篇", "points": 2.0, "cap": None,
                                          "repeat_policy": None, "source_ids": []}
    assert by_code["C01"]["rules"][0]["source_ids"] == ["S1"]
    assert {b["criterion"]["code"] for b in build_bundles(CRITERIA, scope="all")} == {"C01", "C02", "C03", "C04"}


def _finding(**overrides):
    return {"type": "distortion", "rule_ids": ["R1"], "source_ids": ["S1"], "quote": "各扣2分",
            "problem": "原文三项各扣 2 分，只拆出一条", "example": "", "severity": "high", **overrides}


def test_review_keeps_verified_findings_and_discards_invalid_ones():
    bundle_c01 = lambda payload: {"issues": [  # noqa: E731
        _finding(),
        _finding(quote="原文里没有的话"),
        _finding(rule_ids=["R9"]),
        _finding(type="match_too_broad", example=""),
        _finding(type="banana"),
        _finding(type="match_too_broad", rule_ids=["R1"], quote="格式错误", example="“格式错误较少”也会命中"),
    ]}
    scorer = FakeScorer(bundle_c01, {"issues": []})
    result = review_rules([build_bundles(CRITERIA, scope="all")[0]], scorer)
    assert [(f["criterion_code"], f["type"]) for f in result["findings"]] == [
        ("C01", "distortion"), ("C01", "match_too_broad")]
    assert sorted(d["error"] for d in result["discarded"]) == [
        "invalid_type", "missing_example", "quote_not_in_source", "unknown_rule"]
    assert result["findings"][0]["status"] == "open"
    assert result["prompt_version"] == REVIEW_PROMPT_VERSION
    instructions = scorer.calls[0][0]
    assert "宁可漏报" in instructions and "不可信" in instructions


def test_cross_review_checks_summaries_and_quotes_against_matches():
    bundles = build_bundles(CRITERIA, scope="priority")
    per_bundle = [{"issues": []}] * len(bundles)
    cross = {"issues": [
        {"type": "cross_duplicate", "rule_ids": ["C01.R1", "C04.R1"], "source_ids": [], "quote": "格式错误",
         "problem": "同一问题在两个评分项重复扣分", "example": "", "severity": "medium"},
        {"type": "cross_duplicate", "rule_ids": ["C01.R1", "C99.R1"], "source_ids": [], "quote": "格式错误",
         "problem": "x", "example": "", "severity": "low"},
    ]}
    scorer = FakeScorer(*per_bundle, cross)
    result = review_rules(bundles, scorer)
    assert [f["type"] for f in result["findings"]] == ["cross_duplicate"]
    summary = scorer.calls[-1][1]["criteria"]
    assert {item["code"] for item in summary} == {"C01", "C02", "C04"}


def test_mock_is_refused_and_fingerprint_tracks_rule_changes():
    with pytest.raises(ReviewError):
        review_rules(build_bundles(CRITERIA, scope="all"), type("M", (), {"provider": "mock"})())
    base = review_fingerprint(build_bundles(CRITERIA, scope="all"))
    changed = [dict(c) for c in CRITERIA]
    changed[2] = _criterion("C03", 5, ["超长扣8分"], [{"match": "超长", "points": 5}])
    assert base != review_fingerprint(build_bundles(changed, scope="all"))
