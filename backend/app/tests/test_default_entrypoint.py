"""默认入口切换与回退（前端 v2 计划 §8.3，阶段 6B）。

切换由**配置开关**控制，不是删掉旧页：出问题时要能改一个环境变量就退回去，
而不是重新发一版。旧入口在切换后保留在 `/legacy/`，同一份产物同时可达。

回退路径必须始终可用——这是「保留一个可回退发布窗口」的实现。
"""

from backend.app.core.config import settings


def test_default_entry_serves_the_legacy_spa_by_default(client):
    """并存期默认入口不变。切换是显式动作，不能因为部署了新页就自动生效。"""
    response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert "/assets/app.js" in body or "PGS_CLIENT_CONFIG" in body
    assert "/workbench/assets/" not in body


def test_switching_the_flag_serves_the_workbench_at_root(client, monkeypatch):
    monkeypatch.setattr(settings, "WORKBENCH_DEFAULT_ENTRY", True)

    response = client.get("/")

    assert response.status_code == 200
    assert "/workbench/assets/" in response.text


def test_legacy_entry_stays_reachable_after_the_switch(client, monkeypatch):
    """回退窗口：切换后旧页仍在 /legacy/，改一个开关即可退回。"""
    monkeypatch.setattr(settings, "WORKBENCH_DEFAULT_ENTRY", True)

    response = client.get("/legacy/")

    assert response.status_code == 200
    assert "/workbench/assets/" not in response.text


def test_legacy_entry_is_reachable_before_the_switch_too(client):
    """/legacy/ 始终可用，避免切换当天才第一次验证这条路径。"""
    response = client.get("/legacy/")

    assert response.status_code == 200


def test_workbench_path_is_unaffected_by_the_flag(client, monkeypatch):
    monkeypatch.setattr(settings, "WORKBENCH_DEFAULT_ENTRY", True)

    assert client.get("/workbench").status_code == 200
    monkeypatch.setattr(settings, "WORKBENCH_DEFAULT_ENTRY", False)
    assert client.get("/workbench").status_code == 200


def test_auth_pages_keep_serving_the_legacy_shell(client, monkeypatch):
    """邀请与重置链接是外发出去的，切换入口不能让它们失效。"""
    monkeypatch.setattr(settings, "WORKBENCH_DEFAULT_ENTRY", True)

    for path in ("/login", "/register", "/reset-password"):
        response = client.get(path)
        assert response.status_code == 200, path


def test_api_is_never_swallowed_regardless_of_the_flag(client, monkeypatch):
    for value in (True, False):
        monkeypatch.setattr(settings, "WORKBENCH_DEFAULT_ENTRY", value)
        response = client.get("/api/system/integrations")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
