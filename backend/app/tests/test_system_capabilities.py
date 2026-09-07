"""`/api/system/capabilities` 的能力投影契约（前端 v2 计划 §2.1、§6）。

要点：前端只用它决定导航可见性；响应不得泄漏 Key/Secret/平台敏感配置。
"""

from backend.app.core.config import settings


def test_capabilities_projects_roles_and_limits(client):
    payload = client.get("/api/system/capabilities").json()

    assert payload["platform_role"]
    assert set(payload["abilities"]) == {
        "view_organization_ops",
        "view_platform_ops",
        "manage_members",
        "manage_own_ai_connections",
    }
    assert payload["upload"]["max_size_mb"] == settings.DIRECT_UPLOAD_MAX_SIZE_MB
    assert payload["upload"]["tus_threshold_mb"] == settings.DIRECT_UPLOAD_TUS_THRESHOLD_MB
    assert payload["upload"]["accepted_extensions"] == [".docx", ".pdf"]
    assert payload["export"]["offline_mode"] == settings.OFFLINE_MODE


def test_capabilities_never_leaks_secrets(client):
    body = client.get("/api/system/capabilities").text.lower()

    for forbidden in ("secret", "api_key", "apikey", "webapp_url", "password", "token"):
        assert forbidden not in body


def test_offline_deployment_disables_sheets(client, monkeypatch):
    monkeypatch.setattr(settings, "OFFLINE_MODE", True)
    monkeypatch.setattr(settings, "SHEET_WRITER_PROVIDER", "google_sheets")
    monkeypatch.setattr(settings, "GOOGLE_SHEETS_WEBAPP_URL", "https://example.invalid/x")

    payload = client.get("/api/system/capabilities").json()

    assert payload["export"]["offline_mode"] is True
    assert payload["export"]["sheets_available"] is False
