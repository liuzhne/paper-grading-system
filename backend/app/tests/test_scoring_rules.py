from types import SimpleNamespace

from backend.app.services.scoring.engine import _aggregate_chunk_outputs
from backend.app.services.scoring.engine import _apply_evidence_gate
from backend.app.services.scoring.rules import calculate_total_score
from backend.app.services.scoring.rules import is_near_grade_boundary
from backend.app.services.scoring.rules import match_grade
from backend.app.services.scoring.rules import need_manual_review


def test_calculates_total_and_grade():
    items = [
        SimpleNamespace(id="1", ai_score=8, final_score=9, max_score=10, evidence_sufficient=True, confidence=0.9, need_manual_review=False),
        SimpleNamespace(id="2", ai_score=18, final_score=None, max_score=20, evidence_sufficient=True, confidence=0.8, need_manual_review=False),
    ]

    assert calculate_total_score(items, total_score=30) == 27
    assert match_grade(92) == "优秀"
    assert match_grade(77) == "中等"


def test_manual_review_rules():
    item = SimpleNamespace(id="1", ai_score=8, final_score=8, max_score=10, evidence_sufficient=True, confidence=0.9, need_manual_review=False)
    low_confidence = SimpleNamespace(id="2", ai_score=8, final_score=8, max_score=10, evidence_sufficient=True, confidence=0.5, need_manual_review=False)

    assert is_near_grade_boundary(68.5)
    assert need_manual_review(68.5, [item], parse_quality=0.9)
    assert need_manual_review(88, [low_confidence], parse_quality=0.9)
    assert need_manual_review(88, [item], parse_quality=0.4)


def test_evidence_gate_downscores_insufficient_evidence():
    criterion = SimpleNamespace(id="c1", name="研究方法", max_score=20)
    chunk_outputs = [
        {
            "score": 18,
            "confidence": 0.5,
            "evidence_sufficient": False,
            "deductions": [],
            "deduction_items": [],
            "evidence": [],
            "need_manual_review": False,
            "chunk_id": "chunk-1",
            "chunk_location": "第三章",
        },
        {
            "score": 17,
            "confidence": 0.55,
            "evidence_sufficient": False,
            "deductions": [],
            "deduction_items": [],
            "evidence": [],
            "need_manual_review": False,
            "chunk_id": "chunk-2",
            "chunk_location": "第四章",
        },
    ]

    output = _apply_evidence_gate(_aggregate_chunk_outputs(criterion, chunk_outputs), criterion, chunk_outputs)

    # 证据不足：得分下调到证据门槛 0.6*20=12，并标记复核（不再有 0.8 常规封顶）。
    assert output["score"] == 12.0
    assert output["need_manual_review"] is True
    assert any("证据门槛" in item for item in output["deductions"])


def test_evidence_gate_trusts_sufficient_and_confident_score():
    criterion = SimpleNamespace(id="c1", name="研究方法", max_score=20)
    chunk_outputs = [
        {
            "score": 16,
            "confidence": 0.9,
            "evidence_sufficient": True,
            "deductions": [],
            "deduction_items": [],
            "evidence": [{"quote": "证据一", "chunk_id": "chunk-1"}],
            "need_manual_review": False,
            "chunk_id": "chunk-1",
            "chunk_location": "第三章",
        },
        {
            "score": 16,
            "confidence": 0.9,
            "evidence_sufficient": True,
            "deductions": [],
            "deduction_items": [],
            "evidence": [{"quote": "证据二", "chunk_id": "chunk-2"}],
            "need_manual_review": False,
            "chunk_id": "chunk-2",
            "chunk_location": "第四章",
        },
    ]

    output = _apply_evidence_gate(_aggregate_chunk_outputs(criterion, chunk_outputs), criterion, chunk_outputs)

    # 证据充分且置信高、未接近满分：信任模型分，不下调、不强制复核。
    assert output["score"] == 16.0
    assert output["need_manual_review"] is False
    assert output["evidence_gate_applied"] is True
