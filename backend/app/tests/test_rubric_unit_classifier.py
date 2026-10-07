"""阶段 5：兜底分类器（只判断未认领单元，只影响报告，不改变解析结果）。"""

import pytest

from backend.app.services.rubric_import.classification.llm_classifier import CLASSIFIER_PROMPT_VERSION
from backend.app.services.rubric_import.classification.llm_classifier import ClassificationError
from backend.app.services.rubric_import.classification.llm_classifier import classification_fingerprint
from backend.app.services.rubric_import.classification.llm_classifier import classify_units
from backend.app.services.rubric_import.classification.llm_classifier import select_units
from backend.app.services.rubric_import.coverage import compute_coverage
from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.services.rubric_import.sources.units import SourceUnit

CRITERIA = [{"code": "C01", "name": "研究方法", "description": "方法合理"}]


class FakeScorer:
    provider = "fake"
    model_name = "fake-model"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, instructions, payload):
        self.calls.append((instructions, payload))
        response = self.responses.pop(0) if self.responses else None
        return response(payload) if callable(response) else response


def _label_all(label="rule", criterion="C01", confidence="high"):
    return lambda payload: {"items": [
        {"unit_id": u["unit_id"], "label": label, "suggested_criterion": criterion,
         "reason": "含扣分条件", "confidence": confidence} for u in payload["units"]]}


def _ledger():
    ledger = SourceLedger()
    for unit in [
        SourceUnit("r1", "excel", "rules", "cell", "错别字每处扣0.5分", {"sheet": "S", "row": 8}),
        SourceUnit("r2", "excel", "rules", "cell", "XX大学评分表", {}),
        SourceUnit("r3", "excel", "rules", "cell", "12", {}),
        SourceUnit("t1", "word", "template", "paragraph", "说明研究背景", {"heading_path": ["绪论"]}),
        SourceUnit("t2", "word", "template", "paragraph", "正文不少于800字", {"heading_path": ["绪论"]}),
        SourceUnit("t3", "word", "template", "comment", "这里改一下", {"anchor_text": "数据"}),
    ]:
        ledger.register(unit)
    return ledger


def test_select_units_follows_prefilter_rules():
    ledger = _ledger()
    selected = select_units(ledger, compute_coverage(ledger))
    # rules 文档全部未认领（去噪）；template 只送命中信号的段落与批注
    assert [u["unit_id"] for u in selected] == ["r1", "t3", "t2", "r2"]
    assert selected[0]["text"] == "错别字每处扣0.5分"
    assert selected[0]["context"] == {"sheet": "S", "row": 8}


def test_select_units_can_be_narrowed_by_user_scope_but_not_widened():
    ledger = _ledger()
    selected = select_units(ledger, compute_coverage(ledger), unit_ids=["r1", "t1", "missing"])
    assert [u["unit_id"] for u in selected] == ["r1"]  # t1 未命中信号，missing 不存在


def test_classify_accepts_valid_items_and_rejects_invalid_ones():
    units = select_units(_ledger(), compute_coverage(_ledger()))
    scorer = FakeScorer(lambda payload: {"items": [
        {"unit_id": "r1", "label": "rule", "suggested_criterion": "C01", "reason": "扣分", "confidence": "high"},
        {"unit_id": "t3", "label": "context", "suggested_criterion": None, "reason": "编辑意见", "confidence": "medium"},
        {"unit_id": "t2", "label": "rule", "suggested_criterion": "C99", "reason": "x", "confidence": "high"},
        {"unit_id": "zz", "label": "rule", "suggested_criterion": None, "reason": "x", "confidence": "high"},
        {"unit_id": "r2", "label": "banana", "suggested_criterion": None, "reason": "x", "confidence": "high"},
        {"unit_id": "r1", "label": "noise", "suggested_criterion": None, "reason": "重复", "confidence": "low"},
    ]})
    result = classify_units(units, CRITERIA, scorer, batch_size=15)
    assert [(r["unit_id"], r["label"], r["needs_review"]) for r in result["results"]] == [
        ("r1", "rule", False), ("t3", "context", False)]
    assert {r["unit_id"]: r["error"] for r in result["rejected"]} == {
        "t2": "unknown_criterion", "zz": "unknown_unit", "r2": "invalid_label", "r1": "duplicate_unit"}
    assert result["unclassified_unit_ids"] == ["t2", "r2"]
    assert result["prompt_version"] == CLASSIFIER_PROMPT_VERSION
    instructions, payload = scorer.calls[0]
    assert "不可信数据" in instructions
    assert payload["criteria"] == [{"code": "C01", "name": "研究方法", "description": "方法合理"}]


