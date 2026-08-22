import logging
from datetime import datetime
from types import SimpleNamespace

import httpx

from backend.app.core.config import settings
from backend.app.schemas.scoring import ScoreItemRead
from backend.app.services.llm.factory import get_llm_scorer
from backend.app.services.llm.openai_compatible_adapter import OpenAICompatibleChatScorer
from backend.app.services.llm.openai_compatible_adapter import _parse_chat_json_output
from backend.app.services.llm.openai_adapter import OpenAIResponsesScorer
from backend.app.services.llm.mock import MockLLMScorer
from backend.app.services.scoring.engine import _score_with_runtime_fallback
from backend.app.services.scoring.validator import validate_score_output


def test_mock_output_has_required_fields_and_valid_evidence():
    criterion = SimpleNamespace(id="c1", name="研究方法", max_score=20, evidence_hints=["研究方法"], description="")
    evidence = [
        {
            "chunk_id": "chunk-1",
            "text": "本文采用问卷调查、数据清洗、回归分析和分类模型对教学质量进行建模，说明数据来源和实验设计。",
            "location": "第三章 研究方法，第3页",
        }
    ]
    output = MockLLMScorer().score_criterion(None, criterion, evidence, [])
    validated = validate_score_output(output, criterion, evidence)

    assert validated["score"] <= 20
    assert validated["evidence_sufficient"]
    assert validated["confidence"] > 0.6
    assert validated["evidence"][0]["chunk_id"] == "chunk-1"


def test_openai_adapter_parses_structured_response_without_network():
    criterion = SimpleNamespace(id="c1", code="C01", name="研究方法", max_score=20, evidence_hints=["研究方法"], description="")
    paper = SimpleNamespace(id="p1", title="论文题目", student_id="20260001", student_name="张三")
    evidence = [
        {
            "chunk_id": "chunk-1",
            "text": "本文采用问卷调查、数据清洗、回归分析和分类模型对教学质量进行建模，说明数据来源和实验设计。",
            "location": "第三章 研究方法，第3页",
        }
    ]
    scorer = OpenAIResponsesScorer(api_key="test-key", client=FakeOpenAIClient())

    output = scorer.score_criterion(paper, criterion, evidence, [])
    validated = validate_score_output(output, criterion, evidence)

    assert validated["score"] == 18
    assert validated["criterion_id"] == "c1"
    assert validated["evidence"][0]["chunk_id"] == "chunk-1"


def test_openai_compatible_adapter_parses_chat_completion_without_network():
    criterion = SimpleNamespace(id="c1", code="C01", name="研究方法", max_score=20, evidence_hints=["研究方法"], description="")
    paper = SimpleNamespace(id="p1", title="论文题目", student_id="20260001", student_name="张三")
    evidence = [
        {
            "chunk_id": "chunk-1",
            "text": "本文采用问卷调查、数据清洗、回归分析和分类模型对教学质量进行建模，说明数据来源和实验设计。",
            "location": "第三章 研究方法，第3页",
        }
    ]
    scorer = OpenAICompatibleChatScorer(api_key="test-key", client=FakeOpenAICompatibleClient())

    output = scorer.score_criterion(paper, criterion, evidence, [])
    validated = validate_score_output(output, criterion, evidence)

    assert validated["score"] == 17
    assert validated["criterion_id"] == "c1"
    assert validated["evidence"][0]["chunk_id"] == "chunk-1"


