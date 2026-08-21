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
        }.issubset(tables)
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
        # 0005 模板格式规格
        assert "format_spec" in {col["name"] for col in inspector.get_columns("rubrics")}
        # 0008 owner_id 预留（单租户起步，为多用户铺路）
        for table in ("rubrics", "grading_batches", "papers", "scoring_runs"):
            assert "owner_id" in {col["name"] for col in inspector.get_columns(table)}
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
                    "('job-m8', 'missing-batch', 1, 0, 1, 'queued', 0, 0, 0, 0, "
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
