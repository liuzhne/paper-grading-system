"""旧评分路径的提示词从适配器移到 ``llm/legacy_prompts.py`` 时必须逐字不变。

这些文本进入 L0 缓存身份；改动任何一个字都要同时升 ``PROMPT_VERSION``，并更新这里的哈希。
"""

import hashlib
import json
from types import SimpleNamespace

import pytest

from backend.app.services.llm import legacy_prompts
from backend.app.services.llm import openai_adapter
from backend.app.services.llm import openai_compatible_adapter


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "mode, instructions, envelope_instructions, envelope_schema",
    [
        (
            "llm_direct",
            "d3496dee020aac922f672f3b68a96f0c3d44e4f917c6109114a01ec6903d58ed",
            "34ece45e943799f7cfcad01e3f210f40d22c856ddfd4deaae341505be2df1e55",
            "174e48cb5856dbfb0f8c5b4cea116911f7a1375b8a260bf2584aa84354f192c9",
        ),
        (
            "deductive",
            "272b1909688f05fd3ef6971bd33009592b433986a367fffd503000559b9c947c",
            "ddbffdd0cf89e50148773aa1104818f3b92d6489e5789ffde08dbbf07b0755a6",
            "174e48cb5856dbfb0f8c5b4cea116911f7a1375b8a260bf2584aa84354f192c9",
        ),
        (
            "banded",
            "961e6c4bbccb3141f78059225c6c0ee379b6ab1382e9c2be1a7bc983a0c3ec5e",
            "7327511072949bef48c4fbf806a93122288ad7c304123c194d2a653a126b78ff",
            "42e51c45893872b0a6eb719edb3c45d4bb92962a66ed1e02372ddfaaa02d3993",
        ),
    ],
)
def test_legacy_prompts_text_is_frozen(mode, instructions, envelope_instructions, envelope_schema):
    criterion = SimpleNamespace(scoring_mode=mode)
    assert _sha(legacy_prompts.chat_instructions(criterion)) == instructions
    assert _sha(legacy_prompts.chat_envelope_instructions(mode)) == envelope_instructions
    assert _sha(json.dumps(legacy_prompts.envelope_score_schema(mode), sort_keys=True)) == envelope_schema


def test_legacy_schema_and_input_payload_are_frozen():
    assert _sha(json.dumps(legacy_prompts.score_schema(), sort_keys=True)) == (
        "d1a379ca2814d7fde01271dd0ca05dcc1e72395beffa563e2551376c6017cddf"
    )
    paper = SimpleNamespace(id="p1", title="T")
    criterion = SimpleNamespace(
        id="c1", code="C01", name="N", max_score=10, description="d",
        evidence_hints=["h"], deduction_rules=["r"], scoring_mode="banded",
        rubric_levels=[{"level": "A"}],
    )
    payload = legacy_prompts.chat_input_payload(
        paper, criterion,
        [{"chunk_id": "x", "location": "l", "section_title": "s", "text": "t"}],
        [{"k": 1}], [{"a": 1}],
    )
    assert _sha(payload) == "4ce2125bcda78c28de458a2fa52a2ad9b2b8b46f54a87938c58601e205a8a7c2"


def test_adapters_keep_using_the_shared_text():
    # 旧名字仍可从原模块导入（外部测试和脚本在用），而且就是同一个函数。
    assert openai_compatible_adapter._instructions is legacy_prompts.chat_instructions
    assert openai_compatible_adapter._input_payload is legacy_prompts.chat_input_payload
    assert openai_adapter._envelope_score_schema is legacy_prompts.envelope_score_schema