def test_openai_compatible_adapter_sends_thinking_disabled(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_THINKING_TYPE", "disabled")
    criterion = SimpleNamespace(id="c1", code="C01", name="研究方法", max_score=20, evidence_hints=["研究方法"], description="")
    paper = SimpleNamespace(id="p1", title="论文题目", student_id="20260001", student_name="张三")
    evidence = [
        {
            "chunk_id": "chunk-1",
            "text": "本文采用问卷调查、数据清洗、回归分析和分类模型对教学质量进行建模，说明数据来源和实验设计。",
            "location": "第三章 研究方法，第3页",
        }
    ]
    client = FakeOpenAICompatibleClient()
    scorer = OpenAICompatibleChatScorer(api_key="test-key", client=client)

    scorer.score_criterion(paper, criterion, evidence, [])

    assert client.payload["thinking"] == {"type": "disabled"}


def test_openai_compatible_debug_logs_request_and_response_with_redacted_key(monkeypatch, caplog):
    monkeypatch.setattr(settings, "LLM_DEBUG_LOG_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_DEBUG_LOG_MAX_CHARS", 20000)
    caplog.set_level(logging.INFO, logger="paper_grading.llm")
    criterion = SimpleNamespace(id="c1", code="C01", name="研究方法", max_score=20, evidence_hints=["研究方法"], description="")
    paper = SimpleNamespace(id="p1", title="论文题目", student_id="20260001", student_name="张三")
    evidence = [
        {
            "chunk_id": "chunk-1",
            "text": "本文采用问卷调查、数据清洗、回归分析和分类模型对教学质量进行建模，说明数据来源和实验设计。",
            "location": "第三章 研究方法，第3页",
        }
    ]
    scorer = OpenAICompatibleChatScorer(api_key="test-key", client=FakeOpenAICompatibleClient())

    scorer.score_criterion(paper, criterion, evidence, [])

    messages = "\n".join(caplog.messages)
    assert "[LLM request]" in messages
    assert "[LLM response]" in messages
    assert "chat/completions" in messages
    assert "论文题目" in messages
    assert "chatcmpl_test" in messages
    assert "Bearer test-key" not in messages
    assert '"Authorization": "Bearer' not in messages
    assert "***REDACTED***" in messages


def test_openai_compatible_429_uses_long_retry_delay(monkeypatch, caplog):
    # Raw diagnostic logging is opt-in after the BYOK privacy hardening.
    monkeypatch.setattr(settings, "LLM_DEBUG_LOG_ENABLED", True)
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_MAX_RETRIES", 1)
    monkeypatch.setattr(settings, "LLM_429_RETRY_DELAY_SECONDS", 12)
    monkeypatch.setattr(settings, "LLM_RETRY_MAX_DELAY_SECONDS", 30)
    delays = []
    monkeypatch.setattr(
        "backend.app.services.llm.openai_compatible_adapter.time.sleep",
        lambda seconds: delays.append(seconds),
    )
    caplog.set_level(logging.INFO, logger="paper_grading.llm")
    criterion = SimpleNamespace(id="c1", code="C01", name="研究方法", max_score=20, evidence_hints=["研究方法"], description="")
    paper = SimpleNamespace(id="p1", title="论文题目", student_id="20260001", student_name="张三")
    evidence = [
        {
            "chunk_id": "chunk-1",
            "text": "本文采用问卷调查、数据清洗、回归分析和分类模型对教学质量进行建模，说明数据来源和实验设计。",
            "location": "第三章 研究方法，第3页",
        }
    ]
    client = FakeRateLimitThenSuccessClient()
    scorer = OpenAICompatibleChatScorer(api_key="test-key", client=client)

    output = scorer.score_criterion(paper, criterion, evidence, [])

    assert output["provider_response_id"] == "chatcmpl_test"
    assert client.calls == 2
    assert delays == [12]
    messages = "\n".join(caplog.messages)
    assert "[LLM retry sleep]" in messages
    assert "rate_limited_429" in messages


def test_openai_compatible_empty_content_error_has_diagnostics():
    response = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"role": "assistant", "content": "", "reasoning_content": "思考内容" * 20},
            }
        ]
    }

    try:
        _parse_chat_json_output(response)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected empty content to raise")

    assert "reasoning_content_chars" in message
    assert "thinking 模式" in message


