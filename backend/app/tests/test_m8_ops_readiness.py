from __future__ import annotations

import json
import logging
from pathlib import Path
import subprocess
import tarfile

import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings


ROOT = Path(__file__).resolve().parents[3]


def _strong_settings(**overrides):
    values = {
        "AUTH_ENABLED": True,
        "AUTH_USERNAME": "ops-admin",
        "AUTH_PASSWORD": "Strong-login-password-2026",
        "AUTH_SECRET": "0123456789abcdef0123456789abcdef",
        "LLM_DEBUG_LOG_ENABLED": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_production_auth_rejects_weak_secrets_and_raw_llm_debug_logging():
    assert _strong_settings().AUTH_ENABLED is True
    assert Settings(_env_file=None, AUTH_ENABLED=False).AUTH_ENABLED is False

    weak_cases = (
        {"AUTH_PASSWORD": "short"},
        {"AUTH_PASSWORD": "ops-admin"},
        {"AUTH_PASSWORD": "replace-with-strong-login-password"},
        {"AUTH_SECRET": "change-me-in-prod"},
        {"AUTH_SECRET": "replace-with-random-32-plus-char-secret"},
        {"AUTH_SECRET": "x" * 31},
        {"AUTH_SECRET": "x" * 32},
        {"AUTH_PASSWORD": "a" * 12},
        {"LLM_DEBUG_LOG_ENABLED": True},
    )
    for overrides in weak_cases:
        with pytest.raises(ValidationError):
            _strong_settings(**overrides)


def test_llm_logger_runtime_guard_blocks_protected_raw_content_and_scrubs_keys(
    monkeypatch,
    caplog,
):
    from backend.app.core.config import settings
    from backend.app.services.llm import debug_logging

    caplog.set_level(logging.WARNING, logger="paper_grading.llm")
    monkeypatch.setattr(settings, "LLM_DEBUG_LOG_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    debug_logging.log_llm_exception(
        "provider", RuntimeError("private student paper"), 0, 1
    )
    assert "private student paper" not in "\n".join(caplog.messages)

    caplog.clear()
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-runtime-secret")
    debug_logging.log_llm_exception(
        "provider", RuntimeError("failed with sk-runtime-secret"), 0, 1
    )
    messages = "\n".join(caplog.messages)
    assert "sk-runtime-secret" not in messages
    assert "***REDACTED***" in messages


def test_ci_has_locked_unit_postgres_migration_and_restore_gates():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for marker in (
        "permissions:",
        "contents: read",
        "python-version: \"3.12\"",
        "uv sync --locked",
        "python -m pytest -q",
        "postgres:16",
        "0011_version_hash_on_update",
        "0017_batch_scoring_jobs",
        "0022_legacy_tenant_backfill",
        "0023_rule_scoring_review_tasks",
        "verify_postgres_ops",
        "ops_backup",
        "pgs_ops_artifacts",
        "docker-compose-smoke",
        "docker compose --env-file .env.intranet.example build --pull app",
        "docker compose --env-file .env.intranet.example up -d --wait",
        "python -m backend.app.scripts.smoke_deployment",
        "pg-dump-version.txt",
        "container-backup-verify.json",
        "pgs-docker-compose-smoke-evidence",
        "actions/upload-artifact@v7",
    ):
        assert marker in workflow
    assert "pull_request:" in workflow and "push:" in workflow
    assert "SCORING_ENGINE_MODE: legacy" in workflow
    assert "production_default_switch_authorized" not in workflow

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for marker in (
        "python:3.12-slim-bookworm",
        "POSTGRES_MAJOR=16",
        "https://apt.postgresql.org/pub/repos/apt",
        '"postgresql-client-${POSTGRES_MAJOR}"',
    ):
        assert marker in dockerfile


class _FakePostgresRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, args, *, env):
        self.calls.append((list(args), dict(env)))
        if args[0] == "pg_dump":
            option = next(value for value in args if value.startswith("--file="))
            Path(option.split("=", 1)[1]).write_bytes(b"postgres-custom-dump")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _make_storage(root):
    files = {
        "uploads/student.docx": b"private-paper",
        "parsed/snapshot.json": b'{"snapshot": true}',
        "llm_cache.sqlite": b"cache-audit-ledger",
        "reports/report.html": b"<html>report</html>",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return files


def test_backup_is_atomic_verifiable_complete_and_never_serializes_database_secret(
    tmp_path,
):
    from backend.app.services.deployment.backup import create_backup
    from backend.app.services.deployment.backup import verify_backup

    storage = tmp_path / "storage"
    expected = _make_storage(storage)
    with pytest.raises(ValueError, match="outside storage_root"):
        create_backup(
            database_url="postgresql+psycopg://paper:secret@db/source",
            storage_root=storage,
            destination_root=storage / "backups",
            migration_head="0023_rule_scoring_review_tasks",
            revision="unsafe",
            rto_minutes=120,
            rpo_minutes=1440,
            command_runner=_FakePostgresRunner(),
        )
    runner = _FakePostgresRunner()
    secret = "database-super-secret"
    package = create_backup(
        database_url=(
            "postgresql+psycopg://paper:%s@db.example:5432/paper_grading"
            % secret
        ),
        storage_root=storage,
        destination_root=tmp_path / "backups",
        migration_head="0023_rule_scoring_review_tasks",
        revision="abcdef123456",
        rto_minutes=120,
        rpo_minutes=1440,
        command_runner=runner,
    )
    assert package.is_dir()
    assert not any(path.name.endswith(".partial") for path in package.parent.iterdir())
    manifest = verify_backup(package)
    assert manifest["schema_version"] == "paper-grading-backup@1"
    assert manifest["migration_head"] == "0023_rule_scoring_review_tasks"
    assert manifest["revision"] == "abcdef123456"
    assert manifest["recovery_objectives"] == {
        "rto_minutes": 120,
        "rpo_minutes": 1440,
    }
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert secret not in serialized
    assert "PGPASSWORD" not in json.dumps(manifest)
    assert all(secret not in " ".join(args) for args, _env in runner.calls)
    assert any(env.get("PGPASSWORD") == secret for _args, env in runner.calls)

    with tarfile.open(package / "storage.tar.gz", "r:gz") as archive:
        archived = {name.lstrip("./") for name in archive.getnames()}
    assert set(expected).issubset(archived)

    (package / "database.dump").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        verify_backup(package)


def test_restore_verifies_confirmation_empty_target_and_hashes_before_commands(tmp_path):
    from backend.app.services.deployment.backup import create_backup
    from backend.app.services.deployment.backup import restore_backup

    storage = tmp_path / "storage"
    expected = _make_storage(storage)
    create_runner = _FakePostgresRunner()
    package = create_backup(
        database_url="postgresql+psycopg://paper:source-secret@db/source_db",
        storage_root=storage,
        destination_root=tmp_path / "backups",
        migration_head="0023_rule_scoring_review_tasks",
        revision="revision-1",
        rto_minutes=120,
        rpo_minutes=1440,
        command_runner=create_runner,
    )
    runner = _FakePostgresRunner()
    target_url = "postgresql+psycopg://paper:target-secret@db/recovery_db"

    with pytest.raises(ValueError, match="confirmation"):
        restore_backup(
            package,
            database_url=target_url,
            storage_target=tmp_path / "restored",
            confirm_database="wrong_db",
            command_runner=runner,
        )
    assert runner.calls == []

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        restore_backup(
            package,
            database_url=target_url,
            storage_target=nonempty,
            confirm_database="recovery_db",
            command_runner=runner,
        )
    assert runner.calls == []

    restored = tmp_path / "restored"
    result = restore_backup(
        package,
        database_url=target_url,
        storage_target=restored,
        confirm_database="recovery_db",
        command_runner=runner,
    )
    assert result["database"] == "recovery_db"
    assert {relative: (restored / relative).read_bytes() for relative in expected} == expected
    assert all(
        "source-secret" not in " ".join(args)
        and "target-secret" not in " ".join(args)
        for args, _env in runner.calls
    )
    assert runner.calls[-1][0][0] == "pg_restore"
    assert runner.calls[-1][1]["PGPASSWORD"] == "target-secret"


def test_postgres_verifier_freezes_complete_migration_and_stable_order_contract():
    """冻结这份清单，让任何改动都必须是有意的。

    它**不**保证清单跟得上真实迁移链：两边都停在 0023 时它照样通过，而这正是
    0024–0028 的漏登被放过去的原因（CI 里表现为 PostgreSQL 门禁报
    「unexpected alembic head」）。跟不跟得上由
    `test_migration_sequence_is_current.py` 对着 Alembic 本身验。
    """
    from backend.app.services.deployment.postgres_verifier import MIGRATION_SEQUENCE
    from backend.app.services.deployment.postgres_verifier import stable_ordering_clause

    assert MIGRATION_SEQUENCE == (
        "0011_version_hash_on_update",
        "0012_frozen_scoring_policy",
        "0013_core_replay_identity",
        "0014_release_gate_profiles",
        "0015_scoring_run_runtime_identity",
        "0016_general_submissions",
        "0017_batch_scoring_jobs",
        "0018_identity_organizations",
        "0019_resource_organization_scope",
        "0020_rubric_visibility_scope",
        "0021_private_ai_connections",
        "0022_legacy_tenant_backfill",
        "0023_rule_scoring_review_tasks",
        "0024_batch_status_machine",
        "0025_review_contract",
        "0026_review_command_receipts",
        "0027_export_events",
        "0028_export_event_backfill",
        "0029_runtime_access_for_v2_tables",
    )
    assert stable_ordering_clause() == ("created_at", "id")
    script = (ROOT / "backend/app/scripts/verify_postgres_ops.py").read_text(
        encoding="utf-8"
    )
    for marker in (
        "postgresql",
        "alembic_version",
        "ix_batch_scoring_jobs_one_active_per_batch",
        "ORDER BY created_at, id",
        "production_default_switch_authorized",
    ):
        assert marker in script

    runtime_identity_migration = (
        ROOT / "alembic/versions/0015_scoring_run_runtime_identity.py"
    ).read_text(encoding="utf-8")
    for marker in (
        '"alembic_version"',
        '"version_num"',
        "sa.String(length=128)",
        'dialect.name == "postgresql"',
    ):
        assert marker in runtime_identity_migration


def test_ops_readiness_endpoint_is_diagnostic_and_never_authorizes_core(client):
    response = client.get("/api/system/ops-readiness")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema_version"] == "ops-readiness@1"
    assert payload["production_default_switch_authorized"] is False
    assert {"disk", "database", "batch_jobs", "security"}.issubset(
        payload["signals"]
    )
    assert {
        "disk_free_gb_min",
        "database_size_gb_max",
        "batch_stale_minutes",
        "llm_failure_rate_max",
        "rto_minutes",
        "rpo_minutes",
    }.issubset(payload["thresholds"])
    serialized = response.text
    for secret in (
        "change-me-in-prod",
        "OPENAI_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "AUTH_PASSWORD",
        "AUTH_SECRET",
    ):
        assert secret not in serialized


def test_executable_launch_checklist_defines_monitoring_ownership_and_evidence():
    checklist = (ROOT / "docs/上线清单.md").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.intranet.example").read_text(encoding="utf-8")
    for marker in (
        "RTO",
        "RPO",
        "磁盘",
        "数据库",
        "LLM",
        "值班责任人",
        "回滚",
        "备份恢复演练记录",
        "Postgres CI",
        "GATE-03",
        "production_default_switch_authorized=false",
        "deploy-vercel-production",
        "VERCEL_TOKEN",
        "Vercel Git 直部署保持关闭",
    ):
        assert marker in checklist
    for marker in (
        "OPS_DISK_FREE_GB_MIN",
        "OPS_DATABASE_SIZE_GB_MAX",
        "OPS_BATCH_STALE_MINUTES",
        "OPS_LLM_FAILURE_RATE_MAX",
        "OPS_RTO_MINUTES",
        "OPS_RPO_MINUTES",
    ):
        assert marker in env_example
