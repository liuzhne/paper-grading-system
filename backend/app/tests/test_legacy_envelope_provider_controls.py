"""旧评分路径的 PromptEnvelope 必须冻结**本次适配器实例**的调用参数。

`build_prompt_envelope` 产出的 provider 合同就是 `score_envelope` 实际发出的请求参数。
它曾经读全局设置：BYOK / 平台 Chat 连接配置的 max_tokens、temperature、
response_format_json 被静默丢弃，没开 thinking 的连接还会收到 `thinking: {"type": "disabled"}`
——而工厂明确说只有连接选择开启时才发 thinking；Responses 连接的 max_output_tokens、
temperature 同样被丢弃。Core 的 `core_runtime_provider_contract` 一直读实例，两边应一致。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from backend.app.core.config import settings
from backend.app.services.ai_connections import ConnectionRuntime
from backend.app.services.cache import llm_cache
from backend.app.services.llm.base import _provider_contract
from backend.app.services.llm.core_adapter import core_runtime_provider_contract
from backend.app.services.llm.factory import get_llm_scorer
from backend.app.services.llm.openai_adapter import OpenAIResponsesScorer
from backend.app.services.llm.openai_compatible_adapter import OpenAICompatibleChatScorer
from backend.app.services.scoring.engine import _score_with_runtime_fallback


_PAPER = SimpleNamespace(id="paper-1", title="可解释模型的教学质量评价")
_CRITERION = SimpleNamespace(
    id="criterion-1",
    code="C01",
    name="研究方法",
    max_score=10,
    scoring_mode="llm_direct",
    description="评价研究设计与数据来源。",
    evidence_hints=[],
    deduction_rules=[],
    rubric_levels=[],
    authorized_rules=[],
)
_CANDIDATES = [{"text": "本文数据来自公开问卷。", "location": "第三章", "section_title": "第三章"}]
_SCORE = {
    "score": 8,
    "evidence_sufficient": True,
    "reason": "方法说明完整。",
    "deductions": [],
    "evidence": [],
    "confidence": 0.9,
}


def _chat_client(captured):
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chat-1",
                "choices": [
                    {"message": {"content": json.dumps(_SCORE)}, "finish_reason": "stop"}
                ],
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _chat_runtime(base_url, **options):
    return ConnectionRuntime(
        connection_id="conn-1",
        key_version=1,
        organization_id="org-1",
        provider_type="openai_compatible",
        base_url=base_url,
        model_name="byok-chat-model",
        provider_options=options,
        api_key="test-key",
    )


def _byok_chat_scorer(base_url, captured, **options):
    scorer = get_llm_scorer(_chat_runtime(base_url, **options))
    scorer.client.close()
    scorer.client = _chat_client(captured)
    return scorer


def _legacy_score(scorer):
    return _score_with_runtime_fallback(scorer, _PAPER, _CRITERION, _CANDIDATES, [], None)


@pytest.fixture(autouse=True)
def _global_compatible_defaults(monkeypatch):
    # 全局设置故意与连接配置不同；只有读错来源才会让它们出现在请求里。
    monkeypatch.setattr(settings, "LLM_RATE_LIMIT_SLEEP_SECONDS", 0)
    monkeypatch.setattr(settings, "LLM_FALLBACK_TO_MOCK", False)
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_TEMPERATURE", 0.0)
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_MAX_TOKENS", 1200)
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_THINKING_TYPE", "disabled")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON", False)


def test_byok_chat_connection_options_reach_the_legacy_envelope_request():
    captured = []
    scorer = _byok_chat_scorer(
        "https://byok-options.invalid/v1",
        captured,
        max_tokens=333,
        temperature=0.4,
        response_format_json=True,
    )

    result = _legacy_score(scorer)

    assert result["score"] == 8
    assert len(captured) == 1
    payload = captured[0]
    assert payload["max_tokens"] == 333
    assert payload["temperature"] == 0.4
    assert payload["response_format"] == {"type": "json_object"}


def test_byok_chat_connection_without_thinking_opt_in_sends_no_thinking_field():
    captured = []
    scorer = _byok_chat_scorer("https://byok-no-thinking.invalid/v1", captured)

    _legacy_score(scorer)

    assert "thinking" not in captured[0]
    assert "response_format" not in captured[0]


def test_byok_chat_connection_thinking_opt_in_is_sent():
    captured = []
    scorer = _byok_chat_scorer(
        "https://byok-thinking.invalid/v1", captured, thinking_type="enabled"
    )

    _legacy_score(scorer)

    assert captured[0]["thinking"] == {"type": "enabled"}


def _responses_runtime(base_url, **options):
    return ConnectionRuntime(
        connection_id="conn-2",
        key_version=1,
        organization_id="org-1",
        provider_type="openai_responses",
        base_url=base_url,
        model_name="byok-responses-model",
        provider_options=options,
        api_key="test-key",
    )


@pytest.mark.parametrize(
    "runtime",
    [
        _chat_runtime(
            "https://byok-contract.invalid/v1",
            max_tokens=333,
            temperature=0.4,
            top_p=0.95,
            response_format_json=True,
            thinking_type="enabled",
        ),
        _chat_runtime("https://byok-contract-default.invalid/v1"),
        _responses_runtime(
            "https://byok-responses-contract.invalid/v1",
            max_output_tokens=333,
            temperature=0.4,
            top_p=0.95,
        ),
    ],
    ids=["chat-all-options", "chat-defaults", "responses"],
)
def test_legacy_envelope_controls_match_the_core_contract(runtime):
    scorer = get_llm_scorer(runtime)
    try:
        legacy = _provider_contract(scorer)
        core = core_runtime_provider_contract(scorer, artifact_hash="8" * 64)
    finally:
        scorer.close()

    for field in ("sampling", "thinking", "response_format"):
        assert legacy[field] == core[field], field


def test_byok_chat_contract_freezes_every_connection_control():
    scorer = get_llm_scorer(
        _chat_runtime(
            "https://byok-contract.invalid/v1",
            max_tokens=333,
            temperature=0.4,
            top_p=0.95,
            response_format_json=True,
            thinking_type="enabled",
        )
    )
    try:
        contract = _provider_contract(scorer)
    finally:
        scorer.close()

    assert contract["sampling"] == {
        "temperature": "0.4",
        "top_p": "0.95",
        "seed": None,
        "max_tokens": 333,
    }
    assert contract["thinking"] == {"enabled": True, "type": "enabled"}
    assert contract["response_format"] == "json_object"


@pytest.mark.parametrize("thinking_type", ["", None, "disabled", "off"])
def test_envelope_thinking_disabled_spellings(thinking_type):
    scorer = SimpleNamespace(
        provider="openai_compatible",
        provider_name="openai_compatible",
        model_name="m",
        temperature=0,
        max_tokens=100,
        thinking_type=thinking_type,
        response_format_json=False,
    )

    thinking = _provider_contract(scorer)["thinking"]

    assert thinking["enabled"] is False
    # 空值不发 thinking；显式的 disabled/off 原样冻结并发送，与 score_criterion 一致。
    assert thinking["type"] == ((thinking_type or "").strip() or None)


def _envelope_for(scorer):
    return scorer.build_prompt_envelope(_PAPER, _CRITERION, _CANDIDATES, [], None, None)


def test_env_configured_chat_scorer_keeps_its_l0_cache_identity(monkeypatch):
    """非 BYOK 的 Chat 实例参数就来自全局设置，冻结结果必须与旧实现逐字段相同。

    L0 缓存只对非 BYOK 运行生效，所以这条决定了本次修复会不会让已有缓存失效。
    """

    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_TEMPERATURE", 0.3)
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_MAX_TOKENS", 900)
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_THINKING_TYPE", "disabled")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON", True)
    scorer = OpenAICompatibleChatScorer(
        api_key="test-key",
        base_url="https://compatible.invalid/v1",
        model_name="glm-contract",
        provider_name="zhipu",
        client=object(),
    )

    provider = _envelope_for(scorer).to_mapping()["provider"]

    assert provider == {
        "name": "zhipu",
        "model": "glm-contract",
        "model_version": "chat-completions",
        "sampling": {"temperature": "0.3", "top_p": "1", "seed": None, "max_tokens": 900},
        "thinking": {"enabled": False, "type": "disabled"},
        "response_format": "json_object",
        "response_schema": "criterion-score-v2",
    }
    assert _envelope_for(scorer).to_mapping()["prompt_version"] == llm_cache.PROMPT_VERSION


def test_env_configured_openai_scorer_keeps_its_l0_cache_identity(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_TEMPERATURE", 0.2)
    monkeypatch.setattr(settings, "OPENAI_MAX_OUTPUT_TOKENS", 800)
    scorer = OpenAIResponsesScorer(
        api_key="test-key",
        base_url="https://openai.invalid/v1",
        model_name="gpt-contract",
        client=object(),
    )

    provider = _envelope_for(scorer).to_mapping()["provider"]

    assert provider == {
        "name": "openai",
        "model": "gpt-contract",
        "model_version": "responses-api",
        "sampling": {"temperature": "0.2", "top_p": "1", "seed": None, "max_tokens": 800},
        "thinking": {"enabled": False, "type": None},
        "response_format": "json_schema",
        "response_schema": "criterion-score-v2",
    }


def test_byok_responses_connection_options_reach_the_legacy_envelope_request(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_TEMPERATURE", 0.0)
    monkeypatch.setattr(settings, "OPENAI_MAX_OUTPUT_TOKENS", 1200)
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "resp-1", "output_text": json.dumps(_SCORE)})

    scorer = get_llm_scorer(
        _responses_runtime(
            "https://byok-responses.invalid/v1", max_output_tokens=333, temperature=0.4
        )
    )
    scorer.client.close()
    scorer.client = httpx.Client(transport=httpx.MockTransport(handler))

    result = _legacy_score(scorer)

    assert result["score"] == 8
    payload = captured[0]
    assert payload["max_output_tokens"] == 333
    assert payload["temperature"] == 0.4
    # Responses 分支的协议约束不变：强制 json_schema，从不发 thinking。
    assert payload["text"]["format"]["type"] == "json_schema"
    assert "thinking" not in payload
