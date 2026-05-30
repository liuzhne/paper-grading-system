from types import SimpleNamespace

from backend.app.services.llm.mock import MockLLMScorer
from backend.app.services.rubric_import.compiler import annotations_for_criterion
from backend.app.services.rubric_import.compiler import compile_criterion_rules
from backend.app.services.rubric_import.compiler import normalize_rules_llm
from backend.app.services.rubric_import.compiler import parse_explicit_rules


def _criterion(deduction_rules=None, applies_to="global", name="规范性", dimension="规范性"):
    return SimpleNamespace(
        name=name, dimension=dimension, max_score=10, applies_to=applies_to, deduction_rules=deduction_rules or []
    )


def test_parse_explicit_rules_extracts_points_and_skips_numberless():
    rules = parse_explicit_rules(["缺题注 -1", "研究问题未回应扣 3 分", "意义笼统扣 1-3 分", "方法说明不足"])
    by_points = {round(r["points"], 1) for r in rules}
    assert by_points == {1.0, 3.0}  # 三条带数字（1-3 取上限 3），无数字的"方法说明不足"跳过
    assert all(r["source"] == "excel" for r in rules)
    assert any(r["match"] == "缺题注" for r in rules)


def test_compile_prefers_explicit_excel_rules():
    rules = compile_criterion_rules(_criterion(["缺题注 -1"]), [], scorer=MockLLMScorer())
    assert rules == [{"match": "缺题注", "points": 1.0, "reason": "缺题注 -1", "source": "excel"}]


def test_compile_falls_back_to_llm_when_no_explicit_points():
    class FakeScorer:
        def complete_json(self, instructions, payload):
            return {"rules": [{"match": "题注缺失", "points": 2, "reason": "图表无题注"}]}

    rules = compile_criterion_rules(_criterion(["图表要有题注"]), [], scorer=FakeScorer())
    assert rules == [{"match": "题注缺失", "points": 2.0, "reason": "图表无题注", "source": "llm"}]


def test_compile_mock_yields_no_structured_rules():
    # mock.complete_json 返回 coherence 形状，无 "rules" → 不自动扣分。
    assert compile_criterion_rules(_criterion(["图表要有题注"]), [], scorer=MockLLMScorer()) == []


def test_compile_without_scorer_and_no_explicit_is_empty():
    assert compile_criterion_rules(_criterion(["图表要有题注"]), [], scorer=None) == []


def test_normalize_rules_llm_ignores_malformed_items():
    class FakeScorer:
        def complete_json(self, instructions, payload):
            return {"rules": [{"match": "x", "points": None}, {"points": 2}, {"match": "缺引用", "points": 1, "reason": "r"}]}

    rules = normalize_rules_llm(_criterion(), [], FakeScorer())
    assert rules == [{"match": "缺引用", "points": 1.0, "reason": "r", "source": "llm"}]


def test_annotations_bind_by_section():
    annotations = [
        {"section_title": "第三章 研究方法", "comment_text": "需说明数据来源"},
        {"section_title": "结论", "comment_text": "需回应研究问题"},
    ]
    texts = annotations_for_criterion(_criterion(applies_to="研究方法"), annotations)
    assert texts == ["需说明数据来源"]
    assert annotations_for_criterion(_criterion(applies_to="global"), annotations) == []
