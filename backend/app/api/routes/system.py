from pathlib import Path

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from backend.app.api.deps import enforce_auth
from backend.app.core.config import settings
from backend.app.db.session import get_db
from backend.app.services.deployment.readiness import build_ops_readiness
from backend.app.services.llm.diagnostics import check_connectivity
from backend.app.services.llm.factory import LOCAL_PROVIDERS
from backend.app.services.llm.factory import provider_network_scope
from backend.app.services.offline import network_touchpoints
from backend.app.services.offline import offline_ready

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/ops-readiness", dependencies=[Depends(enforce_auth)])
def ops_readiness(db: Session = Depends(get_db)):
    return build_ops_readiness(db)


@router.get("/llm-check")
def llm_check():
    """LLM 连通自检（mock 直接 ok；真实 provider 发极小请求测连通+延迟）。"""
    return check_connectivity()


@router.get("/integrations")
def integration_status():
    llm_provider = (settings.LLM_PROVIDER or "mock").lower()
    sheet_provider = (settings.SHEET_WRITER_PROVIDER or "mock").lower()
    web_dir = Path(__file__).resolve().parents[4] / "frontend" / "web"
    static_web_ready = (web_dir / "index.html").exists() and (web_dir / "assets" / "app.js").exists()
    return {
        "llm": _llm_status(llm_provider),
        "offline_ready": offline_ready(),
        "network_touchpoints": network_touchpoints(),
        "sheets": {
            "provider": sheet_provider,
            "active": sheet_provider in {"google_sheets", "google_apps_script"} and bool(settings.GOOGLE_SHEETS_WEBAPP_URL),
            "configured": bool(settings.GOOGLE_SHEETS_WEBAPP_URL)
            if sheet_provider in {"google_sheets", "google_apps_script"}
            else sheet_provider == "mock",
            "adapter": "GoogleAppsScriptSheetWriter"
            if sheet_provider in {"google_sheets", "google_apps_script"}
            else "MockSheetWriter",
            "fallback_to_mock": settings.SHEET_FALLBACK_TO_MOCK,
            "webapp_url_configured": bool(settings.GOOGLE_SHEETS_WEBAPP_URL),
            "secret_configured": bool(settings.GOOGLE_SHEETS_WEBAPP_SECRET),
        },
        "frontend": {
            "primary": "static_web",
            "static_web_ready": static_web_ready,
            "streamlit_backup": True,
            "entrypoint": "/",
        },
        "storage": {
            "provider": settings.STORAGE_PROVIDER,
            "configured": (
                settings.STORAGE_PROVIDER == "local"
                or bool(settings.SUPABASE_URL and settings.SUPABASE_SECRET_KEY)
            ),
            "bucket": (
                settings.SUPABASE_STORAGE_BUCKET
                if settings.STORAGE_PROVIDER == "supabase"
                else None
            ),
        },
    }


def _llm_status(provider):
    network = provider_network_scope()  # offline(mock) / local(本地端口) / external(外呼厂商)
    if provider in LOCAL_PROVIDERS:
        return {
            "provider": provider,
            "network": network,
            "active": bool(settings.LOCAL_LLM_BASE_URL),
            "configured": bool(settings.LOCAL_LLM_BASE_URL),
            "model": settings.LOCAL_LLM_MODEL,
            "adapter": "OpenAICompatibleChatScorer",
            "base_url": settings.LOCAL_LLM_BASE_URL,
            "fallback_to_mock": settings.LLM_FALLBACK_TO_MOCK,
            "debug_log_enabled": settings.LLM_DEBUG_LOG_ENABLED,
            "api_key_configured": bool(settings.LOCAL_LLM_API_KEY),
        }
    if provider == "openai":
        return {
            "provider": provider,
            "network": network,
            "active": bool(settings.OPENAI_API_KEY),
            "configured": bool(settings.OPENAI_API_KEY),
            "model": settings.OPENAI_MODEL,
            "adapter": "OpenAIResponsesScorer",
            "fallback_to_mock": settings.LLM_FALLBACK_TO_MOCK,
            "debug_log_enabled": settings.LLM_DEBUG_LOG_ENABLED,
            "debug_log_max_chars": settings.LLM_DEBUG_LOG_MAX_CHARS,
            "rate_limit_sleep_seconds": settings.LLM_RATE_LIMIT_SLEEP_SECONDS,
            "retry_max_delay_seconds": settings.LLM_RETRY_MAX_DELAY_SECONDS,
            "retry_429_delay_seconds": settings.LLM_429_RETRY_DELAY_SECONDS,
            "api_key_configured": bool(settings.OPENAI_API_KEY),
        }
    if provider in {"openai_compatible", "zhipu", "bigmodel", "qwen", "dashscope", "google", "gemini", "google_ai_studio"}:
        return {
            "provider": provider,
            "network": network,
            "active": bool(settings.OPENAI_COMPATIBLE_API_KEY and settings.OPENAI_COMPATIBLE_BASE_URL),
            "configured": bool(settings.OPENAI_COMPATIBLE_API_KEY and settings.OPENAI_COMPATIBLE_BASE_URL),
            "model": settings.OPENAI_COMPATIBLE_MODEL,
            "adapter": "OpenAICompatibleChatScorer",
            "fallback_to_mock": settings.LLM_FALLBACK_TO_MOCK,
            "debug_log_enabled": settings.LLM_DEBUG_LOG_ENABLED,
            "debug_log_max_chars": settings.LLM_DEBUG_LOG_MAX_CHARS,
            "rate_limit_sleep_seconds": settings.LLM_RATE_LIMIT_SLEEP_SECONDS,
            "retry_max_delay_seconds": settings.LLM_RETRY_MAX_DELAY_SECONDS,
            "retry_429_delay_seconds": settings.LLM_429_RETRY_DELAY_SECONDS,
            "api_key_configured": bool(settings.OPENAI_COMPATIBLE_API_KEY),
            "base_url_configured": bool(settings.OPENAI_COMPATIBLE_BASE_URL),
            "compatible_provider": settings.OPENAI_COMPATIBLE_PROVIDER_NAME,
            "thinking_type": settings.OPENAI_COMPATIBLE_THINKING_TYPE,
            "response_format_json": settings.OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON,
        }
    return {
        "provider": provider,
        "network": network,
        "active": False,
        "configured": provider == "mock",
        "model": "mock",
        "adapter": "MockLLMScorer",
        "fallback_to_mock": settings.LLM_FALLBACK_TO_MOCK,
        "debug_log_enabled": settings.LLM_DEBUG_LOG_ENABLED,
        "debug_log_max_chars": settings.LLM_DEBUG_LOG_MAX_CHARS,
        "rate_limit_sleep_seconds": settings.LLM_RATE_LIMIT_SLEEP_SECONDS,
        "retry_max_delay_seconds": settings.LLM_RETRY_MAX_DELAY_SECONDS,
        "retry_429_delay_seconds": settings.LLM_429_RETRY_DELAY_SECONDS,
        "api_key_configured": False,
    }
