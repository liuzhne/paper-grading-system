from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_postgres_web_start_exports_scoring_runtime_before_disabling_env_files():
    script = (ROOT / "scripts" / "start-web-pg.sh").read_text(encoding="utf-8")

    source_index = script.index('. "./$ENV_FILE"')
    disable_index = script.index("export PGS_DISABLE_ENV_FILE=1")
    worker_index = script.index("backend.app.scripts.run_batch_worker")
    assert source_index < disable_index < worker_index
    for name in (
        "SCORING_CONTEXT_WINDOW_TOKENS",
        "SCORING_CONTEXT_SAFETY_MARGIN_TOKENS",
        "SCORING_EVIDENCE_TOP_K",
        "OPENAI_COMPATIBLE_MAX_TOKENS",
        "OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON",
    ):
        assert name in script

    # The private .env.intranet is gitignored and absent in CI; the committed
    # example is what operators copy, so it must satisfy the script's checks.
    env = (ROOT / ".env.intranet.example").read_text(encoding="utf-8")
    assert "OPENAI_COMPATIBLE_MAX_TOKENS=2400" in env
    assert "OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON=true" in env
