"""模型解析顺序：绑定的 BYOK → 平台配置 → 抛错（D-027、D-028）。

改这个顺序的理由不是洁癖。生产此前是 `LLM_PROVIDER=mock`，而 `engine.py` 在批次
没绑连接时直接调 `get_llm_scorer()`——于是**通过界面建的任务评出来的是 Mock 假分，
界面上没有任何提示**。同一条路径让 AI 起草产出 Mock 编的扣分规则，却以「AI 起草 ·
待确认」呈现，确认后进入正式发布的评分标准。

假结果比明确失败危险得多：它会被当成真结论沿用下去。
"""

import pytest

from backend.app.core.config import settings
from backend.app.services.llm.factory import get_llm_scorer
from backend.app.services.llm.mock import MockLLMScorer


def _authenticated(monkeypatch):
    """模拟受保护部署。"""
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "a-strong-password-1")


def test_local_development_still_uses_the_environment(monkeypatch):
    """`AUTH_ENABLED=false` 是显式开发模式，必须继续走环境变量。

    否则本地开发与全套浏览器验收会立刻断掉。
    """
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")

    assert isinstance(get_llm_scorer(), MockLLMScorer)


def test_authenticated_deployment_without_any_model_fails_loudly(client, monkeypatch):
    """受保护部署 + 平台未配置 + 没绑连接 = 明确失败，不是 Mock。

    这正是修复前那条路：provider 为 mock 时，`factory` 里那道「认证部署禁止平台
    模型」的保护排在 mock 分支**之后**，所以它不生效。
    """
    _authenticated(monkeypatch)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")

    with pytest.raises(RuntimeError) as excinfo:
        get_llm_scorer()

    message = str(excinfo.value)
    # 报错要能指导下一步，否则运维只会去改 LLM_PROVIDER 再试一次。
    assert "AI" in message or "连接" in message or "connection" in message.lower()


def test_authenticated_deployment_uses_the_platform_config_when_present(
    client, monkeypatch
):
    _authenticated(monkeypatch)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")

    from backend.app.services import platform_llm

    with client.session_factory() as session:
        platform_llm.set_config(
            session,
            provider_type="openai_compatible",
            base_url="https://api.example.com/v1",
            model_name="platform-model",
            api_key="sk-platform-key",
            configured_by="admin-1",
        )
        session.commit()

        # 传入调用方的会话——评分引擎与起草端点手里都有事务，这就是它们的调用方式。
        scorer = get_llm_scorer(session=session)

    assert not isinstance(scorer, MockLLMScorer)


def test_a_disabled_platform_config_is_not_served(client, monkeypatch):
    """停用之后要回到「没有模型」，而不是继续用旧配置。"""
    _authenticated(monkeypatch)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")

    from backend.app.services import platform_llm

    with client.session_factory() as session:
        platform_llm.set_config(
            session, provider_type="openai_compatible",
            base_url="https://api.example.com/v1", model_name="m",
            api_key="sk-platform-key-value", configured_by="admin-1",
        )
        session.commit()
        platform_llm.disable_config(session, actor_id="admin-1")
        session.commit()

        with pytest.raises(RuntimeError):
            get_llm_scorer(session=session)


def test_a_bound_connection_still_wins_over_everything(client, monkeypatch):
    """绑定的 BYOK runtime 优先，且**永不**回落。

    这条是既有行为（factory 的 docstring 写明 fail-closed），这里钉住它不被
    平台配置的引入破坏。
    """
    _authenticated(monkeypatch)
    from backend.app.services.ai_connections import ConnectionRuntime

    runtime = ConnectionRuntime(
        connection_id="c1",
        organization_id="org-1",
        provider_type="openai_compatible",
        base_url="https://mine.example.com/v1",
        model_name="my-model",
        provider_options={},
        api_key="sk-mine",
        key_version=1,
    )

    scorer = get_llm_scorer(runtime)

    assert not isinstance(scorer, MockLLMScorer)
    assert scorer._ai_connection_snapshot["ai_connection_id"] == "c1"
