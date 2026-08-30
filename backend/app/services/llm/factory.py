from backend.app.core.config import settings
from backend.app.services.llm.mock import MockLLMScorer
from backend.app.services.llm.openai_compatible_adapter import OpenAICompatibleChatScorer
from backend.app.services.llm.openai_adapter import OpenAIResponsesScorer
from backend.app.services.ai_connections import ConnectionRuntime

# 云·OpenAI 兼容厂商别名（走 OPENAI_COMPATIBLE_*）
COMPATIBLE_PROVIDERS = {"openai_compatible", "zhipu", "bigmodel", "qwen", "dashscope", "google", "gemini", "google_ai_studio"}
# 本地私有模型别名（走 LOCAL_LLM_*，OpenAI 兼容协议连本地端口）
LOCAL_PROVIDERS = {"local", "llama", "llamacpp", "llama_cpp", "vllm", "ollama"}


def get_llm_scorer(connection_runtime: ConnectionRuntime | None = None):
    """Return a scorer for either deployment config or one bound BYOK runtime.

    A supplied runtime is intentionally fail-closed: it never consults global
    provider selection or falls back to Mock, because that would bill or expose
    a submission through a different account than the task selected.
    """

    if connection_runtime is not None:
        options = connection_runtime.provider_options
        if connection_runtime.provider_type == "openai_responses":
            scorer = OpenAIResponsesScorer(
                api_key=connection_runtime.api_key,
                base_url=connection_runtime.base_url,
                model_name=connection_runtime.model_name,
                timeout_seconds=options.get("timeout_seconds"),
                max_output_tokens=options.get("max_output_tokens"),
                temperature=options.get("temperature"),
            )
        elif connection_runtime.provider_type == "openai_compatible":
            scorer = OpenAICompatibleChatScorer(
                api_key=connection_runtime.api_key,
                base_url=connection_runtime.base_url,
                model_name=connection_runtime.model_name,
                provider_name="openai_compatible",
                timeout_seconds=options.get("timeout_seconds"),
                max_tokens=options.get("max_tokens"),
                temperature=options.get("temperature"),
                response_format_json=options.get("response_format_json"),
                # BYOK endpoints are only protocol-compatible, not guaranteed
                # to accept the platform provider's non-standard `thinking`
                # extension.  Send it only when this connection opted in.
                thinking_type=options.get("thinking_type", ""),
            )
        else:  # Defensive even though the persistence validator already rejects it.
            raise ValueError("unsupported AI connection provider type")
        scorer._ai_connection_snapshot = connection_runtime.snapshot()
        scorer._ai_connection_organization_id = connection_runtime.organization_id
        return scorer

    provider = (settings.LLM_PROVIDER or "mock").lower()
    if provider == "mock":
        return MockLLMScorer()
    if settings.AUTH_ENABLED and settings.AUTH_PASSWORD and not settings.PLATFORM_MANAGED_LLM_ENABLED:
        raise RuntimeError(
            "platform-managed LLM is disabled for authenticated deployments; "
            "bind a private AI connection or explicitly authorize the platform model"
        )
    if provider == "openai":
        try:
            return OpenAIResponsesScorer()
        except ValueError:
            if settings.LLM_FALLBACK_TO_MOCK:
                return MockLLMScorer()
            raise
    if provider in LOCAL_PROVIDERS:
        try:
            # 本地私有模型：同 OpenAI 兼容 adapter，但用 LOCAL_LLM_* 本地默认；
            # 本地多数无需鉴权，给占位 key 以过空校验（llama.cpp/Ollama 忽略它）。
            return OpenAICompatibleChatScorer(
                api_key=settings.LOCAL_LLM_API_KEY or "local",
                base_url=settings.LOCAL_LLM_BASE_URL,
                model_name=settings.LOCAL_LLM_MODEL,
                provider_name=provider,
            )
        except ValueError:
            if settings.LLM_FALLBACK_TO_MOCK:
                return MockLLMScorer()
            raise
    if provider in COMPATIBLE_PROVIDERS:
        try:
            return OpenAICompatibleChatScorer()
        except ValueError:
            if settings.LLM_FALLBACK_TO_MOCK:
                return MockLLMScorer()
            raise
    raise ValueError("unsupported LLM_PROVIDER: %s" % settings.LLM_PROVIDER)


def _is_local_url(url):
    url = (url or "").lower()
    return any(h in url for h in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "host.docker.internal"))


def provider_network_scope():
    """当前 LLM 配置的网络属性：
    - offline：mock，完全不触网；
    - local：连本地端口（本地私有模型，或 openai_compatible 被指向 localhost）；
    - external：外呼厂商云。
    """
    provider = (settings.LLM_PROVIDER or "mock").lower()
    if provider == "mock":
        return "offline"
    if provider in LOCAL_PROVIDERS:
        return "local"
    if provider == "openai":
        base = settings.OPENAI_BASE_URL
    elif provider in COMPATIBLE_PROVIDERS:
        base = settings.OPENAI_COMPATIBLE_BASE_URL
    else:
        base = ""
    return "local" if _is_local_url(base) else "external"
