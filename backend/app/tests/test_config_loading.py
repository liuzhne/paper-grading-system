from backend.app.core.config import Settings


def test_local_env_overrides_base_file_but_not_process_environment(tmp_path, monkeypatch):
    """`.env.local` is a local override; deployment environment wins."""

    (tmp_path / ".env").write_text(
        "LLM_PROVIDER=mock\nOPENAI_MODEL=base-model\n", encoding="utf-8"
    )
    (tmp_path / ".env.local").write_text(
        "LLM_PROVIDER=openai_compatible\nOPENAI_MODEL=local-model\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    local_settings = Settings()
    assert local_settings.LLM_PROVIDER == "openai_compatible"
    assert local_settings.OPENAI_MODEL == "local-model"

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert Settings().LLM_PROVIDER == "openai"
