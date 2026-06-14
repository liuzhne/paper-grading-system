"""A1 回归：openai_compatible 评分 prompt 不得再硬锚定「普通证据封顶 80%」。

背景（docs/QWK基线攻坚-对话全记录.md §10/§11）：config.SCORING_STANDARD_CAP_RATIO 已标注
「已弃用：被证据门槛取代」，但 prompt 仍硬写「普通证据块最高只能给满分的 80%」+
scoring_policy.normal_cap_ratio=0.8。实证：一篇论文 R01-R05 五项得分精确全部落在 80.0%，
说明模型被该锚定压平、区分度坍塌，直接威胁 QWK。本测试守护：评分依据应是「证据门槛」
（证据不足才设上限，取自 config），而非固定的普通封顶。
"""

import json
from types import SimpleNamespace

from backend.app.core.config import settings
from backend.app.services.llm.openai_compatible_adapter import _input_payload
from backend.app.services.llm.openai_compatible_adapter import _instructions


def _paper():
    return SimpleNamespace(id="p1", title="测试论文标题")


def _criterion(scoring_mode="llm_direct", rubric_levels=None):
    return SimpleNamespace(
        id="c1",
        code="R01",
        name="外语运用能力",
        max_score=20.0,
        description="评估外语运用能力。",
        evidence_hints=[],
        deduction_rules=[],
        scoring_mode=scoring_mode,
        rubric_levels=rubric_levels or [],
    )


def _evidence():
    return [{"chunk_id": "ch1", "location": "第1段", "section_title": "摘要", "text": "证据文本"}]


def test_instructions_drop_hard_80pct_cap():
    text = _instructions(_criterion())
    assert "80%" not in text


def test_scoring_policy_omits_fixed_normal_cap():
    payload = json.loads(_input_payload(_paper(), _criterion(), _evidence(), {}))
    policy = payload.get("scoring_policy", {})
    assert "normal_cap_ratio" not in policy


def test_scoring_policy_uses_insufficient_evidence_cap_from_config():
    payload = json.loads(_input_payload(_paper(), _criterion(), _evidence(), {}))
    policy = payload.get("scoring_policy", {})
    assert policy.get("insufficient_evidence_cap_ratio") == settings.SCORING_INSUFFICIENT_EVIDENCE_CAP_RATIO
