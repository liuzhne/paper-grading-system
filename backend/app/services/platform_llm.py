"""平台默认模型的单例配置（D-028）。

平台模型此前完全由环境变量决定，没有配置表，也就没有「谁配的、什么时候配的、
配了什么」。改为管理员在页面上配置，**初始状态是未配置**——于是「不绑连接就评分」
这条路从一开始就不存在，而不是靠某个 env 恰好设对。

密钥沿用 BYOK 那套信封加密，但**用不同的 AAD 域**：否则一份 BYOK 密文可以被搬进
平台配置行并解出来，加密就只剩「存了密文」这一个作用，绑不住它属于谁。
"""

from __future__ import annotations

import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select

from backend.app.db.models import PlatformLLMConfig
from backend.app.db.models import new_id
from backend.app.db.models import utcnow
from backend.app.services.ai_connections import ConnectionRuntime
from backend.app.services.ai_connections import _decoded
from backend.app.services.ai_connections import _encoded
from backend.app.services.ai_connections import _master_key
from backend.app.services.ai_connections import key_masked
from backend.app.services.ai_connections import validate_base_url
from backend.app.services.ai_connections import validate_provider_options


#: 平台配置只允许这些 provider——与 BYOK 同一套 adapter。
SUPPORTED_PROVIDERS = ("openai_responses", "openai_compatible")


def _aad(*, config_id: str, key_version: int) -> bytes:
    """与 BYOK 不同的 AAD 域。

    `ai_connections` 用的是 `ai-connection|<org>|<owner>|<version>`。两者都以
    domain 前缀开头且各自绑住自己的身份，任一方的密文搬到另一方都解不开。
    """
    return ("platform-llm|%s|%d" % (config_id, key_version)).encode("utf-8")


def encrypt_api_key(api_key: str, *, config_id: str, key_version: int):
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(_master_key()).encrypt(
        nonce,
        api_key.encode("utf-8"),
        _aad(config_id=config_id, key_version=key_version),
    )
    return _encoded(encrypted[:-16]), _encoded(nonce), _encoded(encrypted[-16:])


def decrypt_api_key(config: PlatformLLMConfig) -> str:
    try:
        plaintext = AESGCM(_master_key()).decrypt(
            _decoded(config.api_key_nonce),
            _decoded(config.api_key_ciphertext) + _decoded(config.api_key_tag),
            _aad(config_id=config.id, key_version=config.key_version),
        )
        return plaintext.decode("utf-8")
    except (InvalidTag, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("platform LLM key cannot be decrypted") from exc


def count_configs(session) -> int:
    return len(session.scalars(select(PlatformLLMConfig)).all())


def _existing(session) -> PlatformLLMConfig | None:
    return session.scalars(select(PlatformLLMConfig)).first()


def get_active_config(session) -> PlatformLLMConfig | None:
    """当前生效的平台模型；未配置或已停用都返回 None。"""
    config = _existing(session)
    if config is None or config.status != "active":
        return None
    return config


def set_config(
    session,
    *,
    provider_type: str,
    base_url: str,
    model_name: str,
    api_key: str,
    configured_by: str,
    provider_options: dict | None = None,
) -> PlatformLLMConfig:
    """写入或替换平台模型配置。

    单例：再设置一次是**替换**，不是新增——两条「活跃平台模型」意味着谁也说不清
    用的是哪个。
    """
    if provider_type not in SUPPORTED_PROVIDERS:
        raise ValueError("unsupported platform LLM provider type")
    if not (api_key or "").strip():
        raise ValueError("platform LLM api key is required")
    if len(api_key.strip()) < 8:
        # last4 取不满时说明这不是一个真实的 key。
        raise ValueError("platform LLM api key is too short")

    checked_url = validate_base_url(base_url)
    options = validate_provider_options(provider_options)

    config = _existing(session)
    if config is None:
        config = PlatformLLMConfig(id=new_id())
        session.add(config)
    else:
        # 替换时 bump key_version：AAD 绑了它，旧密文自然失效，不会被误用。
        config.key_version = (config.key_version or 1) + 1

    key = api_key.strip()
    ciphertext, nonce, tag = encrypt_api_key(
        key, config_id=config.id, key_version=config.key_version or 1
    )
    config.provider_type = provider_type
    config.base_url = checked_url
    config.model_name = model_name
    config.provider_options = options
    config.api_key_ciphertext = ciphertext
    config.api_key_nonce = nonce
    config.api_key_tag = tag
    config.key_last4 = key[-4:]
    config.status = "active"
    config.configured_by = configured_by
    config.configured_at = utcnow()
    config.disabled_at = None
    config.disabled_by = None
    config.last_error_code = None
    session.flush()
    return config


def disable_config(session, *, actor_id: str) -> PlatformLLMConfig | None:
    """停用但保留配置——出问题时不必先删再重配。"""
    config = _existing(session)
    if config is None:
        return None
    config.status = "disabled"
    config.disabled_at = utcnow()
    config.disabled_by = actor_id
    session.flush()
    return config


def runtime_for(config: PlatformLLMConfig) -> ConnectionRuntime:
    """把平台配置投影成与 BYOK 同形的运行时。

    复用同一个 `ConnectionRuntime`，`get_llm_scorer` 那边就不必为平台模型再开一条
    分支。`connection_id` 用固定标识，便于在用量与快照里区分来源。
    """
    return ConnectionRuntime(
        connection_id="platform",
        organization_id="platform",
        provider_type=config.provider_type,
        base_url=config.base_url,
        model_name=config.model_name,
        provider_options=dict(config.provider_options or {}),
        api_key=decrypt_api_key(config),
        key_version=config.key_version,
    )


def masked_view(config: PlatformLLMConfig | None) -> dict:
    """给管理页的脱敏视图。**不含明文 key，也不含密文字段。**"""
    if config is None:
        return {"configured": False, "status": "unconfigured"}
    return {
        "configured": True,
        "status": config.status,
        "provider_type": config.provider_type,
        "base_url": config.base_url,
        "model_name": config.model_name,
        "provider_options": dict(config.provider_options or {}),
        "key_masked": key_masked(config.key_last4),
        "configured_by": config.configured_by,
        "configured_at": config.configured_at,
        "last_verified_at": config.last_verified_at,
        "last_error_code": config.last_error_code,
        "disabled_at": config.disabled_at,
        "disabled_by": config.disabled_by,
    }


__all__ = [
    "SUPPORTED_PROVIDERS",
    "count_configs",
    "decrypt_api_key",
    "disable_config",
    "encrypt_api_key",
    "get_active_config",
    "masked_view",
    "runtime_for",
    "set_config",
]
