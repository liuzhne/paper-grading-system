"""A2 回归：openai_compatible 的 system 指令应按 criterion.scoring_mode 裁剪。

背景（docs/QWK基线攻坚-对话全记录.md §10）：当前 _instructions() 把 deductive/banded/
llm_direct 三种模式的指令一次性全塞给模型，不管当前评分项实际是哪种模式。对本地小模型
（Qwen3-30B Q4）易混淆，也推高 token。本测试守护：每次只下发与当前 scoring_mode 相关的
模式专属指令。
"""

from types import SimpleNamespace

from backend.app.services.llm.openai_compatible_adapter import _instructions


def _criterion(scoring_mode, rubric_levels=None):
    return SimpleNamespace(
        id="c1",
        code="R03",
        name="解决策略与技术路线",
        max_score=30.0,
        description="评估技术路线。",
        evidence_hints=[],
        deduction_rules=[],
        scoring_mode=scoring_mode,
        rubric_levels=rubric_levels or [],
    )


def test_deductive_criterion_gets_only_deductive_mode_instructions():
    text = _instructions(_criterion("deductive"))
    assert "扣分制" in text
    assert "band_selection" not in text
    assert "分档制" not in text


def test_banded_criterion_gets_only_banded_mode_instructions():
    levels = [{"level": "优秀", "min": 27, "max": 30}, {"level": "良好", "min": 24, "max": 26}]
    text = _instructions(_criterion("banded", rubric_levels=levels))
    assert "band_selection" in text
    assert "分档制" in text
    assert "扣分制" not in text


def test_llm_direct_criterion_omits_mode_specific_blocks():
    text = _instructions(_criterion("llm_direct"))
    assert "扣分制" not in text
    assert "分档制" not in text
    assert "band_selection" not in text