def test_score_output_normalizes_loose_llm_shapes():
    criterion = SimpleNamespace(id="c1", name="研究方法", max_score=20)
    evidence = [
        {
            "chunk_id": "chunk-1",
            "text": "本文采用问卷调查、数据清洗、回归分析和分类模型对教学质量进行建模，说明数据来源和实验设计。",
            "location": "第三章 研究方法，第3页",
        }
    ]
    output = {
        "criterion_id": "c1",
        "criterion_name": "研究方法",
        "max_score": 20,
        "score": 16,
        "evidence_sufficient": True,
        "reason": "评分理由",
        "deductions": 0.0,
        "evidence": {"quote": "本文采用问卷调查", "location": "第三章", "chunk_id": "chunk-1"},
        "suggestion": None,
        "confidence": 0.8,
        "need_manual_review": False,
    }

    validated = validate_score_output(output, criterion, evidence)

    assert validated["deductions"] == []
    assert validated["evidence"] == [{"quote": "本文采用问卷调查", "location": "第三章", "chunk_id": "chunk-1"}]
    assert validated["suggestion"] == ""


def test_score_item_read_normalizes_legacy_bad_json_fields():
    payload = SimpleNamespace(
        id="i1",
        scoring_run_id="r1",
        criterion_id="c1",
        criterion_name="研究方法",
        criterion_code="C01",
        max_score=20,
        ai_score=16,
        final_score=16,
        evidence_sufficient=True,
        reason="评分理由",
        deductions=0.0,
        evidence={"quote": "原文", "location": "第三章", "chunk_id": "chunk-1"},
        suggestion="",
        confidence=0.8,
        need_manual_review=False,
        created_at=datetime(2026, 5, 22),
    )

    item = ScoreItemRead.model_validate(payload)

    assert item.deductions == []
    assert item.evidence == [{"quote": "原文", "location": "第三章", "chunk_id": "chunk-1"}]


def test_factory_returns_openai_compatible_scorer(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai_compatible")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_API_KEY", "test-key")
    scorer = get_llm_scorer()
    assert isinstance(scorer, OpenAICompatibleChatScorer)


def test_factory_local_provider_uses_local_defaults(monkeypatch):
    # LLM_PROVIDER=local 走本地私有模型：OpenAI 兼容 adapter + LOCAL_LLM_* 本地端点，无需 API Key。
    monkeypatch.setattr(settings, "LLM_PROVIDER", "local")
    monkeypatch.setattr(settings, "LOCAL_LLM_BASE_URL", "http://localhost:8080/v1")
    monkeypatch.setattr(settings, "LOCAL_LLM_API_KEY", None)
    scorer = get_llm_scorer()
    assert isinstance(scorer, OpenAICompatibleChatScorer)
    assert scorer.base_url == "http://localhost:8080/v1"


def test_provider_network_scope(monkeypatch):
    from backend.app.services.llm.factory import provider_network_scope

    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")
    assert provider_network_scope() == "offline"
    monkeypatch.setattr(settings, "LLM_PROVIDER", "local")
    assert provider_network_scope() == "local"
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai_compatible")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    assert provider_network_scope() == "external"
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_BASE_URL", "http://localhost:8000/v1")
    assert provider_network_scope() == "local"


def test_llm_runtime_error_falls_back_to_mock_and_requires_review(monkeypatch):
    monkeypatch.setattr(settings, "LLM_FALLBACK_TO_MOCK", True)
    monkeypatch.setattr(settings, "LLM_RATE_LIMIT_SLEEP_SECONDS", 0)
    criterion = SimpleNamespace(id="c1", code="C01", name="研究方法", max_score=20, evidence_hints=["研究方法"], description="")
    paper = SimpleNamespace(id="p1", title="论文题目", student_id="20260001", student_name="张三")
    evidence = [
        {
            "chunk_id": "chunk-1",
            "text": "本文采用问卷调查、数据清洗、回归分析和分类模型对教学质量进行建模，说明数据来源和实验设计。",
            "location": "第三章 研究方法，第3页",
        }
    ]

    output = _score_with_runtime_fallback(FailingScorer(), paper, criterion, evidence, [])
    validated = validate_score_output(output, criterion, evidence)

    assert validated["fallback_from_provider"] == "openai_compatible"
    assert "SSL" in validated["fallback_reason"]
    assert validated["need_manual_review"] is True
    assert validated["confidence"] <= 0.6
    assert any("降级为 Mock 评分" in item for item in validated["deductions"])


