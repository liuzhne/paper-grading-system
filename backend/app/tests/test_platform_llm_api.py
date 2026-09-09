"""平台默认模型的管理端点（D-028，限平台管理员）。

配置平台模型等于决定「所有没绑 BYOK 的用户用哪个模型、花谁的钱」。这条能力必须
限平台管理员，且读接口永远不能回显密钥。
"""

import pytest

from backend.app.core.config import settings

from backend.app.tests.test_ops_permissions import _login_org_admin
from backend.app.tests.test_ops_permissions import _login_platform_admin


ENDPOINT = "/api/system/platform-llm"


def _payload(**overrides):
    body = {
        "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "model_name": "platform-model",
        "api_key": "sk-platform-secret-1234",
    }
    body.update(overrides)
    return body


def test_unconfigured_deployment_reports_it_plainly(client, monkeypatch):
    _login_platform_admin(client, monkeypatch)

    response = client.get(ENDPOINT)

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["status"] == "unconfigured"


def test_org_admin_cannot_read_the_platform_model(client, monkeypatch):
    """组织管理员不是平台管理员。

    这条配置跨组织生效，读它等于知道平台在用哪个供应商、哪个模型。
    """
    _login_org_admin(client, monkeypatch, "platform-llm-org-admin")

    assert client.get(ENDPOINT).status_code == 403


def test_org_admin_cannot_configure_the_platform_model(client, monkeypatch):
    _login_org_admin(client, monkeypatch, "platform-llm-writer")

    assert client.post(ENDPOINT, json=_payload()).status_code == 403
    assert client.post("%s/disable" % ENDPOINT).status_code == 403


def test_configuring_stores_the_key_and_never_returns_it(client, monkeypatch):
    _login_platform_admin(client, monkeypatch)

    response = client.post(ENDPOINT, json=_payload())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is True
    assert body["status"] == "active"
    assert body["model_name"] == "platform-model"
    assert body["key_masked"].endswith("1234")
    # 明文与密文都不能出现在响应里。
    assert "sk-platform-secret-1234" not in response.text
    assert "ciphertext" not in response.text
    assert "api_key" not in body


def test_reconfiguring_replaces_instead_of_adding(client, monkeypatch):
    _login_platform_admin(client, monkeypatch)
    client.post(ENDPOINT, json=_payload())

    client.post(ENDPOINT, json=_payload(model_name="second-model", api_key="sk-second-key-9999"))

    body = client.get(ENDPOINT).json()
    assert body["model_name"] == "second-model"
    assert body["key_masked"].endswith("9999")

    from backend.app.services import platform_llm

    with client.session_factory() as session:
        assert platform_llm.count_configs(session) == 1


def test_disabling_stops_serving_but_keeps_the_record(client, monkeypatch):
    _login_platform_admin(client, monkeypatch)
    client.post(ENDPOINT, json=_payload())

    disabled = client.post("%s/disable" % ENDPOINT)

    assert disabled.status_code == 200, disabled.text
    body = client.get(ENDPOINT).json()
    # 保留配置：出问题时不必先删再重配。
    assert body["configured"] is True
    assert body["status"] == "disabled"
    assert body["disabled_by"]


def test_who_configured_it_is_recorded(client, monkeypatch):
    """「谁把平台模型换了」必须可追溯——这正是从环境变量搬过来的理由之一。"""
    _login_platform_admin(client, monkeypatch)

    client.post(ENDPOINT, json=_payload())

    body = client.get(ENDPOINT).json()
    assert body["configured_by"]
    assert body["configured_at"]


def test_an_unsupported_provider_is_rejected(client, monkeypatch):
    _login_platform_admin(client, monkeypatch)

    response = client.post(ENDPOINT, json=_payload(provider_type="anthropic_messages"))

    assert response.status_code == 422


def test_a_private_base_url_is_rejected(client, monkeypatch):
    """与 BYOK 同一条 SSRF 边界，不能因为是平台配置就放行。"""
    _login_platform_admin(client, monkeypatch)

    response = client.post(ENDPOINT, json=_payload(base_url="http://169.254.169.254/latest"))

    assert response.status_code == 422


# --- 能力表：前端据此决定是否强制引导去配置 BYOK ---------------------------


