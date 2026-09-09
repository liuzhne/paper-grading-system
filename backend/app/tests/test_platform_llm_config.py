"""平台默认模型的持久化配置（D-028，v3 计划 §4.0）。

平台模型此前完全由环境变量决定，没有配置表，也就没有「谁配的、什么时候配的、
配了什么」。改为管理员在页面上配置的**单例**，初始状态是「未配置」。

不复用 `ai_connections`：它的 `organization_id`/`owner_id` 都是 NOT NULL 且有
`scope='private'` 约束，为一个单例记录削弱一张多租户表的完整性约束代价太大。
"""

import pytest

from backend.app.core.config import settings
from backend.app.services import platform_llm


def test_a_fresh_deployment_has_no_platform_model(client):
    """初始状态必须是「没有」。

    这条是 D-027/D-028 的地基：平台没有模型时，「不绑连接就评分」这条路
    从一开始就不存在，而不是靠某个 env 恰好设对。
    """
    with client.session_factory() as session:
        assert platform_llm.get_active_config(session) is None


def test_setting_a_config_stores_only_ciphertext(client):
    with client.session_factory() as session:
        config = platform_llm.set_config(
            session,
            provider_type="openai_compatible",
            base_url="https://api.example.com/v1",
            model_name="test-model",
            api_key="sk-platform-secret-value",
            configured_by="admin-1",
        )
        session.commit()

        # 明文不得出现在任何字段里。
        stored = "".join(
            str(getattr(config, name) or "")
            for name in ("api_key_ciphertext", "api_key_nonce", "api_key_tag", "key_last4")
        )
        assert "sk-platform-secret-value" not in stored
        assert config.key_last4 == "alue"
        assert platform_llm.decrypt_api_key(config) == "sk-platform-secret-value"


def test_the_config_is_a_singleton(client):
    """再设置一次是替换，不是新增一条。

    两条「活跃平台模型」意味着谁也说不清用的是哪个。
    """
    with client.session_factory() as session:
        platform_llm.set_config(
            session, provider_type="openai_compatible",
            base_url="https://a.example.com/v1", model_name="m1",
            api_key="sk-first-key", configured_by="admin-1",
        )
        platform_llm.set_config(
            session, provider_type="openai_compatible",
            base_url="https://b.example.com/v1", model_name="m2",
            api_key="sk-second-key", configured_by="admin-2",
        )
        session.commit()

        active = platform_llm.get_active_config(session)
        assert active.model_name == "m2"
        assert active.configured_by == "admin-2"
        assert platform_llm.count_configs(session) == 1


def test_disabling_keeps_the_config_but_stops_serving_it(client):
    """停用是保留配置、立刻停止使用——出问题时不必先删再重配。"""
    with client.session_factory() as session:
        platform_llm.set_config(
            session, provider_type="openai_compatible",
            base_url="https://a.example.com/v1", model_name="m1",
            api_key="sk-key-value", configured_by="admin-1",
        )
        session.commit()

        platform_llm.disable_config(session, actor_id="admin-1")
        session.commit()

        assert platform_llm.get_active_config(session) is None
        assert platform_llm.count_configs(session) == 1


def test_a_byok_ciphertext_cannot_be_decrypted_as_platform_config(client):
    """两种密钥用不同的 AAD 域绑定上下文。

    否则一份 BYOK 密文可以被搬进平台配置行并解出来——加密就只剩「存了密文」
    这一个作用，绑不住它属于谁。
    """
    from backend.app.services import ai_connections

    ciphertext, nonce, tag = ai_connections.encrypt_api_key(
        "sk-user-key", organization_id="org-1", owner_id="user-1", key_version=1
    )
    with client.session_factory() as session:
        config = platform_llm.set_config(
            session, provider_type="openai_compatible",
            base_url="https://a.example.com/v1", model_name="m1",
            api_key="sk-platform", configured_by="admin-1",
        )
        # 把 BYOK 的密文搬进平台配置行。
        config.api_key_ciphertext = ciphertext
        config.api_key_nonce = nonce
        config.api_key_tag = tag
        session.flush()

        with pytest.raises(ValueError):
            platform_llm.decrypt_api_key(config)


def test_masked_view_never_exposes_the_key(client):
    with client.session_factory() as session:
        platform_llm.set_config(
            session, provider_type="openai_compatible",
            base_url="https://a.example.com/v1", model_name="m1",
            api_key="sk-abcdefgh1234", configured_by="admin-1",
        )
        session.commit()

        view = platform_llm.masked_view(platform_llm.get_active_config(session))

        assert view["key_masked"].endswith("1234")
        assert "sk-abcdefgh1234" not in str(view)
        assert "api_key" not in view
