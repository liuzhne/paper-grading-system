from types import SimpleNamespace

from backend.app.services.scoring.engine import _apply_deductive
from backend.app.services.scoring.validator import coerce_deduction_items
from backend.app.services.scoring.validator import _format_deduction


def _criterion(max_score=10.0, name="研究方法"):
    return SimpleNamespace(name=name, max_score=max_score, scoring_mode="deductive")


def test_deductive_awarded_is_computed_from_points_not_model_score():
    criterion = _criterion(max_score=10)
    output = {
        "score": 9.5,  # 模型自报，应被忽略
        "deduction_items": [
            {"points": 2, "reason": "方法描述不完整", "rule_ref": "C01"},
            {"points": 3, "reason": "缺少数据来源说明", "rule_ref": "C01"},
        ],
    }
    result = _apply_deductive(criterion, output)
    assert result["score"] == 5.0
    assert result["score_before_deductive"] == 9.5
    assert result["scoring_mode"] == "deductive"


def test_deductive_clamps_to_zero_and_flags_review_when_overdrawn():
    criterion = _criterion(max_score=10)
    output = {
        "score": 1.0,
        "need_manual_review": False,
        "deduction_items": [{"points": 7, "reason": "a"}, {"points": 6, "reason": "b"}],
    }
    result = _apply_deductive(criterion, output)
    assert result["score"] == 0.0
    assert result["need_manual_review"] is True


def test_deductive_ignores_items_without_points():
    criterion = _criterion(max_score=10)
    output = {"score": 4.0, "deduction_items": [{"points": None, "reason": "提示性说明"}]}
    result = _apply_deductive(criterion, output)
    assert result["score"] == 10.0


def test_coerce_deduction_items_from_structured_dicts():
    items = coerce_deduction_items(
        [{"points": "2.5", "reason": "x", "rule_ref": "C03", "evidence_quote": "q", "evidence_location": "loc"}]
    )
    assert items == [
        {"points": 2.5, "reason": "x", "rule_ref": "C03", "evidence_quote": "q", "evidence_location": "loc"}
    ]


def test_coerce_deduction_items_falls_back_to_strings():
    items = coerce_deduction_items(None, fallback_strings=["论述不足。"])
    assert items[0]["reason"] == "论述不足。"
    assert items[0]["points"] is None


def test_format_deduction_appends_points_suffix():
    assert _format_deduction({"points": 3, "reason": "缺少方法"}) == "缺少方法（-3分）"
    assert _format_deduction({"points": None, "reason": "仅说明"}) == "仅说明"
