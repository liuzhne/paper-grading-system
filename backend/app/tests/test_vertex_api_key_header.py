"""Vertex AI 的 OpenAI 兼容端点用 x-goog-api-key 传 Key，不用 Bearer（2026-10-15 测试连接 401）。"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from backend.app.core.config import settings
from backend.app.services.ai_connections import ConnectionRuntime
from backend.app.services.ai_connections import verify_connection_runtime
from backend.app.services.llm.auth_headers import is_vertex_host
from backend.app.services.llm.auth_headers import openai_auth_headers
from backend.app.services.llm.openai_adapter import OpenAIResponsesScorer
from backend.app.services.llm.openai_compatible_adapter import OpenAICompatibleChatScorer


KEY = "AQ.vertex-express-key-for-tests-0123456789abcdef"
VERTEX = "https://aiplatform.googleapis.com/v1/projects/demo-project/locations/global/endpoints/openapi"
REGIONAL = "https://us-central1-aiplatform.googleapis.com/v1/projects/demo-project/locations/us-central1/endpoints/openapi"


@pytest.mark.parametrize(
    "base_url, vertex",
    [
        (VERTEX, True),
        (REGIONAL, True),
        ("https://AIPLATFORM.googleapis.com./v1/projects/p/locations/global/endpoints/openapi", True),
        # Gemini API 的 OpenAI 兼容端点接受 Bearer，保持原样。
        ("https://generativelanguage.googleapis.com/v1beta/openai", False),
        ("https://api.openai.com/v1", False),
        ("https://bedrock-runtime.ap-southeast-1.amazonaws.com/openai/v1", False),
        # 形似但不是 Google 的主机，不能把 Key 换个头发出去。
        ("https://aiplatform.googleapis.com.example.com/v1", False),
        ("https://fakeaiplatform.googleapis.com/v1", False),
        ("", False),
    ],
)
def test_only_vertex_hosts_get_the_goog_api_key_header(base_url, vertex):
    assert is_vertex_host(base_url) is vertex
    headers = openai_auth_headers(base_url, KEY)
    if vertex:
        assert headers == {"x-goog-api-key": KEY}
    else:
        assert headers == {"Authorization": "Bearer %s" % KEY}


class _ProbeClient:
    def __init__(self):
        self.calls = []

    def __call__(self, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, url, *, json, headers):
        self.calls.append((url, headers))
        return httpx.Response(200, json={}, request=httpx.Request("POST", url))


def test_connection_test_sends_the_vertex_key_in_x_goog_api_key(monkeypatch):
    client = _ProbeClient()
    monkeypatch.setattr("backend.app.services.ai_connections.validate_outbound_base_url", lambda value: value)
    monkeypatch.setattr("backend.app.services.ai_connections.httpx.Client", client)

    verify_connection_runtime(ConnectionRuntime(
        connection_id="c", key_version=1, organization_id="o", provider_type="openai_compatible",
        base_url=VERTEX, model_name="google/gemini-2.5-flash", provider_options={}, api_key=KEY,
    ))

    url, headers = client.calls[0]
    assert url == VERTEX + "/chat/completions"
    assert headers == {"x-goog-api-key": KEY}


_SCORE = {
    "criterion_id": "c1", "criterion_name": "研究方法", "max_score": 20, "score": 17,
    "evidence_sufficient": True, "reason": "证据能覆盖研究方法。", "deductions": [],
    "evidence": [{"quote": "本文采用问卷调查与回归分析。", "location": "第3页", "chunk_id": "chunk-1"}],
    "suggestion": "补充参数说明。", "confidence": 0.82, "need_manual_review": False,
}


class _ChatClient:
    def __init__(self):
        self.headers = None

    def post(self, url, headers, json):
        assert url == VERTEX + "/chat/completions"
        self.headers = headers
        body = {"id": "chatcmpl_vertex", "choices": [{"message": {"role": "assistant", "content": _dumps(_SCORE)}}]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))


def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


def _score(scorer):
    criterion = SimpleNamespace(id="c1", code="C01", name="研究方法", max_score=20, evidence_hints=["研究方法"], description="")
    paper = SimpleNamespace(id="p1", title="论文题目", student_id="20260001", student_name="张三")
    evidence = [{"chunk_id": "chunk-1", "text": "本文采用问卷调查与回归分析。", "location": "第3页"}]
    return scorer.score_criterion(paper, criterion, evidence, [])


def test_scoring_calls_to_vertex_use_x_goog_api_key_and_never_bearer():
    client = _ChatClient()
    scorer = OpenAICompatibleChatScorer(
        api_key=KEY, base_url=VERTEX, model_name="google/gemini-2.5-flash", provider_name="vertex", client=client,
    )

    _score(scorer)

    assert client.headers["x-goog-api-key"] == KEY
    assert "Authorization" not in client.headers
    assert client.headers["Content-Type"] == "application/json"


def test_responses_adapter_uses_the_same_rule():
    seen = {}

    class _Client:
        def post(self, url, headers, json):
            seen.update(headers)
            raise httpx.ConnectError("stop after capturing headers")

    scorer = OpenAIResponsesScorer(api_key=KEY, base_url=VERTEX, model_name="google/gemini-2.5-flash", client=_Client())
    with pytest.raises(Exception):
        scorer._post_with_retry({"model": "m", "input": "x"}, attempts_limit=1)

    assert seen.get("x-goog-api-key") == KEY and "Authorization" not in seen


def test_debug_logging_redacts_the_goog_api_key_header(monkeypatch, caplog):
    monkeypatch.setattr(settings, "LLM_DEBUG_LOG_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_DEBUG_LOG_MAX_CHARS", 20000)
    caplog.set_level(logging.INFO, logger="paper_grading.llm")
    scorer = OpenAICompatibleChatScorer(
        api_key=KEY, base_url=VERTEX, model_name="google/gemini-2.5-flash", provider_name="vertex", client=_ChatClient(),
    )

    _score(scorer)

    messages = "\n".join(caplog.messages)
    assert "[LLM request]" in messages
    assert KEY not in messages
    assert "***REDACTED***" in messages
