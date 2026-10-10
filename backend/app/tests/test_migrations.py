from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import JSON
from sqlalchemy import MetaData
from sqlalchemy import Table
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import select
from sqlalchemy import text

from backend.app.core.config import settings

ROOT = Path(__file__).resolve().parents[3]


def _p1_01_revision(config):
    """只定位 0008 的直接后继，避免把未来 0010+ 混入 P1-01 测试。"""
    revisions = ScriptDirectory.from_config(config).walk_revisions()
    direct_children = []
    for revision in revisions:
        down_revisions = revision.down_revision
        if isinstance(down_revisions, str):
            down_revisions = (down_revisions,)
        if down_revisions and "0008_owner_id" in down_revisions:
            direct_children.append(revision.revision)
    assert len(direct_children) == 1, "P1-01 必须且只能新增一个 0008 的直接后继迁移"
    assert direct_children[0].startswith("0009"), "P1-01 迁移必须从 0009 开始"
    return direct_children[0]


def test_alembic_migrations_apply_to_head(monkeypatch, tmp_path):
    """pytest 平时用 create_all 建表，不走 Alembic；这里独立验证 0001-0008 迁移链。"""
    url = "sqlite+pysqlite:///%s" % (tmp_path / "migrations.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)

    # 不传 alembic.ini：使 config_file_name=None，env.py 跳过 fileConfig，
    # 避免 disable_existing_loggers 把 app 日志器禁掉而污染其它用例。
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        # 0001 基础表 + 0004 校准锚点表
        assert {
            "users",
            "rubrics",
            "rubric_criteria",
            "scoring_runs",
            "score_items",
            "calibration_anchors",
            "release_gate_profiles",
            "release_gate_runs",
            "release_gate_approvals",
            "evaluation_batches",
            "submissions",
            "document_snapshots",
            "batch_scoring_jobs",
            "batch_scoring_items",
            "organizations",
            "organization_members",
            "organization_invitations",
            "auth_sessions",
            "email_verification_tokens",
            "password_reset_tokens",
            "audit_logs",
            "rule_scoring_tasks",
            "manual_review_tasks",
        }.issubset(tables)
        user_cols = {col["name"] for col in inspector.get_columns("users")}
        assert {"email", "email_verified_at", "platform_role"}.issubset(user_cols)
        # 0002 原子项语义
        criterion_cols = {col["name"] for col in inspector.get_columns("rubric_criteria")}
        assert {"criterion_type", "scoring_mode", "applies_to", "rubric_levels", "sub_checks"}.issubset(criterion_cols)
        # 0007 维度 + 结构化扣分规则
        assert {"dimension", "deduction_rules_structured"}.issubset(criterion_cols)
        # 0002 结构化扣分 + 0003 篇章一致性 + 0006 格式问题 + token 计量
        score_item_cols = {col["name"] for col in inspector.get_columns("score_items")}
        assert {"deduction_items", "band_selection", "sub_results"}.issubset(score_item_cols)
        run_cols = {col["name"] for col in inspector.get_columns("scoring_runs")}
        assert {
            "prompt_tokens",
            "total_tokens",
            "coherence_findings",
            "format_findings",
            "business_profile_version",
            "prompt_version",
            "runtime_identity",
            "submission_id",
            "document_snapshot_id",
        }.issubset(run_cols)
        assert next(
            column for column in inspector.get_columns("scoring_runs")
            if column["name"] == "paper_id"
        )["nullable"] is True
        job_cols = {
            col["name"] for col in inspector.get_columns("batch_scoring_jobs")
        }
        assert {
            "observation_policy",
            "observation_policy_hash",
            "metrics_snapshot",
            "runner_token",
            "heartbeat_at",
        }.issubset(job_cols)
        item_cols = {
            col["name"] for col in inspector.get_columns("batch_scoring_items")
        }
        assert {
            "attempt_count",
            "scoring_run_id",
            "baseline_scoring_run_id",
            "telemetry",
            "attempt_history",
        }.issubset(item_cols)
        rule_task_cols = {
            col["name"] for col in inspector.get_columns("rule_scoring_tasks")
        }
        assert {
            "organization_id",
            "scoring_run_id",
            "rule_code",
            "status",
            "provider_error",
            "result_snapshot",
        }.issubset(rule_task_cols)
        manual_task_cols = {
            col["name"] for col in inspector.get_columns("manual_review_tasks")
        }
        assert {
            "organization_id",
            "scoring_run_id",
            "rule_scoring_task_id",
            "status",
            "resolution_evidence",
            "version",
        }.issubset(manual_task_cols)
        # 0005 模板格式规格
        assert "format_spec" in {col["name"] for col in inspector.get_columns("rubrics")}
        # 0008 owner_id 预留（单租户起步，为多用户铺路）
        for table in ("rubrics", "grading_batches", "papers", "scoring_runs"):
            assert "owner_id" in {col["name"] for col in inspector.get_columns(table)}
    finally:
        engine.dispose()


def test_0023_review_task_migration_downgrades_empty_and_replays(
    monkeypatch,
    tmp_path,
):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "review-tasks-empty.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))

    command.upgrade(config, "head")
    command.downgrade(config, "0022_legacy_tenant_backfill")
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {"rule_scoring_tasks", "manual_review_tasks"}.isdisjoint(tables)
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        assert {"rule_scoring_tasks", "manual_review_tasks"}.issubset(
            inspect(engine).get_table_names()
        )
    finally:
        engine.dispose()


def test_0023_review_task_migration_refuses_lossy_downgrade(
    monkeypatch,
    tmp_path,
):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "review-tasks-guard.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")

    engine = create_engine(url)
    now = datetime(2026, 9, 4)
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys = OFF"))
            connection.execute(
                text(
                    "INSERT INTO rule_scoring_tasks "
                    "(id, organization_id, scoring_run_id, criterion_code, rule_code, "
                    "judge_type, dependency_rule_codes, status, blocking_final_total, "
                    "attempt_count, max_attempts, created_at, updated_at) VALUES "
                    "('task-1', 'org-1', 'run-1', 'criterion-1', 'rule-1', "
                    "'llm', '[]', 'failed_exhausted', 1, 1, 3, :now, :now)"
                ),
                {"now": now},
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="would lose workflow audit history"):
        command.downgrade(config, "0022_legacy_tenant_backfill")

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0023_rule_scoring_review_tasks"
    finally:
        engine.dispose()


