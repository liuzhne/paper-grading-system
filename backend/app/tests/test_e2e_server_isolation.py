"""验收后端不得从 `.env.local` 取任何东西（2026-09-08 CI 暴露）。

鉴权模式的浏览器验收本地一直是绿的，CI 上却起不来：`AUTH_PASSWORD_WEAK,
AUTH_SECRET_WEAK`。原因是 `Settings` 的 `env_file` 含 `.env.local`——开发机上
那份文件带的是**生产口令与生产 AUTH_SECRET**，于是本地验收一直在拿生产密钥当
测试口令用，而 CI 没有这个文件才把缺口暴露出来。

与更早的「测试打到生产库」是同一类问题：`settings` 隐式加载 `.env.local`。
"""

import inspect

from e2e_server import __main__ as entry


def test_env_file_loading_is_disabled():
    """显式关掉 env 文件，而不是指望恰好没装。"""
    source = inspect.getsource(entry.main)

    assert "PGS_DISABLE_ENV_FILE" in source


def test_throwaway_auth_credentials_are_set_explicitly():
    """一次性口令写在这里，不从任何外部文件继承。"""
    source = inspect.getsource(entry.main)

    assert "AUTH_PASSWORD" in source
    assert "AUTH_SECRET" in source


def test_the_throwaway_credentials_actually_start_a_protected_deployment():
    """守卫要靠真的合格来满足，不是靠关掉它。

    构造真实 `Settings` 而不是复刻一个 stub：校验读的字段会变，stub 只会在下次
    改动时静默失配。
    """
    from backend.app.core.config import Settings

    settings = Settings(
        AUTH_ENABLED=True,
        AUTH_USERNAME=entry.E2E_USERNAME,
        AUTH_PASSWORD=entry.E2E_AUTH_PASSWORD,
        AUTH_SECRET=entry.E2E_AUTH_SECRET,
        LLM_DEBUG_LOG_ENABLED=False,
    )

    assert settings.AUTH_ENABLED is True


def test_settings_can_opt_out_of_env_files(monkeypatch):
    """配置层要提供这个开关，否则验收只能靠「希望机器上没有那个文件」。

    `model_config` 在类创建时定型，所以这里验的是它取值的那个函数——调用方
    （e2e 入口）在导入 backend 之前置位环境变量。
    """
    from backend.app.core.config import env_files_for_settings

    monkeypatch.delenv("PGS_DISABLE_ENV_FILE", raising=False)
    assert env_files_for_settings() == (".env", ".env.local")

    monkeypatch.setenv("PGS_DISABLE_ENV_FILE", "1")
    assert env_files_for_settings() == ()
