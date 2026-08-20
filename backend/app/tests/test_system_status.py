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


def test_web_page_injects_api_path_from_service_configuration(client, monkeypatch):
    monkeypatch.setattr(settings, "API_PREFIX", "/review-api")

    response = client.get("/")

    assert response.status_code == 200
    assert 'window.__PGS_CONFIG__ = {"apiBase": "/review-api"};' in response.text
    assert 'id="api-base"' not in response.text


def test_llm_check_reports_mock_without_real_call(client):
    response = client.get("/api/system/llm-check")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["stage"] == "mock"  # 默认 mock：不发真实请求
    assert payload["network"] == "offline"  # mock 不触网


def test_integration_status_reports_network_scope(client):
    # conftest 默认 mock → offline（零配置不触网）
    payload = client.get("/api/system/integrations").json()
    assert payload["llm"]["network"] == "offline"


def test_local_provider_status_is_local(client, monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "local")
    payload = client.get("/api/system/integrations").json()["llm"]
    assert payload["provider"] == "local"
    assert payload["network"] == "local"  # 连本地端口、无外部依赖
    assert payload["adapter"] == "OpenAICompatibleChatScorer"


def test_check_connectivity_local_unreachable_gives_hint(monkeypatch):
    from backend.app.services.llm import diagnostics

    class _Boom:
        provider = "openai_compatible"
        model_name = "local-model"

        def complete_json(self, *args):
            raise ConnectionError("Connection refused")

    monkeypatch.setattr(settings, "LLM_PROVIDER", "local")
    monkeypatch.setattr(diagnostics, "get_llm_scorer", lambda: _Boom())
    res = diagnostics.check_connectivity()
    assert res["ok"] is False and res["network"] == "local"
    assert "本地模型" in res["error"]  # 友好提示


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