def test_0017_batch_scoring_job_migration_downgrades_empty_and_replays(
    monkeypatch,
    tmp_path,
):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "m8-jobs-empty-replay.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")
    command.downgrade(config, "0016_general_submissions")

    engine = create_engine(url)
    try:
        assert {
            "batch_scoring_jobs",
            "batch_scoring_items",
        }.isdisjoint(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        assert {
            "batch_scoring_jobs",
            "batch_scoring_items",
        }.issubset(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_0017_batch_scoring_job_migration_refuses_lossy_downgrade(
    monkeypatch,
    tmp_path,
):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "m8-jobs-downgrade-guard.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")
    engine = create_engine(url)
    now = datetime(2026, 8, 2)
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys = OFF"))
            connection.execute(
                text(
                    "INSERT INTO batch_scoring_jobs "
                    "(id, grading_batch_id, generation, rescore, max_workers, status, "
                    "total_items, pending_count, running_count, succeeded_count, "
                    "skipped_count, failed_count, canceled_count, observation_policy, "
                    "observation_policy_hash, created_at, updated_at) VALUES "
                    # 已结束的任务：0035 只拒绝活动任务，这里验证的是 0017 自己的守卫。
                    "('job-m8', 'missing-batch', 1, 0, 1, 'completed', 0, 0, 0, 0, "
                    "0, 0, 0, '{}', :digest, :now, :now)"
                ),
                {"digest": "a" * 64, "now": now},
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="would lose durable batch scoring state"):
        command.downgrade(config, "0016_general_submissions")


def test_0016_general_submission_migration_downgrades_empty_and_replays(
    monkeypatch,
    tmp_path,
):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "m6-empty-replay.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")
    command.downgrade(config, "0015_scoring_run_runtime_identity")

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        assert {
            "evaluation_batches",
            "submissions",
            "document_snapshots",
        }.isdisjoint(inspector.get_table_names())
        columns = {
            column["name"]: column
            for column in inspector.get_columns("scoring_runs")
        }
        assert "submission_id" not in columns
        assert "document_snapshot_id" not in columns
        assert columns["paper_id"]["nullable"] is False
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        assert {
            "evaluation_batches",
            "submissions",
            "document_snapshots",
        }.issubset(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_0016_upgrade_preserves_legacy_paper_run_without_synthetic_submission(
    monkeypatch,
    tmp_path,
):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "m6-legacy-history.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "0015_scoring_run_runtime_identity")

    engine = create_engine(url)
    now = datetime(2026, 8, 2)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO rubrics "
                    "(id, name, version, total_score, status, created_at) "
                    "VALUES ('legacy-rubric', 'Legacy', '1', 100, 'published', :now)"
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO grading_batches "
                    "(id, name, rubric_id, status, created_at, updated_at) "
                    "VALUES ('legacy-batch', 'Legacy batch', 'legacy-rubric', "
                    "'completed', :now, :now)"
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO papers "
                    "(id, batch_id, file_name, file_path, status, created_at, updated_at) "
                    "VALUES ('legacy-paper', 'legacy-batch', 'legacy.docx', "
                    "'/tmp/legacy.docx', 'scored', :now, :now)"
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO scoring_runs "
                    "(id, paper_id, rubric_id, model_provider, model_name, status, "
                    "need_manual_review, created_at) VALUES "
                    "('legacy-run', 'legacy-paper', 'legacy-rubric', 'mock', "
                    "'legacy', 'scored', 0, :now)"
                ),
                {"now": now},
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT paper_id, submission_id, document_snapshot_id "
                    "FROM scoring_runs WHERE id = 'legacy-run'"
                )
            ).one()
            assert tuple(row) == ("legacy-paper", None, None)
            assert connection.execute(
                text("SELECT count(*) FROM submissions")
            ).scalar_one() == 0
            assert connection.execute(
                text("SELECT count(*) FROM document_snapshots")
            ).scalar_one() == 0
    finally:
        engine.dispose()


