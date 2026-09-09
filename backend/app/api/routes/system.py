from pathlib import Path

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from backend.app.api.deps import CurrentPrincipal
from backend.app.api.deps import current_principal
from backend.app.api.deps import require_organization_role
from backend.app.api.deps import require_platform_admin
from backend.app.api.deps import require_selected_organization
from backend.app.core.config import settings
from backend.app.db.session import get_db
from backend.app.schemas.system import CapabilitiesRead
from backend.app.schemas.system import PlatformLLMConfigWrite
from backend.app.services.auth import auth_active
from backend.app.services.deployment.readiness import build_ops_readiness
from backend.app.services.deployment.readiness import build_organization_readiness
from backend.app.services.llm.diagnostics import check_connectivity
from backend.app.services.llm.factory import LOCAL_PROVIDERS
from backend.app.services.llm.factory import provider_network_scope
from backend.app.services import platform_llm
from backend.app.services.ai_connections import verify_connection_runtime
from backend.app.services.auth import audit
from backend.app.db.models import utcnow
from fastapi import HTTPException
from backend.app.services.offline import network_touchpoints
from backend.app.services.offline import offline_ready
from backend.app.services.llm_observability import observability_status

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/ops-readiness")
def ops_readiness(
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """Platform-wide readiness. Restricted to platform administrators.

    Carries host facts (disk, database size, deployment security) that must not
    reach an organization administrator; they use ``/organization-readiness``
    instead (frontend v2 plan §2.1).
    """
    require_platform_admin(principal)
    return build_ops_readiness(db)


@router.get("/organization-readiness")
def organization_readiness(
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """Batch-scoring health for the caller's currently selected organization.

    Filters before aggregating; a missing organization context is rejected
    rather than silently widened to a platform-wide query.
    """
    organization_id = require_selected_organization(principal)
    require_organization_role(principal, "org_admin")
    return build_organization_readiness(db, organization_id)


def _llm_availability(db: Session, principal: CurrentPrincipal) -> dict:
    """这个用户现在到底能不能调模型（D-027、D-028）。

    两条来源任一可用即可：平台管理员配了平台默认模型，或用户自己绑了 BYOK 连接。
    **停用的都不算**——放行到一个必然失败的流程比直接挡住更糟。
    """

    from sqlalchemy import select

    from backend.app.db.models import AIConnection

    # 开发模式（`AUTH_ENABLED=false`）的模型来自环境变量——解析顺序里就是这么定的
    # （D-028）。能力表漏算这条，本地开发与非鉴权验收会被前端守卫全数拦下，而那些
    # 环境本来就能正常调用。
    if not auth_active():
        return {
            "platform_model_available": True,
            "has_own_connection": False,
            "can_use_llm": True,
        }

    platform_available = platform_llm.get_active_config(db) is not None

    has_own = False
    if principal.user_id:
        query = select(AIConnection.id).where(
            AIConnection.owner_id == principal.user_id,
            AIConnection.status == "active",
            AIConnection.deleted_at.is_(None),
        )
        has_own = db.scalar(query) is not None

    return {
        "platform_model_available": platform_available,
        "has_own_connection": has_own,
        "can_use_llm": platform_available or has_own,
    }


@router.get("/capabilities", response_model=CapabilitiesRead)
def capabilities(
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """鉴权后的角色能力与部署能力投影（前端 v2 计划 §2.1、§6）。

    前端只用它决定导航与控件可见性；**真正的权限一律由各端点服务端执行**。
    本响应不包含任何 Key、Secret 或平台敏感配置。
    """
    # 与 deps.require_platform_admin / require_selected_organization 保持同一判据：
    # 关闭鉴权的显式开发模式下守卫放行，能力表也必须放行，否则会出现
    # 「页面可访问但导航不显示」的错位。它不是生产权限证明，调用方看
    # auth_enforced 字段即可区分。
    dev_mode = not auth_active()
    is_platform_admin = dev_mode or principal.platform_role == "platform_admin"
    is_org_admin = is_platform_admin or principal.organization_role == "org_admin"
    has_org_context = dev_mode or bool(principal.organization_id)
    sheet_provider = (settings.SHEET_WRITER_PROVIDER or "mock").lower()
    sheets_available = (
        not settings.OFFLINE_MODE
        and sheet_provider in {"google_sheets", "google_apps_script"}
        and bool(settings.GOOGLE_SHEETS_WEBAPP_URL)
    )
    return {
        "user_id": principal.user_id,
        "organization_id": principal.organization_id,
        "organization_role": principal.organization_role,
        "platform_role": principal.platform_role,
        "auth_enforced": auth_active(),
        # 前端据此决定是否把用户引导去配置 BYOK。没有这个字段，前端只能等某次
        # 调用炸了才知道用不了——那时用户已经上传完材料了。
        "llm": _llm_availability(db, principal),
        "abilities": {
            # 组织视图需要已选定组织；缺上下文时端点会拒绝，导航也不应出现。
            "view_organization_ops": is_org_admin and has_org_context,
            "view_platform_ops": is_platform_admin,
            "manage_members": is_org_admin,
            "manage_own_ai_connections": True,
        },
        "upload": {
            "provider": settings.STORAGE_PROVIDER,
            "max_size_mb": settings.DIRECT_UPLOAD_MAX_SIZE_MB,
            "tus_threshold_mb": settings.DIRECT_UPLOAD_TUS_THRESHOLD_MB,
            "accepted_extensions": [".docx", ".pdf"],
        },
        "export": {
            "sheets_available": sheets_available,
            "offline_mode": settings.OFFLINE_MODE,
        },
    }


@router.get("/llm-check")
def llm_check(principal: CurrentPrincipal = Depends(current_principal)):
    """LLM 连通自检（mock 直接 ok；真实 provider 发极小请求测连通+延迟）。

    这条检查用的是**平台配置的** provider，会暴露部署侧的连通性与延迟，
    因此限平台管理员。用户自带连接（BYOK）的测试走 `/ai-connections`
    的所有者校验路径，不受此限制（前端 v2 计划 §2.1）。
    """
    require_platform_admin(principal)
    return check_connectivity()


@router.get("/integrations")
def integration_status():
    llm_provider = (settings.LLM_PROVIDER or "mock").lower()
    sheet_provider = (settings.SHEET_WRITER_PROVIDER or "mock").lower()
    web_dir = Path(__file__).resolve().parents[4] / "frontend" / "web"
    static_web_ready = (web_dir / "index.html").exists() and (web_dir / "assets" / "app.js").exists()
    return {
        "llm": _llm_status(llm_provider),
        "llm_observability": observability_status(),
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


# --- 平台默认模型（D-028） -------------------------------------------------
#
# 配置它等于决定「所有没绑 BYOK 的用户用哪个模型、花谁的钱」，因此限平台管理员。
# 组织管理员走不到这里：这条配置跨组织生效。


@router.get("/platform-llm")
def read_platform_llm(
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """脱敏读。**永远不回显密钥**，连密文字段也不出现。"""
    require_platform_admin(principal)
    return platform_llm.masked_view(platform_llm.get_active_or_disabled(db))


# 用 POST 而不是 PUT：语义是「写入或替换」，且前端 api 客户端只暴露
# get/post/patch/del——为一个端点新增一种客户端方法，不如让端点用已有的动词。
@router.post("/platform-llm")
def write_platform_llm(
    payload: PlatformLLMConfigWrite,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """写入或替换。单例——再配一次是替换，不是新增。"""
    require_platform_admin(principal)
    try:
        config = platform_llm.set_config(
            db,
            provider_type=payload.provider_type,
            base_url=payload.base_url,
            model_name=payload.model_name,
            api_key=payload.api_key,
            configured_by=principal.user_id or "unknown",
            provider_options=payload.provider_options,
        )
    except ValueError as exc:
        # 供应商不受支持、key 缺失或过短、base_url 越界都算入参问题。
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(
        db,
        "platform_llm.configured",
        actor_id=principal.user_id,
        metadata={
            "provider_type": config.provider_type,
            "model_name": config.model_name,
            "key_version": config.key_version,
        },
    )
    db.commit()
    return platform_llm.masked_view(platform_llm.get_active_or_disabled(db))


@router.post("/platform-llm/test")
def test_platform_llm(
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """对已保存的配置试连。失败要记下来，别让页面只显示一个红字。"""
    require_platform_admin(principal)
    config = platform_llm.get_active_config(db)
    if config is None:
        raise HTTPException(status_code=404, detail="尚未配置平台默认模型。")
    try:
        verified = verify_connection_runtime(platform_llm.runtime_for(config))
    except ValueError as exc:
        config.last_error_code = "PLATFORM_LLM_TEST_FAILED"
        audit(
            db,
            "platform_llm.verification_failed",
            actor_id=principal.user_id,
            metadata={"error_code": config.last_error_code},
        )
        db.commit()
        raise HTTPException(status_code=400, detail="平台模型连接测试失败。") from exc
    config.last_verified_at = utcnow()
    config.last_error_code = None
    audit(db, "platform_llm.verified", actor_id=principal.user_id, metadata={})
    db.commit()
    return {**verified, "status": "verified"}


@router.post("/platform-llm/disable")
def disable_platform_llm(
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """停用但保留配置——出问题时不必先删再重配。"""
    require_platform_admin(principal)
    config = platform_llm.disable_config(db, actor_id=principal.user_id or "unknown")
    if config is None:
        raise HTTPException(status_code=404, detail="尚未配置平台默认模型。")
    audit(db, "platform_llm.disabled", actor_id=principal.user_id, metadata={})
    db.commit()
    return platform_llm.masked_view(config)
