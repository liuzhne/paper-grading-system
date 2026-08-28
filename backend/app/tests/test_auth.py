"""单租户简单登录（opt-in）：默认关闭不影响现有行为；开启后数据端点要求登录。"""

from backend.app.core.config import settings


def test_auth_off_by_default(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", None)
    assert client.get("/api/auth/status").json()["auth_required"] is False
    assert client.get("/api/batches").status_code == 200  # 免登录


def test_public_auth_routes_render_the_same_web_entrypoint(client):
    for path in ("/", "/login", "/register", "/reset-password"):
        response = client.get(path)
        assert response.status_code == 200
        assert "衡鉴" in response.text


def test_api_responses_expose_request_timings(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", None)
    response = client.get("/api/batches")
    timing = response.headers["server-timing"]
    assert "app;dur=" in timing


def test_auth_on_gates_data_routes(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "s3cret")
    monkeypatch.setattr(settings, "AUTH_USERNAME", "admin")
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", False)

    assert client.get("/api/auth/status").json()["auth_required"] is True
    assert client.get("/api/batches").status_code == 401  # 无 token
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401

    login = client.post("/api/auth/login", json={"username": "admin", "password": "s3cret"})
    assert login.status_code == 204
    ok = client.get("/api/batches")
    assert ok.status_code == 200
    # /system 与 /auth 始终开放（登录页/状态自检需在登录前可达）
    assert client.get("/api/system/integrations").status_code == 200


def test_tampered_token_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "s3cret")
    bad = client.get("/api/batches", headers={"Authorization": "Bearer admin|9999999999|deadbeef"})
    assert bad.status_code == 401