def test_0022_backfills_legacy_resources_into_the_default_organization(
    monkeypatch,
    tmp_path,
):
    """PGS-49: upgrading an existing 0017 database preserves and scopes history."""
    url = "sqlite+pysqlite:///%s" % (tmp_path / "pgs49-legacy-backfill.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "0017_batch_scoring_jobs")

    engine = create_engine(url)
    now = datetime(2026, 8, 22)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, username, display_name, role, created_at, updated_at) "
                    "VALUES (:id, 'dev-user', 'Legacy developer', 'developer', :now, :now)"
                ),
                {"id": settings.DEFAULT_DEV_USER_ID, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO rubrics (id, name, version, total_score, status, owner_id, created_at) "
                    "VALUES ('legacy-rubric', 'Legacy', '1', 100, 'published', :owner, :now)"
                ),
                {"owner": settings.DEFAULT_DEV_USER_ID, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO grading_batches (id, name, rubric_id, status, owner_id, created_at, updated_at) "
                    "VALUES ('legacy-batch', 'Legacy batch', 'legacy-rubric', 'completed', :owner, :now, :now)"
                ),
                {"owner": settings.DEFAULT_DEV_USER_ID, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO papers (id, batch_id, file_name, file_path, status, owner_id, created_at, updated_at) "
                    "VALUES ('legacy-paper', 'legacy-batch', 'legacy.docx', '/tmp/legacy.docx', 'scored', :owner, :now, :now)"
                ),
                {"owner": settings.DEFAULT_DEV_USER_ID, "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO scoring_runs "
                    "(id, paper_id, rubric_id, model_provider, model_name, status, need_manual_review, owner_id, created_at) "
                    "VALUES ('legacy-run', 'legacy-paper', 'legacy-rubric', 'openai', 'legacy-model', 'scored', 0, :owner, :now)"
                ),
                {"owner": settings.DEFAULT_DEV_USER_ID, "now": now},
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            organization_id = connection.execute(
                text("SELECT id FROM organizations WHERE id = '00000000-0000-0000-0000-000000000002'")
            ).scalar_one()
            assert connection.execute(
                text("SELECT platform_role, email_verified_at IS NOT NULL FROM users WHERE id = :id"),
                {"id": settings.DEFAULT_DEV_USER_ID},
            ).one() == ("platform_admin", 1)
            assert connection.execute(
                text("SELECT role FROM organization_members WHERE organization_id = :organization_id AND user_id = :user_id"),
                {"organization_id": organization_id, "user_id": settings.DEFAULT_DEV_USER_ID},
            ).scalar_one() == "org_admin"
            for table in ("rubrics", "grading_batches", "papers", "scoring_runs", "evaluation_batches", "submissions"):
                assert connection.execute(
                    text("SELECT count(*) FROM %s WHERE organization_id IS NULL" % table)
                ).scalar_one() == 0
            assert connection.execute(
                text("SELECT model_provider, model_name FROM scoring_runs WHERE id = 'legacy-run'")
            ).one() == ("openai", "legacy-model")
            assert connection.execute(
                text("SELECT ai_connection_id FROM scoring_runs WHERE id = 'legacy-run'")
            ).scalar_one() is None
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="would lose tenant ownership"):
        command.downgrade(config, "0021_private_ai_connections")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0022_legacy_tenant_backfill"
    finally:
        engine.dispose()


@pytest.mark.parametrize("stage", ("batch", "submission", "snapshot"))
def test_0016_general_submission_migration_refuses_any_lossy_downgrade(
    monkeypatch,
    tmp_path,
    stage,
):
    url = "sqlite+pysqlite:///%s" % (tmp_path / ("m6-guard-%s.db" % stage))
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")

    engine = create_engine(url)
    now = datetime(2026, 8, 2)
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys = OFF"))
            connection.execute(
                text(
                    "INSERT INTO evaluation_batches "
                    "(id, name, rubric_id, rubric_version_id, business_profile_key, "
                    "business_profile_version, status, created_at, updated_at) VALUES "
                    "('batch-m6', 'B', 'rubric-missing', 'version-missing', "
                    "'technical_proposal', 'technical-proposal@1', 'active', :now, :now)"
                ),
                {"now": now},
            )
            if stage in {"submission", "snapshot"}:
                connection.execute(
                    text(
                        "INSERT INTO submissions "
                        "(id, evaluation_batch_id, source_artifact_hash, source_artifact_ref, "
                        "file_name, media_type, byte_length, metadata, status, created_at, updated_at) "
                        "VALUES ('submission-m6', 'batch-m6', :hash, 'blob:test', "
                        "'x.docx', 'application/test', 1, '{}', 'uploaded', :now, :now)"
                    ),
                    {"hash": "a" * 64, "now": now},
                )
            if stage == "snapshot":
                connection.execute(
                    text(
                        "INSERT INTO document_snapshots "
                        "(id, submission_id, schema_version, business_profile_key, "
                        "business_profile_version, parser_version, normalizer_version, "
                        "content_hash, snapshot_hash, snapshot_ref, snapshot_payload, created_at) "
                        "VALUES ('snapshot-m6', 'submission-m6', 'document-snapshot@1', "
                        "'technical_proposal', 'technical-proposal@1', 'parser@1', "
                        "'normalizer@1', :content_hash, :snapshot_hash, 'snapshot:test', '{}', :now)"
                    ),
                    {
                        "content_hash": "b" * 64,
                        "snapshot_hash": "c" * 64,
                        "now": now,
                    },
                )
            connection.execute(text("PRAGMA foreign_keys = ON"))
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="would lose general submission data"):
        command.downgrade(config, "0015_scoring_run_runtime_identity")


