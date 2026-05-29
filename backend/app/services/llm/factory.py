from backend.app.core.config import settings
from backend.app.services.llm.mock import MockLLMScorer
from backend.app.services.llm.openai_compatible_adapter import OpenAICompatibleChatScorer
from backend.app.services.llm.openai_adapter import OpenAIResponsesScorer


def get_llm_scorer():
    provider = (settings.LLM_PROVIDER or "mock").lower()
    if provider == "mock":
        return MockLLMScorer()
    if provider == "openai":
        try:
            return OpenAIResponsesScorer()
        except ValueError:
            if settings.LLM_FALLBACK_TO_MOCK:
                return MockLLMScorer()
            raise
    if provider in {"openai_compatible", "zhipu", "bigmodel", "qwen", "dashscope"}:
        try:
            return OpenAICompatibleChatScorer()
        except ValueError:
            if settings.LLM_FALLBACK_TO_MOCK:
                return MockLLMScorer()
            raise
    raise ValueError("unsupported LLM_PROVIDER: %s" % settings.LLM_PROVIDER)
