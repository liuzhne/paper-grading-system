from types import SimpleNamespace

from backend.app.services.llm.mock import MockLLMScorer
from backend.app.services.scoring import engine


class _CallSpyScorer(MockLLMScorer):
    """复用 Mock 的合法输出，仅记录每次 score_criterion 收到的证据块数。"""

    def __init__(self):
        self.candidate_counts = []

    def score_criterion(self, paper, criterion, evidence_candidates, structure_checks, anchors=None):
        self.candidate_counts.append(len(evidence_candidates))
        return super().score_criterion(paper, criterion, evidence_candidates, structure_checks, anchors)


def _criterion():
    return SimpleNamespace(
        id="c1", name="研究方法", max_score=20, scoring_mode="llm_direct",
        code="R03", rubric_levels=[], deduction_rules_structured=[],
    )


def _candidates():
    return [
        {"chunk_id": "c0", "text": "研究方法采用问卷调查与访谈相结合，收集数据并做统计分析。" * 2, "location": "第三章"},
        {"chunk_id": "c1", "text": "实验设计严谨，数据来源可靠，模型构建合理，结果验证充分。" * 2, "location": "第四章"},
        {"chunk_id": "c2", "text": "针对研究问题提出创新性解决方案与改进措施，贡献明确。" * 2, "location": "第五章"},
    ]


def test_llm_direct_per_chunk_by_default(monkeypatch):
    """默认（flag off）：每块各判一次 → top_k 次调用、汇总模式 per-evidence-chunk。"""
    monkeypatch.setattr(engine.settings, "SCORING_LLM_DIRECT_SINGLE_CALL", False)
    spy = _CallSpyScorer()
    paper = SimpleNamespace(id="p1", title="测试论文")

    output = engine._score_criterion_by_chunks(spy, paper, _criterion(), _candidates(), [], "v1", [])

    assert spy.candidate_counts == [1, 1, 1]  # 3 块 → 3 次调用，各喂 1 块
    assert output["chunk_evaluation_mode"] == "per-evidence-chunk"
    assert 0 <= output["score"] <= 20


def test_llm_direct_single_call_when_enabled(monkeypatch):
    """opt-in（flag on）：多块一次性整体判 → 仅 1 次调用、模式 single-combined。"""
    monkeypatch.setattr(engine.settings, "SCORING_LLM_DIRECT_SINGLE_CALL", True)
    spy = _CallSpyScorer()
    paper = SimpleNamespace(id="p1", title="测试论文")

    output = engine._score_criterion_by_chunks(spy, paper, _criterion(), _candidates(), [], "v1", [])

    assert spy.candidate_counts == [3]  # 一次性喂入全部 3 块
    assert output["chunk_evaluation_mode"] == "single-combined"
    assert output["evidence_gate_applied"] is True  # 仍走证据门槛（非 deductive/banded）
    assert 0 <= output["score"] <= 20
