from backend.app.services.coherence.semantic import _findings_from_verification
from backend.app.services.coherence.semantic import analyze_semantic_coherence
from backend.app.services.llm.mock import MockLLMScorer


def _parsed(intro="本文的研究问题是如何评估教学质量。", conclusion="本文回应了教学质量评估问题。"):
    return {
        "sections": [
            {"title": "第一章 绪论", "paragraphs": [{"text": intro}]},
            {"title": "结论", "paragraphs": [{"text": conclusion}]},
        ],
        "full_text": intro + conclusion,
    }


def test_findings_from_verification_flags_unanswered_and_unsupported():
    result = {
        "research_questions": [{"text": "RQ1", "answered": False}, {"text": "RQ2", "answered": True}],
        "conclusion_claims": [{"text": "C1", "supported": False}],
    }
    findings = _findings_from_verification(result)
    assert {f["kind"] for f in findings} == {"research_question_unanswered", "conclusion_claim_unsupported"}
    assert len(findings) == 2


def test_mock_scorer_yields_no_semantic_findings():
    assert analyze_semantic_coherence(_parsed(), MockLLMScorer()) == []


def test_missing_sections_returns_empty():
    parsed = {"sections": [{"title": "第二章 方法", "paragraphs": [{"text": "x"}]}], "full_text": "x"}
    assert analyze_semantic_coherence(parsed, MockLLMScorer()) == []


def test_fake_scorer_unanswered_produces_finding():
    class FakeScorer:
        def complete_json(self, instructions, payload):
            return {"research_questions": [{"text": "研究问题A", "answered": False}], "conclusion_claims": []}

    findings = analyze_semantic_coherence(_parsed(), FakeScorer())
    assert any(f["kind"] == "research_question_unanswered" for f in findings)


def test_scorer_error_degrades_gracefully():
    class BoomScorer:
        def complete_json(self, instructions, payload):
            raise RuntimeError("boom")

    findings = analyze_semantic_coherence(_parsed(), BoomScorer())
    assert findings and findings[0]["kind"] == "coherence_semantic_skipped"
    assert findings[0]["severity"] == "info"