def test_0015_runtime_identity_migration_downgrades_without_data_loss_and_replays(
    monkeypatch,
    tmp_path,
):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "runtime-identity-replay.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))

    command.upgrade(config, "head")
    command.downgrade(config, "0014_release_gate_profiles")

    engine = create_engine(url)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("scoring_runs")}
        assert {
            "business_profile_version",
            "prompt_version",
            "runtime_identity",
        }.isdisjoint(columns)
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("scoring_runs")}
        assert {
            "business_profile_version",
            "prompt_version",
            "runtime_identity",
        }.issubset(columns)
    finally:
        engine.dispose()


def test_0015_runtime_identity_migration_refuses_lossy_downgrade(
    monkeypatch,
    tmp_path,
):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "runtime-identity-guard.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")
    # This test deliberately creates an invalid 0015 row to exercise the 0015
    # loss guard itself. Remove later schemas first so SQLite does not rebuild
    # that intentionally invalid row while crossing the unrelated 0016 edge.
    command.downgrade(config, "0015_scoring_run_runtime_identity")

    engine = create_engine(url)
    now = datetime(2026, 8, 2)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO rubrics "
                    "(id, name, version, total_score, status, created_at) "
                    "VALUES ('rubric-1', 'R', 'v1', 100, 'draft', :now)"
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO grading_batches "
                    "(id, name, rubric_id, status, created_at, updated_at) "
                    "VALUES ('batch-1', 'B', 'rubric-1', 'draft', :now, :now)"
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO papers "
                    "(id, batch_id, file_name, file_path, status, created_at, updated_at) "
                    "VALUES ('paper-1', 'batch-1', 'p.docx', '/tmp/p.docx', "
                    "'parsed', :now, :now)"
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO scoring_runs "
                    "(id, paper_id, rubric_id, model_provider, model_name, status, "
                    "need_manual_review, created_at) VALUES "
                    "('run-1', 'paper-1', 'rubric-1', 'legacy', 'legacy', "
                    "'completed', 0, :now)"
                ),
                {"now": now},
            )
            connection.execute(text("PRAGMA ignore_check_constraints = ON"))
            connection.execute(
                text(
                    "UPDATE scoring_runs SET business_profile_version = 'thesis@1', "
                    "prompt_version = 'prompt@1', runtime_identity = '{}' "
                    "WHERE id = 'run-1'"
                )
            )
            connection.execute(text("PRAGMA ignore_check_constraints = OFF"))
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="would lose frozen runtime identity"):
        command.downgrade(config, "0014_release_gate_profiles")


