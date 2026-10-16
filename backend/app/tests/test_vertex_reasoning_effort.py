"""Google 主机上关闭思考要发 reasoning_effort（2026-10-16 评分调用被 Gemini 思考撑满输出上限而截断）。"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.app.services.llm.core_adapter import core_runtime_provider_contract
from backend.app.services.llm.openai_compatible_adapter import OpenAICompatibleChatScorer
from backend.app.services.llm.thinking_params import GOOGLE_PROVIDER_DEFAULT
from backend.app.services.llm.thinking_params import effective_thinking_type
from backend.app.services.llm.thinking_params import google_can_disable_thinking
from backend.app.services.llm.thinking_params import is_google_openai_host
from backend.app.services.llm.thinking_params import thinking_request_fields


VERTEX = "https://aiplatform.googleapis.com/v1/projects/demo/locations/global/endpoints/openapi"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/openai"
ZHIPU = "https://open.bigmodel.cn/api/paas/v4"


@pytest.mark.parametrize(
    "base_url, google",
    [
        (VERTEX, True),
        ("https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1/endpoints/openapi", True),
        (GEMINI_API, True),
        (ZHIPU, False),
        ("https://openrouter.ai/api/v1", False),
        ("https://generativelanguage.googleapis.com.example.com/v1", False),
        ("", False),
    ],
)
def test_google_openai_hosts(base_url, google):
    assert is_google_openai_host(base_url) is google


@pytest.mark.parametrize(
    "model, can_disable",
    [
        ("google/gemini-2.5-flash", True),
        ("gemini-2.5-flash-lite", True),
        ("models/gemini-2.5-flash-preview-09-2025", True),
        # 2.5 Pro 与 Gemini 3 关不掉思考；2.0 Flash 本来就不思考，不发参数。
        ("google/gemini-2.5-pro", False),
        ("gemini-3-flash", False),
        ("gemini-2.0-flash", False),
    ],
)
def test_only_gemini_25_flash_can_disable_thinking(model, can_disable):
    assert google_can_disable_thinking(model) is can_disable


@pytest.mark.parametrize(
    "base_url, model, configured, effective, fields",
    [
        # Vertex + 2.5 Flash：关闭思考改发 reasoning_effort，不再发 Vertex 不认的 thinking 字段。
        (VERTEX, "google/gemini-2.5-flash", "disabled", "none", {"reasoning_effort": "none"}),
        (GEMINI_API, "gemini-2.5-flash", "off", "none", {"reasoning_effort": "none"}),
        # 关不掉或没配置：不发参数，身份如实记为按模型默认思考。
        (VERTEX, "google/gemini-2.5-pro", "disabled", GOOGLE_PROVIDER_DEFAULT, {}),
        (VERTEX, "google/gemini-2.5-flash", None, GOOGLE_PROVIDER_DEFAULT, {}),
        (VERTEX, "google/gemini-2.5-flash", "enabled", "enabled", {}),
        # 其他主机原样：智谱的 thinking 字段照发。
        (ZHIPU, "glm-4.7-flash", "disabled", "disabled", {"thinking": {"type": "disabled"}}),
        (ZHIPU, "glm-4.7-flash", None, None, {}),
    ],
)
def test_thinking_setting_is_translated_per_host(base_url, model, configured, effective, fields):
    assert effective_thinking_type(base_url, model, configured) == effective
    assert thinking_request_fields(base_url, model, effective) == fields


def _scorer(base_url, model, calls, *, thinking_type=None):
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"items": []}'},
                                                      "finish_reason": "stop"}]})

    return OpenAICompatibleChatScorer(
        api_key="test-key", base_url=base_url, model_name=model, thinking_type=thinking_type,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_vertex_flash_requests_disable_thinking_with_reasoning_effort():
    calls = []
    scorer = _scorer(VERTEX, "google/gemini-2.5-flash", calls)  # 连接没配，取全局默认 disabled
    scorer.complete_json("系统", {"units": []}, attempts_limit=1)
    assert calls[0]["reasoning_effort"] == "none"
    assert "thinking" not in calls[0]


def test_other_hosts_keep_the_thinking_field():
    calls = []
    scorer = _scorer(ZHIPU, "glm-4.7-flash", calls, thinking_type="disabled")
    scorer.complete_json("系统", {"units": []}, attempts_limit=1)
    assert calls[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in calls[0]


@pytest.mark.parametrize(
    "model, thinking",
    [
        ("google/gemini-2.5-flash", {"enabled": False, "type": "none"}),
        ("google/gemini-2.5-pro", {"enabled": True, "type": GOOGLE_PROVIDER_DEFAULT}),
    ],
)
def test_runtime_identity_records_what_vertex_actually_does(model, thinking):
    # 身份变了，开着思考时的旧判定就不会被决策账本与缓存当作同一请求复用。
    scorer = _scorer(VERTEX, model, [])
    contract = core_runtime_provider_contract(scorer, artifact_hash="sha256:" + "0" * 64)
    assert contract["thinking"] == thinking


def test_raising_the_default_output_cap_keeps_the_evidence_budget():
    # 输入预算 = 上下文窗口 − 输出上限 − 安全余量。只调大输出上限会挤掉送给模型的证据；
    # 2026-10-16 两个默认值一起调，输入预算保持 1200 时代的 5968。
    from backend.app.core.config import Settings

    fields = Settings.model_fields
    window = fields["SCORING_CONTEXT_WINDOW_TOKENS"].default
    output = fields["OPENAI_COMPATIBLE_MAX_TOKENS"].default
    margin = fields["SCORING_CONTEXT_SAFETY_MARGIN_TOKENS"].default
    assert output == 2048
    assert window - output - margin == 5968


def test_diagnose_llm_reports_the_reasoning_effort_it_sent(monkeypatch, capsys):
    # 只构造请求、用假响应，不发真实调用。
    from backend.app.core.config import settings
    from backend.app.scripts import diagnose_llm

    sent = {}

    def fake_post(url, headers, json, timeout):
        sent.update(json)
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}]})

    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_API_KEY", "test-key")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_BASE_URL", VERTEX)
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_MODEL", "google/gemini-2.5-flash")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_THINKING_TYPE", "disabled")
    monkeypatch.setattr(diagnose_llm.httpx, "post", fake_post)

    diagnose_llm.main()
    report = json.loads(capsys.readouterr().out)
    assert sent["reasoning_effort"] == "none" and "thinking" not in sent
    assert report["reasoning_effort"] == "none"
    assert report["thinking"] is None
