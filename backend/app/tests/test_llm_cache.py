from types import SimpleNamespace

from backend.app.core.config import settings
from backend.app.services.cache import llm_cache
from backend.app.services.scoring.engine import _score_with_runtime_fallback


def _criterion():
    return SimpleNamespace(
        id="c1",
        code="C1",
        name="研究方法",
        max_score=10,
        description=None,
        evidence_hints=[],
        deduction_rules=[],
        criterion_type="llm_judgment",
        scoring_mode="llm_direct",
        applies_to="global",
        rubric_levels=[],
    )


class CountingScorer:
    provider = "dummy"
    model_name = "dummy-model"
    model_version = "v1"

    def __init__(self):
        self.calls = 0

    def score_criterion(self, paper, criterion, candidates, structure_checks, anchors=None):
        self.calls += 1
        return {
            "criterion_id": criterion.id,
            "criterion_name": criterion.name,
            "max_score": float(criterion.max_score),
            "score": 7.0,
            "evidence_sufficient": True,
            "reason": "ok",
            "deductions": [],
            "evidence": [],
            "suggestion": "",
            "confidence": 0.8,
            "need_manual_review": False,
        }


def test_key_is_deterministic_and_input_sensitive():
    scorer = CountingScorer()
    criterion = _criterion()
    candidates = [{"chunk_id": "k1", "text": "原文证据"}]

    base = llm_cache.key_of(llm_cache.build_request(scorer, criterion, candidates, [], "v1.0"))
    same = llm_cache.key_of(llm_cache.build_request(scorer, criterion, candidates, [], "v1.0"))
    diff_text = llm_cache.key_of(
        llm_cache.build_request(scorer, criterion, [{"chunk_id": "k1", "text": "不同"}], [], "v1.0")
    )
    diff_version = llm_cache.key_of(llm_cache.build_request(scorer, criterion, candidates, [], "v2.0"))

    assert base == same
    assert diff_text != base
    assert diff_version != base


def test_calibration_anchors_change_cache_key():
    scorer = CountingScorer()
    criterion = _criterion()
    candidates = [{"chunk_id": "k1", "text": "原文证据"}]

    without = llm_cache.key_of(llm_cache.build_request(scorer, criterion, candidates, [], "v1.0", None))
    with_anchor = llm_cache.key_of(
        llm_cache.build_request(
            scorer, criterion, candidates, [], "v1.0",
            [{"label": "优", "score": 9, "max_score": 10, "excerpt": "范文", "rationale": "好"}],
        )
    )
    assert without != with_anchor  # 锚点变化 → 缓存失效（保可复现）


def test_get_put_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "STORAGE_ROOT", tmp_path)
    assert llm_cache.get("missing") is None
    llm_cache.put("k", {"input": 1}, {"score": 5}, model="m")
    assert llm_cache.get("k") == {"score": 5}


def test_cache_hit_avoids_second_model_call(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(settings, "LLM_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_RATE_LIMIT_SLEEP_SECONDS", 0)

    scorer = CountingScorer()
    criterion = _criterion()
    candidates = [{"chunk_id": "k1", "text": "原文证据"}]

    out1 = _score_with_runtime_fallback(scorer, None, criterion, candidates, [], "v1.0")
    out2 = _score_with_runtime_fallback(scorer, None, criterion, candidates, [], "v1.0")

    assert scorer.calls == 1
    assert out1.get("cache_hit") is False
    assert out2.get("cache_hit") is True
    assert out2["score"] == 7.0
    assert out2["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_cache_disabled_always_calls_model(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(settings, "LLM_CACHE_ENABLED", False)
    monkeypatch.setattr(settings, "LLM_RATE_LIMIT_SLEEP_SECONDS", 0)

    scorer = CountingScorer()
    criterion = _criterion()
    candidates = [{"chunk_id": "k1", "text": "原文证据"}]

    _score_with_runtime_fallback(scorer, None, criterion, candidates, [], "v1.0")
    _score_with_runtime_fallback(scorer, None, criterion, candidates, [], "v1.0")

    assert scorer.calls == 2