def test_low_confidence_rule_or_requirement_needs_review():
    units = select_units(_ledger(), compute_coverage(_ledger()), unit_ids=["r1"])
    result = classify_units(units, CRITERIA, FakeScorer(_label_all("requirement", None, "medium")))
    assert result["results"][0]["needs_review"] is True


def test_units_are_sent_in_batches():
    ledger = SourceLedger()
    for index in range(20):
        ledger.register(SourceUnit(f"u{index}", "excel", "rules", "cell", f"第{index}项扣1分", {}))
    units = select_units(ledger, compute_coverage(ledger))
    scorer = FakeScorer(_label_all(), _label_all())
    result = classify_units(units, CRITERIA, scorer, batch_size=15)
    assert [len(call[1]["units"]) for call in scorer.calls] == [15, 5]
    assert len(result["results"]) == 20


def test_invalid_envelope_is_retried_once_then_reported():
    units = select_units(_ledger(), compute_coverage(_ledger()), unit_ids=["r1"])
    retried = FakeScorer({"oops": True}, _label_all())
    assert len(classify_units(units, CRITERIA, retried)["results"]) == 1
    assert "上次输出" in retried.calls[1][0]
    failed = classify_units(units, CRITERIA, FakeScorer("not json", {"items": "x"}))
    assert failed["results"] == []
    assert failed["failed_unit_ids"] == ["r1"]


@pytest.mark.parametrize("scorer", [None, type("Mock", (), {"provider": "mock"})()])
def test_mock_or_missing_connection_is_refused(scorer):
    with pytest.raises(ClassificationError) as exc:
        classify_units([{"unit_id": "r1", "text": "x", "context": {}}], CRITERIA, scorer)
    assert exc.value.code == "AI_CONNECTION_MISSING"


def test_fingerprint_changes_when_criteria_or_units_change():
    units = [{"unit_id": "r1", "text": "扣1分", "context": {}}]
    base = classification_fingerprint(units, CRITERIA)
    assert base == classification_fingerprint(units, CRITERIA)
    assert base != classification_fingerprint(units, [{**CRITERIA[0], "name": "研究设计"}])
    assert base != classification_fingerprint([{**units[0], "text": "扣2分"}], CRITERIA)


def test_budget_and_safe_truncation_diagnostic():
    from backend.app.services.llm.openai_adapter import ResponsesJSONOutputError

    class Truncated:
        provider = 'openai'
        def complete_json(self, instructions, payload, *, default_max_tokens=None):
            assert default_max_tokens == 8192
            raise ResponsesJSONOutputError('output_truncated')

    result = classify_units([{'unit_id': 'u', 'text': '合成', 'context': {}}], CRITERIA, Truncated())
    assert result['failed_unit_ids'] == ['u']
    assert result['rejected'] == [{'unit_id': 'u', 'error': 'output_truncated'}]


def test_fingerprint_includes_description_and_context():
    units = [{'unit_id': 'u', 'text': '合成', 'context': {'heading_path': ['一']}}]
    base = classification_fingerprint(units, CRITERIA)
    assert base != classification_fingerprint(units, [{**CRITERIA[0], 'description': '修改要求'}])
    assert base != classification_fingerprint([{**units[0], 'context': {'heading_path': ['二']}}], CRITERIA)


def test_small_batches_preserve_success_and_stop_on_timeout():
    import httpx
    from backend.app.services.llm.errors import ProviderCallError, project_provider_error

    class SlowScorer:
        provider = "openai"
        calls = 0

        def complete_json(self, instructions, payload, *, default_max_tokens=None, attempts_limit=None):
            assert attempts_limit == 1
            assert default_max_tokens == 8192
            assert len(payload["units"]) <= 3
            self.calls += 1
            if self.calls == 2:
                raise ProviderCallError("openai", project_provider_error(httpx.ReadTimeout("secret upstream text")))
            return _label_all()(payload)

    scorer = SlowScorer()
    units = [{"unit_id": str(i), "text": "合成测试", "context": {}} for i in range(8)]
    result = classify_units(units, CRITERIA, scorer)
    assert scorer.calls == 2
    assert [r["unit_id"] for r in result["results"]] == ["0", "1", "2"]
    assert result["failed_unit_ids"] == ["3", "4", "5"]
    assert result["unclassified_unit_ids"] == ["6", "7"]
    assert {r["error"] for r in result["rejected"]} == {"request_timeout"}
    assert "secret" not in str(result)
