from backend.app.core.config import settings


def test_integration_status_masks_secrets_and_reports_real_adapters(client, monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test-secret")
    monkeypatch.setattr(settings, "OPENAI_MODEL", "gpt-test")
    monkeypatch.setattr(settings, "SHEET_WRITER_PROVIDER", "google_sheets")
    monkeypatch.setattr(settings, "GOOGLE_SHEETS_WEBAPP_URL", "https://script.google.test/exec")
    monkeypatch.setattr(settings, "GOOGLE_SHEETS_WEBAPP_SECRET", "sheet-secret")

    response = client.get("/api/system/integrations")
    assert response.status_code == 200
    payload = response.json()

    assert payload["llm"]["active"] is True
    assert payload["llm"]["adapter"] == "OpenAIResponsesScorer"
    assert payload["llm"]["model"] == "gpt-test"
    assert payload["llm"]["api_key_configured"] is True
    assert "sk-test-secret" not in response.text

    assert payload["sheets"]["active"] is True
    assert payload["sheets"]["adapter"] == "GoogleAppsScriptSheetWriter"
    assert payload["sheets"]["secret_configured"] is True
    assert "sheet-secret" not in response.text

    assert payload["frontend"]["static_web_ready"] is True
    assert payload["frontend"]["primary"] == "static_web"


def test_llm_check_reports_mock_without_real_call(client):
    response = client.get("/api/system/llm-check")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["stage"] == "mock"  # 默认 mock：不发真实请求


def test_integration_status_reports_openai_compatible_without_leaking_key(client, monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai_compatible")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_API_KEY", "zhipu-secret")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_MODEL", "glm-4.7-flash")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_PROVIDER_NAME", "zhipu")

    response = client.get("/api/system/integrations")
    assert response.status_code == 200
    payload = response.json()

    assert payload["llm"]["active"] is True
    assert payload["llm"]["adapter"] == "OpenAICompatibleChatScorer"
    assert payload["llm"]["model"] == "glm-4.7-flash"
    assert payload["llm"]["compatible_provider"] == "zhipu"
    assert payload["llm"]["thinking_type"] == settings.OPENAI_COMPATIBLE_THINKING_TYPE
    assert payload["llm"]["response_format_json"] == settings.OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON
    assert payload["llm"]["api_key_configured"] is True
    assert "zhipu-secret" not in response.text