def test_capabilities_report_no_model_on_a_fresh_deployment(client, monkeypatch):
    """全新部署：平台没配、用户没绑，`can_use_llm` 必须是 false。

    前端据此把用户引导到「账户与连接」。没有这个字段，前端只能等某次调用炸了
    才知道用不了——那时用户已经上传完材料了。
    """
    _login_platform_admin(client, monkeypatch)

    body = client.get("/api/system/capabilities").json()

    assert body["llm"]["platform_model_available"] is False
    assert body["llm"]["has_own_connection"] is False
    assert body["llm"]["can_use_llm"] is False


def test_configuring_the_platform_model_unblocks_everyone(client, monkeypatch):
    """平台配好后，没绑 BYOK 的用户也能正常使用（用户决定，2026-09-09）。"""
    _login_platform_admin(client, monkeypatch)
    client.post(ENDPOINT, json=_payload())

    body = client.get("/api/system/capabilities").json()

    assert body["llm"]["platform_model_available"] is True
    assert body["llm"]["can_use_llm"] is True


def test_disabling_the_platform_model_blocks_again(client, monkeypatch):
    _login_platform_admin(client, monkeypatch)
    client.post(ENDPOINT, json=_payload())
    client.post("%s/disable" % ENDPOINT)

    body = client.get("/api/system/capabilities").json()

    assert body["llm"]["platform_model_available"] is False
    assert body["llm"]["can_use_llm"] is False


def test_a_users_own_connection_unblocks_them_without_a_platform_model(
    client, monkeypatch
):
    """自带 BYOK 的用户不该被平台没配模型挡住。"""
    _login_platform_admin(client, monkeypatch)
    created = client.post(
        "/api/ai-connections",
        json={
            "name": "mine",
            "provider_type": "openai_responses",
            "base_url": "https://api.openai.com/v1",
            "model_name": "gpt-4.1-mini",
            "provider_options": {},
            "api_key": "sk-own-connection-4321",
        },
    )
    assert created.status_code == 201, created.text

    body = client.get("/api/system/capabilities").json()

    assert body["llm"]["platform_model_available"] is False
    assert body["llm"]["has_own_connection"] is True
    assert body["llm"]["can_use_llm"] is True


def test_a_disabled_connection_does_not_count(client, monkeypatch):
    """停用的连接不算「有可用连接」——否则用户会被放行到一个必然失败的流程。"""
    _login_platform_admin(client, monkeypatch)
    created = client.post(
        "/api/ai-connections",
        json={
            "name": "disabled one",
            "provider_type": "openai_responses",
            "base_url": "https://api.openai.com/v1",
            "model_name": "gpt-4.1-mini",
            "provider_options": {},
            "api_key": "sk-disabled-conn-8888",
        },
    )
    assert client.post("/api/ai-connections/%s/disable" % created.json()["id"]).status_code == 200

    body = client.get("/api/system/capabilities").json()

    assert body["llm"]["has_own_connection"] is False
    assert body["llm"]["can_use_llm"] is False


def test_development_mode_reports_the_environment_model_as_available(
    client, monkeypatch
):
    """`AUTH_ENABLED=false` 时模型来自环境变量，能力表必须承认这一点。

    否则本地开发与非鉴权验收会被前端的模型守卫全数拦下——而那些环境本来就能
    正常调 Mock。解析顺序里开发模式走 env 是既定行为（D-028），能力表漏算它
    就与实际能力对不上。
    """
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")

    body = client.get("/api/system/capabilities").json()

    assert body["llm"]["can_use_llm"] is True


def test_capabilities_survive_a_missing_platform_table(client, monkeypatch):
    """迁移还没跑到时，能力表不能 500。

    含迁移的发布里总有一个窗口：代码已上、迁移未到（或反过来）。此时查
    `platform_llm_config` 会抛 ProgrammingError。`/system/capabilities` 是前端
    启动就要读的——它 500，整个工作台起不来，**故障面远大于「平台模型读不到」**。

    读不到就当作「没有平台模型」：这与真实的未配置状态一致，且不回落 Mock。
    """
    from backend.app.services import platform_llm

    def _boom(_session):
        raise RuntimeError("relation \"platform_llm_config\" does not exist")

    _login_platform_admin(client, monkeypatch)
    monkeypatch.setattr(platform_llm, "get_active_config", _boom)

    response = client.get("/api/system/capabilities")

    assert response.status_code == 200, response.text
    assert response.json()["llm"]["platform_model_available"] is False
