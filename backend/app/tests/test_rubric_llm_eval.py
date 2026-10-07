"""离线评估：注入错误测试集与召回/精确率计算（真实模型评估不进入默认 pytest）。"""

import pytest

from backend.app.eval.rubric_llm_eval import default_classifier_cases
from backend.app.eval.rubric_llm_eval import default_review_cases
from backend.app.eval.rubric_llm_eval import evaluate_classifier
from backend.app.eval.rubric_llm_eval import evaluate_review
from backend.app.tests.test_rubric_unit_classifier import FakeScorer


def test_review_cases_cover_each_injected_error_kind():
    cases = default_review_cases()
    assert {case["injection"] for case in cases} == {"drop_split_rule", "change_points", "each_to_total", "broaden_match"}
    for case in cases:
        assert case["expected"]["criterion_code"]
        assert case["expected"]["types"]


class Oracle:
    """按注入的错误如实报告（用来验证指标计算，而非模型能力）。"""

    provider = "oracle"
    model_name = "oracle"

    def __init__(self, cases):
        self.by_code = {c["expected"]["criterion_code"]: c for c in cases}

    def complete_json(self, instructions, payload):
        if "criteria" in payload:
            return {"issues": []}
        code = payload["criterion"]["code"]
        case = self.by_code.get(code)
        if case is None:
            return {"issues": []}
        source = payload["sources"][0]
        kind = sorted(case["expected"]["types"])[0]
        return {"issues": [{"type": kind, "rule_ids": ["R1"], "source_ids": [source["id"]], "quote": source["text"][:3],
                            "problem": "注入错误", "example": "例：任何文本" , "severity": "high"}]}


def test_perfect_reviewer_scores_full_recall_and_precision():
    cases = default_review_cases()
    report = evaluate_review(Oracle(cases), cases)
    assert report["recall"] == 1.0
    assert report["precision"] == 1.0
    assert set(report["by_injection"]) == {"drop_split_rule", "change_points", "each_to_total", "broaden_match"}


def test_silent_reviewer_scores_zero_recall_and_undefined_precision():
    cases = default_review_cases()
    report = evaluate_review(FakeScorer(*([{"issues": []}] * 50)), cases)
    assert report["recall"] == 0.0
    assert report["precision"] is None


def test_classifier_metrics_count_rule_labels():
    cases = default_classifier_cases()
    gold = {case["unit_id"]: case["label"] for case in cases}

    def perfect(payload):
        return {"items": [{"unit_id": u["unit_id"], "label": gold[u["unit_id"]], "suggested_criterion": None,
                           "reason": "r", "confidence": "high"} for u in payload["units"]]}

    report = evaluate_classifier(FakeScorer(perfect, perfect, perfect), cases)
    assert report["accuracy"] == 1.0 and report["rule_recall"] == 1.0 and report["rule_precision"] == 1.0

    def all_rule(payload):
        return {"items": [{"unit_id": u["unit_id"], "label": "rule", "suggested_criterion": None,
                           "reason": "r", "confidence": "high"} for u in payload["units"]]}

    report = evaluate_classifier(FakeScorer(all_rule), cases)
    assert report["rule_recall"] == 1.0
    assert report["rule_precision"] < 1.0


def test_mock_scorer_is_rejected_for_evaluation():
    with pytest.raises(ValueError):
        evaluate_review(type("M", (), {"provider": "mock"})(), default_review_cases())