def test_0009_provenance_migration_applies_from_0008(monkeypatch, tmp_path):
    """P1-01 的来源文件/原始规则 -> 编译运行 -> 可选版本可从空库迁移得到。"""
    url = "sqlite+pysqlite:///%s" % (tmp_path / "provenance-migration.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    p1_revision = _p1_01_revision(config)
    command.upgrade(config, p1_revision)

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        provenance_tables = {"rubric_compilations", "source_artifacts", "source_rules", "rubric_versions"}
        assert provenance_tables.issubset(tables)
        expected_columns = {
            "rubric_compilations": {
                "rubric_id",
                "status",
                "parser_version",
                "compiler_version",
                "model_provider",
                "model_name",
                "sampling_params",
                "prompt_version",
                "raw_parse_output",
                "raw_model_output",
                "validation_result",
                "blockers",
                "warnings",
                "human_changes",
                "created_by",
                "reviewed_by",
                "reviewed_at",
                "published_at",
                "final_version_hash",
            },
            "source_artifacts": {
                "compilation_id",
                "artifact_type",
                "file_name",
                "file_hash",
                "file_size_bytes",
                "uploaded_by",
            },
            "source_rules": {
                "source_artifact_id",
                "source_rule_code",
                "sheet_name",
                "row_number",
                "cell_locator",
                "raw_text",
            },
            "rubric_versions": {
                "rubric_id",
                "compilation_id",
                "version",
                "workflow_profile",
                "global_policy",
                "version_hash",
                "created_by",
            },
        }
        for table, expected in expected_columns.items():
            assert expected.issubset({column["name"] for column in inspector.get_columns(table)})

        required_non_nullable = {
            "rubric_compilations": {
                "rubric_id",
                "status",
                "parser_version",
                "compiler_version",
                "sampling_params",
                "prompt_version",
                "raw_parse_output",
                "raw_model_output",
                "validation_result",
                "blockers",
                "warnings",
                "human_changes",
                "created_by",
            },
            "source_artifacts": {
                "compilation_id",
                "artifact_type",
                "file_name",
                "file_hash",
                "file_size_bytes",
                "uploaded_by",
            },
            "source_rules": {
                "source_artifact_id",
                "source_rule_code",
                "sheet_name",
                "row_number",
                "cell_locator",
                "raw_text",
            },
            "rubric_versions": {
                "rubric_id",
                "compilation_id",
                "version",
                "workflow_profile",
                "global_policy",
                "version_hash",
                "created_by",
            },
        }
        for table, required in required_non_nullable.items():
            columns = {column["name"]: column for column in inspector.get_columns(table)}
            assert all(columns[column]["nullable"] is False for column in required)

        json_columns = {
            "rubric_compilations": {
                "sampling_params",
                "raw_parse_output",
                "raw_model_output",
                "validation_result",
                "blockers",
                "warnings",
                "human_changes",
            },
            "rubric_versions": {"global_policy"},
        }
        for table, expected_json_columns in json_columns.items():
            columns = {column["name"]: column for column in inspector.get_columns(table)}
            assert all(isinstance(columns[column]["type"], JSON) for column in expected_json_columns)

        expected_foreign_keys = {
            ("rubric_compilations", "rubric_id"): ("rubrics", "id"),
            ("rubric_compilations", "created_by"): ("users", "id"),
            ("rubric_compilations", "reviewed_by"): ("users", "id"),
            ("source_artifacts", "compilation_id"): ("rubric_compilations", "id"),
            ("source_artifacts", "uploaded_by"): ("users", "id"),
            ("source_rules", "source_artifact_id"): ("source_artifacts", "id"),
            ("rubric_versions", "created_by"): ("users", "id"),
        }
        for (table, local_column), (target_table, target_column) in expected_foreign_keys.items():
            matching = [
                foreign_key
                for foreign_key in inspector.get_foreign_keys(table)
                if foreign_key["constrained_columns"] == [local_column]
            ]
            assert len(matching) == 1
            assert matching[0]["referred_table"] == target_table
            assert matching[0]["referred_columns"] == [target_column]

        version_foreign_keys = inspector.get_foreign_keys("rubric_versions")
        assert any(
            foreign_key["referred_table"] == "rubric_compilations"
            and set(zip(foreign_key["constrained_columns"], foreign_key["referred_columns"]))
            == {("compilation_id", "id"), ("rubric_id", "rubric_id")}
            for foreign_key in version_foreign_keys
        )

        unique_constraints = inspector.get_unique_constraints("rubric_versions")
        unique_indexes = [index for index in inspector.get_indexes("rubric_versions") if index.get("unique")]
        assert ["compilation_id"] in [constraint["column_names"] for constraint in unique_constraints] or [
            "compilation_id"
        ] in [index["column_names"] for index in unique_indexes]
    finally:
        engine.dispose()


def test_0009_provenance_migration_downgrades_to_0008_and_replays(monkeypatch, tmp_path):
    """0009 回退只移除 P1-01 新表；0008 旧数据与 owner_id 必须保留，且可再次升级。"""
    url = "sqlite+pysqlite:///%s" % (tmp_path / "migration-replay.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    p1_revision = _p1_01_revision(config)
    command.upgrade(config, "0008_owner_id")

    engine = create_engine(url)
    legacy_rubric_id = "00000000-0000-0000-0000-000000000008"
    legacy_user_id = "00000000-0000-0000-0000-000000000001"
    now = datetime(2026, 7, 15, 10, 0, 0)
    try:
        metadata = MetaData()
        users = Table("users", metadata, autoload_with=engine)
        rubrics = Table("rubrics", metadata, autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(
                users.insert().values(
                    id=legacy_user_id,
                    username="migration-owner",
                    display_name="迁移测试用户",
                    role="developer",
                    department=None,
                    password_hash=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                rubrics.insert().values(
                    id=legacy_rubric_id,
                    owner_id=legacy_user_id,
                    name="0008 既有评分标准",
                    version="1.0",
                    total_score=100,
                    status="draft",
                    description=None,
                    format_spec={},
                    created_by=legacy_user_id,
                    created_at=now,
                    published_at=None,
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, p1_revision)
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {"rubric_compilations", "source_artifacts", "source_rules", "rubric_versions"}.issubset(tables)
        rubrics = Table("rubrics", MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            assert connection.scalar(select(rubrics.c.name).where(rubrics.c.id == legacy_rubric_id)) == "0008 既有评分标准"
    finally:
        engine.dispose()

    command.downgrade(config, "0008_owner_id")
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {"rubric_compilations", "source_artifacts", "source_rules", "rubric_versions"}.isdisjoint(tables)
        assert {"users", "rubrics", "rubric_criteria", "scoring_runs"}.issubset(tables)
        assert "owner_id" in {column["name"] for column in inspector.get_columns("rubrics")}
        rubrics = Table("rubrics", MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            assert connection.scalar(select(rubrics.c.owner_id).where(rubrics.c.id == legacy_rubric_id)) == legacy_user_id
    finally:
        engine.dispose()

    command.upgrade(config, p1_revision)
    engine = create_engine(url)
    try:
        assert {"rubric_compilations", "source_artifacts", "source_rules", "rubric_versions"}.issubset(
            set(inspect(engine).get_table_names())
        )
    finally:
        engine.dispose()


def test_0030_creates_the_platform_llm_config_table(monkeypatch, tmp_path):
    """平台模型配置表随迁移建出来（D-028）。"""
    url = "sqlite+pysqlite:///%s" % (tmp_path / "platform.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")

    engine = create_engine(url)
    try:
        assert "platform_llm_config" in set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_0030_refuses_to_downgrade_with_a_configured_platform_model(
    monkeypatch, tmp_path
):
    """有配置时拒绝降级。

    这一行里有密钥材料和「谁配的」记录；删掉之后即便重建表，管理员也得重新找回
    API key 再录一次——降级把一次运维动作变成一次事故。空表照常允许回滚。
    """
    url = "sqlite+pysqlite:///%s" % (tmp_path / "platform-downgrade.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "0030_platform_llm_config")

    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO platform_llm_config (id, provider_type, base_url,"
                    " model_name, provider_options, api_key_ciphertext,"
                    " api_key_nonce, api_key_tag, key_version, key_last4, status,"
                    " configured_by, configured_at, created_at, updated_at) VALUES"
                    " ('c1','openai_compatible','https://a/v1','m','{}','x','y','z',"
                    " 1,'abcd','active','admin',CURRENT_TIMESTAMP,"
                    " CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                )
            )
    finally:
        engine.dispose()

    with pytest.raises(Exception) as excinfo:
        command.downgrade(config, "0029_runtime_access_for_v2_tables")

    assert "platform LLM configuration" in str(excinfo.value)


def test_0030_downgrades_cleanly_when_nothing_is_configured(monkeypatch, tmp_path):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "platform-empty.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "0030_platform_llm_config")

    command.downgrade(config, "0029_runtime_access_for_v2_tables")

    engine = create_engine(url)
    try:
        assert "platform_llm_config" not in set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_0032_normalizes_old_active_connections_and_preserves_status_on_downgrade(monkeypatch, tmp_path):
    from sqlalchemy.orm import Session
    from sqlalchemy.exc import IntegrityError
    from backend.app.db import models
    url = "sqlite+pysqlite:///%s" % (tmp_path / "single-active.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "0031_rubric_import_sessions")
    engine = create_engine(url)
    with Session(engine) as db:
        user = models.User(username="migration-fixture", display_name="Fixture")
        db.add(user)
        db.flush()
        org = models.Organization(name="migration fixture", created_by=user.id)
        db.add(org)
        db.flush()
        for name, verified in (("old", None), ("verified", datetime(2026, 9, 20)), ("disabled", None)):
            db.add(models.AIConnection(
                id=name, owner_id=user.id, organization_id=org.id, name=name,
                provider_type="openai_compatible", base_url="https://example.test/v1", model_name="fixture",
                api_key_ciphertext="fixture", api_key_nonce="fixture", api_key_tag="fixture", key_last4="test",
                status="disabled" if name == "disabled" else "active", last_verified_at=verified,
            ))
        db.commit()
    command.upgrade(config, "head")
    with engine.connect() as db:
        assert dict(db.execute(text("SELECT id, status FROM ai_connections")).all()) == {
            "old": "disabled", "verified": "active", "disabled": "disabled",
        }
        assert "uq_ai_connections_one_active" in {i["name"] for i in inspect(engine).get_indexes("ai_connections")}
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE ai_connections SET status='active' WHERE id='old'"))
        db.rollback()
    command.downgrade(config, "0031_rubric_import_sessions")
    with engine.connect() as db:
        assert db.execute(text("SELECT status FROM ai_connections WHERE id='old'")).scalar_one() == "disabled"
    engine.dispose()


def test_0033_adds_the_decision_ledger_and_guards_the_reuse_audit_flag(monkeypatch, tmp_path):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "decision-ledger.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    assert "rule_decision_ledger" in inspector.get_table_names()
    task_columns = {column["name"] for column in inspector.get_columns("rule_scoring_tasks")}
    assert {"decision_reused", "group_call_id"} <= task_columns
    assert "uq_rule_decision_ledger_scope_identity" in {
        item["name"] for item in inspector.get_unique_constraints("rule_decision_ledger")
    }

    # An empty reuse flag and a cache-only ledger downgrade cleanly ...
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO rule_decision_ledger (id, scope_key, decision_identity_hash, "
                "rule_code, response, prompt_tokens, completion_tokens, created_at, expires_at) "
                "VALUES ('l1', 'no-organization|x', :hash, 'R', '{}', 0, 0, :now, :now)"
            ),
            {"hash": "e" * 64, "now": datetime(2026, 10, 5)},
        )
    command.downgrade(config, "0032_single_active_ai_connection")
    assert "rule_decision_ledger" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")

    # ... but the audit fact "this decision was replayed" cannot be rebuilt.
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(
            text(
                "INSERT INTO rule_scoring_tasks (id, organization_id, scoring_run_id, "
                "criterion_code, rule_code, judge_type, dependency_rule_codes, status, "
                "blocking_final_total, decision_reused, attempt_count, max_attempts, "
                "created_at, updated_at) VALUES ('t1', 'org', 'run', 'C', 'R', 'semantic', "
                "'[]', 'succeeded', 0, 1, 1, 3, :now, :now)"
            ),
            {"now": datetime(2026, 10, 5)},
        )
    with pytest.raises(RuntimeError, match="reuse audit flag"):
        command.downgrade(config, "0032_single_active_ai_connection")

    # A group-call link alone is the same kind of unrebuildable audit fact.
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE rule_scoring_tasks SET decision_reused = 0, group_call_id = :id"),
            {"id": "f" * 64},
        )
    with pytest.raises(RuntimeError, match="reuse audit flag"):
        command.downgrade(config, "0032_single_active_ai_connection")
    engine.dispose()


def _seed_connection(engine, *, connection_id, provider_type, status="active"):
    from sqlalchemy.orm import Session
    from backend.app.db import models

    with Session(engine) as db:
        user = db.scalar(select(models.User).where(models.User.username == "claude-fixture"))
        if user is None:
            user = models.User(username="claude-fixture", display_name="Fixture")
            db.add(user)
            db.flush()
            db.add(models.Organization(id="org-claude", name="claude fixture", created_by=user.id))
            db.flush()
        db.add(models.AIConnection(
            id=connection_id, owner_id=user.id, organization_id="org-claude", name=connection_id,
            provider_type=provider_type, base_url="https://example.test/v1", model_name="fixture",
            api_key_ciphertext="fixture", api_key_nonce="fixture", api_key_tag="fixture",
            key_last4="test", status=status,
        ))
        db.flush()
        # A row that references the connection must survive the SQLite table rebuild.
        db.add(models.AIUsageLedger(
            organization_id="org-claude", owner_id=user.id, ai_connection_id=connection_id,
            provider_type=provider_type, model_name="fixture",
        ))
        db.commit()


def test_0034_allows_claude_connections_and_keeps_the_other_constraints(monkeypatch, tmp_path):
    from sqlalchemy.exc import IntegrityError

    url = "sqlite+pysqlite:///%s" % (tmp_path / "claude-protocol.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "0033_rule_decision_ledger")
    engine = create_engine(url)
    try:
        _seed_connection(engine, connection_id="chat", provider_type="openai_compatible")
        command.upgrade(config, "0034_anthropic_messages_provider")
        _seed_connection(engine, connection_id="claude", provider_type="anthropic_messages", status="disabled")

        with engine.connect() as db:
            table_sql = db.execute(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_connections'")
            ).scalar_one()
            index_sql = db.execute(
                text("SELECT sql FROM sqlite_master WHERE name='uq_ai_connections_one_active'")
            ).scalar_one()
            assert db.execute(text("SELECT count(*) FROM ai_usage_ledger")).scalar_one() == 2
        assert "ck_ai_connections_private_scope" in table_sql
        assert "ck_ai_connections_status" in table_sql
        assert "anthropic_messages" in table_sql
        assert "WHERE" in index_sql.upper()
        with engine.begin() as db, pytest.raises(IntegrityError):
            db.execute(text("UPDATE ai_connections SET provider_type='gemini' WHERE id='chat'"))
    finally:
        engine.dispose()


@pytest.mark.parametrize("owner", ["connection", "deleted-connection", "platform"])
def test_0034_refuses_to_downgrade_while_claude_is_in_use(monkeypatch, tmp_path, owner):
    url = "sqlite+pysqlite:///%s" % (tmp_path / ("claude-downgrade-%s.db" % owner))
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "0034_anthropic_messages_provider")
    engine = create_engine(url)
    try:
        if owner == "platform":
            with engine.begin() as db:
                db.execute(text(
                    "INSERT INTO platform_llm_config (id, provider_type, base_url, model_name,"
                    " provider_options, api_key_ciphertext, api_key_nonce, api_key_tag, key_version,"
                    " key_last4, status, configured_by, configured_at, created_at, updated_at) VALUES"
                    " ('p1','anthropic_messages','https://api.anthropic.com/v1','m','{}','x','y','z',"
                    " 1,'abcd','disabled','admin',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                ))
        else:
            _seed_connection(
                engine, connection_id="claude", provider_type="anthropic_messages",
                status="deleted" if owner == "deleted-connection" else "active",
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="anthropic_messages"):
        command.downgrade(config, "0033_rule_decision_ledger")


def test_0034_downgrades_and_replays_without_claude_rows(monkeypatch, tmp_path):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "claude-empty.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "0034_anthropic_messages_provider")
    engine = create_engine(url)
    try:
        _seed_connection(engine, connection_id="chat", provider_type="openai_compatible")
        command.downgrade(config, "0033_rule_decision_ledger")
        with engine.connect() as db:
            table_sql = db.execute(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_connections'")
            ).scalar_one()
            assert db.execute(text("SELECT provider_type FROM ai_connections")).scalar_one() == "openai_compatible"
        assert "anthropic_messages" not in table_sql
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def _seed_0034_batch_jobs(url):
    """At 0034: one bound batch with an active job, one unbound batch with a finished job."""
    engine = create_engine(url)
    base = datetime(2026, 10, 1, 8, 0, 0)
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys = OFF"))
            for batch_id, connection_id, owner in (
                ("bound-batch", "conn-1", "batch-owner"),
                ("unbound-batch", None, "batch-owner"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO grading_batches (id, name, rubric_id, status, owner_id, "
                        "ai_connection_id, state_version, created_at, updated_at) VALUES "
                        "(:id, :id, 'rubric', 'scoring', :owner, :connection, 1, :now, :now)"
                    ),
                    {"id": batch_id, "connection": connection_id, "owner": owner, "now": base},
                )
            for job_id, batch_id, status, created_by in (
                ("active-job", "bound-batch", "running", "starter"),
                ("done-job", "unbound-batch", "completed", None),
            ):
                connection.execute(
                    text(
                        "INSERT INTO batch_scoring_jobs "
                        "(id, grading_batch_id, generation, rescore, max_workers, status, "
                        "total_items, pending_count, running_count, succeeded_count, "
                        "skipped_count, failed_count, canceled_count, observation_policy, "
                        "observation_policy_hash, created_by, created_at, updated_at) VALUES "
                        "(:id, :batch, 1, 0, 1, :status, 0, 0, 0, 0, 0, 0, 0, '{}', :digest, "
                        ":created_by, :now, :now)"
                    ),
                    {
                        "id": job_id,
                        "batch": batch_id,
                        "status": status,
                        "created_by": created_by,
                        "digest": "b" * 64,
                        "now": base,
                    },
                )
            for item_id, job_id, status, minute in (
                ("item-c", "active-job", "pending", 3),
                ("item-a", "active-job", "running", 1),
                ("item-b", "active-job", "pending", 2),
                ("item-z", "done-job", "succeeded", 1),
            ):
                created = base.replace(minute=minute)
                connection.execute(
                    text(
                        "INSERT INTO batch_scoring_items (id, job_id, paper_id, status, "
                        "attempt_count, attempt_history, started_at, created_at, updated_at) "
                        "VALUES (:id, :job, :paper, :status, 0, '[]', :started, :created, :created)"
                    ),
                    {
                        "id": item_id,
                        "job": job_id,
                        "paper": "paper-" + item_id,
                        "status": status,
                        "started": created if status == "running" else None,
                        "created": created,
                    },
                )
    finally:
        engine.dispose()


def test_0035_backfills_the_work_queue_columns_and_partial_indexes(monkeypatch, tmp_path):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "0035-backfill.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "0034_anthropic_messages_provider")
    _seed_0034_batch_jobs(url)
    command.upgrade(config, "0035_unified_work_queue")

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            rows = {
                row.id: row
                for row in connection.execute(
                    text(
                        "SELECT id, source_key, owner_id, ordinal, heartbeat_at, stall_count, "
                        "not_before FROM batch_scoring_items"
                    )
                )
            }
        assert {key: row.source_key for key, row in rows.items()} == {
            "item-a": "connection:conn-1",
            "item-b": "connection:conn-1",
            "item-c": "connection:conn-1",
            "item-z": "platform",
        }
        # 发起人优先，没有记录发起人时退回批次归属人。
        assert rows["item-a"].owner_id == "starter"
        assert rows["item-z"].owner_id == "batch-owner"
        # 任务内序号按创建时间排列，每个任务从 0 开始。
        assert [rows[key].ordinal for key in ("item-a", "item-b", "item-c")] == [0, 1, 2]
        assert rows["item-z"].ordinal == 0
        # 在跑的条目从此按心跳租约判断，回填为开始时间，过期后由巡检接手。
        assert rows["item-a"].heartbeat_at is not None
        assert rows["item-b"].heartbeat_at is None
        assert all(row.stall_count == 0 and row.not_before is None for row in rows.values())

        inspector = inspect(engine)
        indexes = {value["name"]: value for value in inspector.get_indexes("batch_scoring_items")}
        assert indexes["ix_batch_scoring_items_claim"]["column_names"] == [
            "source_key",
            "ordinal",
            "created_at",
        ]
        assert indexes["ix_batch_scoring_items_running"]["column_names"] == [
            "source_key",
            "heartbeat_at",
        ]
        assert "last_swept_at" in {
            column["name"] for column in inspector.get_columns("batch_scoring_jobs")
        }
        assert "work_runtime_state" in inspector.get_table_names()
        with engine.connect() as connection:
            plan = " ".join(
                str(row[-1])
                for row in connection.execute(
                    text(
                        "EXPLAIN QUERY PLAN SELECT id FROM batch_scoring_items "
                        "WHERE source_key = 'platform' AND status = 'pending' "
                        "ORDER BY ordinal, created_at LIMIT 1"
                    )
                )
            )
        assert "ix_batch_scoring_items_claim" in plan
    finally:
        engine.dispose()


def test_0035_refuses_to_downgrade_while_a_job_is_active(monkeypatch, tmp_path):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "0035-guard.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "0034_anthropic_messages_provider")
    _seed_0034_batch_jobs(url)
    command.upgrade(config, "0035_unified_work_queue")

    with pytest.raises(RuntimeError, match="0035 downgrade refused: 1 batch scoring job"):
        command.downgrade(config, "0034_anthropic_messages_provider")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0035_unified_work_queue"
    finally:
        engine.dispose()


def test_0035_downgrades_and_replays_with_only_finished_jobs(monkeypatch, tmp_path):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "0035-replay.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "0034_anthropic_messages_provider")
    _seed_0034_batch_jobs(url)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE batch_scoring_jobs SET status = 'canceled' WHERE id = 'active-job'")
            )
    finally:
        engine.dispose()
    command.upgrade(config, "0035_unified_work_queue")
    command.downgrade(config, "0034_anthropic_messages_provider")

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("batch_scoring_items")}
        assert columns.isdisjoint({"source_key", "ordinal", "heartbeat_at", "stall_count"})
        assert "work_runtime_state" not in inspector.get_table_names()
        with engine.connect() as connection:
            # 降级只删运行时列，不删条目本身。
            assert connection.execute(
                text("SELECT count(*) FROM batch_scoring_items")
            ).scalar_one() == 4
    finally:
        engine.dispose()
    command.upgrade(config, "head")
