from types import SimpleNamespace

from backend.app.services.scoring.engine import _aggregate_hybrid
from backend.app.services.scoring.engine import _apply_banded
from backend.app.services.scoring.engine import _blank_usage
from backend.app.services.scoring.engine import _compute_hybrid
from backend.app.services.scoring.engine import _make_sub_criterion
from backend.app.services.scoring.engine import CriterionPlan
from backend.app.services.scoring.engine import ScoringInputs
from backend.app.services.scoring.engine import compute_scoring
from backend.app.services.llm.mock import MockLLMScorer


BANDS = [
    {"label": "优", "points": 10},
    {"label": "良", "points": 7},
    {"label": "中", "points": 4},
    {"label": "差", "points": 0},
]


def test_banded_snaps_to_nearest_band():
    criterion = SimpleNamespace(name="创新性", code="C04", max_score=10, rubric_levels=BANDS)
    output = {
        "score": 6.5,
        "evidence": [{"quote": "确有改进", "location": "第五章"}],
        "reason": "有一定创新",
        "need_manual_review": False,
        "deduction_items": [],
    }
    result = _apply_banded(criterion, output)
    assert result["score"] == 7.0  # 6.5 最近档为 良(7)
    assert result["scoring_mode"] == "banded"
    assert result["band_selection"]["level"] == "良"
    assert result["band_selection"]["evidence_quote"] == "确有改进"


def test_banded_without_levels_falls_back_without_crashing():
    criterion = SimpleNamespace(name="创新性", code="C04", max_score=10, rubric_levels=[])
    output = {"score": 8.0, "evidence": [], "confidence": 0.9, "evidence_sufficient": True}
    result = _apply_banded(criterion, output)
    assert isinstance(result["score"], float)
    assert result.get("scoring_mode") != "banded"  # 无档位 → 走证据门槛


def test_banded_prefers_model_selected_band():
    criterion = SimpleNamespace(name="创新性", code="C04", max_score=10, rubric_levels=BANDS)
    output = {
        "score": 6.0,  # 仅就近吸附会落到 良(7)
        "band_selection": {"level": "优", "rationale": "很有创新"},
        "evidence": [{"quote": "q", "location": "l"}],
        "reason": "r",
    }
    result = _apply_banded(criterion, output)
    assert result["score"] == 10.0  # 采用模型选的 优(10)
    assert result["band_selection"]["level"] == "优"
    assert result["band_selection_basis"] == "model-band"


def test_make_sub_criterion_maps_kind_and_points():
    parent = SimpleNamespace(id="h1", code="H1", name="规范性", applies_to="global", evidence_hints=[], deduction_rules=[])
    sub = _make_sub_criterion(parent, {"kind": "deterministic", "max_points": 10, "name": "参考文献"}, 1)
    assert sub.criterion_type == "deterministic"
    assert sub.scoring_mode == "deductive"
    assert sub.max_score == 10.0
    assert sub.name == "参考文献"


def test_aggregate_hybrid_sums_subscores():
    criterion = SimpleNamespace(id="h1", name="规范性", max_score=20)
    sub_results = [
        {"score": 8, "deduction_items": [{"points": 2, "reason": "a"}], "deductions": ["a"],
         "evidence": [{"quote": "x", "chunk_id": "1"}], "confidence": 1.0, "evidence_sufficient": True, "need_manual_review": False},
        {"score": 9, "deduction_items": [], "deductions": [],
         "evidence": [], "confidence": 0.8, "evidence_sufficient": True, "need_manual_review": False},
    ]
    out = _aggregate_hybrid(criterion, sub_results, _blank_usage())
    assert out["scoring_mode"] == "hybrid"
    assert out["score"] == 17.0
    assert len(out["sub_results"]) == 2
    assert out["confidence"] == 0.8  # 取最小


def test_compute_hybrid_deterministic_subchecks_end_to_end():
    parsed = {
        "structure_checks": [],
        "full_text": "研究方法见[1]。" + "字" * 200,
        "references": ["[1] 张三. 研究. 2024."],
    }
    criterion = SimpleNamespace(
        id="h1",
        code="H1",
        name="规范性",
        max_score=20,
        applies_to="global",
        evidence_hints=[],
        deduction_rules=[],
        sub_checks=[
            {"kind": "deterministic", "max_points": 10, "name": "参考文献"},
            {"kind": "deterministic", "max_points": 10, "name": "正文字数"},
        ],
    )
    # 6.1 解耦后：候选证据在 collect 阶段预取，compute 收 sub_plans（确定性子项候选为空）。
    sub_plans = [(_make_sub_criterion(criterion, sub, i), []) for i, sub in enumerate(criterion.sub_checks, start=1)]
    out = _compute_hybrid(None, None, criterion, sub_plans, parsed, [], "v1.0")
    assert out["scoring_mode"] == "hybrid"
    assert len(out["sub_results"]) == 2
    assert {s["checker_kind"] for s in out["sub_results"]} == {"citation", "word_count"}
    assert out["score"] == round(min(sum(s["score"] for s in out["sub_results"]), 20), 2)


def test_compute_scoring_runs_without_any_db():
    """6.1 解耦：compute_scoring 是纯函数——仅凭手搓的 ScoringInputs + scorer 即可出分，全程无 DB/会话。"""
    criterion = SimpleNamespace(
        id="c1",
        code="C01",
        name="研究方法",
        max_score=20.0,
        description=None,
        evidence_hints=[],
        deduction_rules=[],
        deduction_rules_structured=[],
        criterion_type="llm_judgment",
        scoring_mode="llm_direct",
        applies_to="global",
        rubric_levels=[],
        sub_checks=[],
        dimension=None,
    )
    plan = CriterionPlan(
        criterion=criterion,
        route="chunks",
        candidates=[{"chunk_id": "1", "location": "第三章", "section_title": "研究方法", "text": "本文采用实验法，数据来源清楚。"}],
        anchors=[],
    )
    inputs = ScoringInputs(
        paper_id="p1",
        paper_title="测试论文",
        parse_quality=0.9,
        parsed={"structure_checks": [], "coherence_findings": []},
        structure_checks=[],
        rubric_id="r1",
        rubric_total_score=20.0,
        rubric_version="v1",
        base_coherence=[],
        format_findings=[],
        criteria=[plan],
    )
    result = compute_scoring(inputs, MockLLMScorer())  # 不传任何 db/session
    assert len(result.items) == 1
    assert result.items[0]["criterion_id"] == "c1"
    assert 0 <= result.final_total <= 20.0
    assert result.grade