class FakeOpenAIClient:
    def post(self, url, headers, json):
        assert url.endswith("/responses")
        assert headers["Authorization"] == "Bearer test-key"
        assert json["text"]["format"]["type"] == "json_schema"
        return FakeOpenAIResponse()


class FakeOpenAIResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    @property
    def text(self):
        return '{"id":"resp_test","output_text":"..."}'

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "id": "resp_test",
            "output_text": (
                '{"criterion_id":"c1","criterion_name":"研究方法","max_score":20,'
                '"score":18,"evidence_sufficient":true,"reason":"证据充分，方法说明较完整。",'
                '"deductions":["方法局限讨论略少。"],'
                '"evidence":[{"quote":"本文采用问卷调查、数据清洗、回归分析和分类模型对教学质量进行建模，说明数据来源和实验设计。",'
                '"location":"第三章 研究方法，第3页","chunk_id":"chunk-1"}],'
                '"suggestion":"补充方法局限与参数说明。","confidence":0.86,"need_manual_review":false}'
            ),
        }


class FakeOpenAICompatibleClient:
    payload = None

    def post(self, url, headers, json):
        assert url.endswith("/chat/completions")
        assert headers["Authorization"] == "Bearer test-key"
        assert json["model"]
        assert json["messages"][0]["role"] == "system"
        self.payload = json
        return FakeOpenAICompatibleResponse()


class FakeRateLimitThenSuccessClient:
    def __init__(self):
        self.calls = 0

    def post(self, url, headers, json):
        self.calls += 1
        if self.calls == 1:
            return FakeRateLimitResponse(url)
        return FakeOpenAICompatibleResponse()


class FakeRateLimitResponse:
    status_code = 429
    headers = {"content-type": "application/json"}
    text = '{"error":{"message":"rate limit exceeded"}}'

    def __init__(self, url):
        self.request = httpx.Request("POST", url)
        self.response = httpx.Response(
            self.status_code,
            request=self.request,
            headers=self.headers,
            content=self.text.encode("utf-8"),
        )

    def raise_for_status(self):
        raise httpx.HTTPStatusError(
            "Client error '429 Too Many Requests'",
            request=self.request,
            response=self.response,
        )


class FakeOpenAICompatibleResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    @property
    def text(self):
        return (
            '{"id":"chatcmpl_test","choices":[{"message":{"role":"assistant",'
            '"content":"{\\"criterion_id\\":\\"c1\\"}"}}]}'
        )

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "id": "chatcmpl_test",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            '{"criterion_id":"c1","criterion_name":"研究方法","max_score":20,'
                            '"score":17,"evidence_sufficient":true,"reason":"证据能覆盖研究方法和实验设计。",'
                            '"deductions":["参数细节说明不足。"],'
                            '"evidence":[{"quote":"本文采用问卷调查、数据清洗、回归分析和分类模型对教学质量进行建模，说明数据来源和实验设计。",'
                            '"location":"第三章 研究方法，第3页","chunk_id":"chunk-1"}],'
                            '"suggestion":"补充参数和实验设置。","confidence":0.82,"need_manual_review":false}'
                        ),
                    }
                }
            ],
        }


class FailingScorer:
    provider = "openai_compatible"
    model_name = "glm-4.7-flash"
    model_version = "chat-completions"

    def score_criterion(self, paper, criterion, evidence_candidates, structure_checks, anchors=None):
        raise RuntimeError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
