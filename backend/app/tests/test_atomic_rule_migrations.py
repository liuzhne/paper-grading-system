"""P1-02 的 0010 独立迁移合同测试。"""

import json
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import JSON
from sqlalchemy import MetaData
from sqlalchemy import Numeric
from sqlalchemy import Table
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import select
from sqlalchemy import text

from backend.app.core.config import settings


ROOT = Path(__file__).resolve().parents[3]
P1_02_TABLES = {
    "template_items",
    "atomic_rules",
    "atomic_rule_source_rules",
    "rule_levels",
    "rule_template_links",
}


def _direct_child(config, parent_revision, expected_prefix, task_name):
    revisions = ScriptDirectory.from_config(config).walk_revisions()
    direct_children = []
    for revision in revisions:
        down_revisions = revision.down_revision
        if isinstance(down_revisions, str):
            down_revisions = (down_revisions,)
        if down_revisions and parent_revision in down_revisions:
            direct_children.append(revision.revision)
    assert len(direct_children) == 1, (
        f"{task_name} 必须且只能新增一个 {parent_revision} 的直接后继迁移"
    )
    assert direct_children[0].startswith(expected_prefix), f"{task_name} 迁移必须从 {expected_prefix} 开始"
    return direct_children[0]


def _p1_revisions(config):
    p1_01_revision = _direct_child(config, "0008_owner_id", "0009", "P1-01")
    p1_02_revision = _direct_child(config, p1_01_revision, "0010", "P1-02")
    return p1_01_revision, p1_02_revision


def _assert_single_foreign_key(inspector, table, local_column, target_table):
    matching = [
        foreign_key
        for foreign_key in inspector.get_foreign_keys(table)
        if foreign_key["constrained_columns"] == [local_column]
    ]
    assert len(matching) == 1
    assert matching[0]["referred_table"] == target_table
    assert matching[0]["referred_columns"] == ["id"]


def _unique_column_sets(inspector, table):
    constraints = {
        frozenset(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(table)
        if constraint.get("column_names")
    }
    indexes = {
        frozenset(index["column_names"])
        for index in inspector.get_indexes(table)
        if index.get("unique") and index.get("column_names")
    }
    return constraints | indexes


def _assert_p1_01_data_survives(engine, source_rule_id, version_id):
    metadata = MetaData()
    source_rules = Table("source_rules", metadata, autoload_with=engine)
    versions = Table("rubric_versions", metadata, autoload_with=engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(select(source_rules.c.raw_text).where(source_rules.c.id == source_rule_id))
            == "0010 升降级不得改写这条 0009 原始规则。"
        )
        assert connection.scalar(select(versions.c.version_hash).where(versions.c.id == version_id)) == "9" * 64


def test_0010_atomic_rules_migration_applies_from_0009(monkeypatch, tmp_path):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "atomic-rules-migration.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    _, p1_02_revision = _p1_revisions(config)
    command.upgrade(config, p1_02_revision)

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        assert P1_02_TABLES.issubset(set(inspector.get_table_names()))
        expected_columns = {
            "template_items": {
                "id",
                "source_artifact_id",
                "item_code",
                "kind",
                "section_path",
                "raw_text",
                "normalized_constraint",
                "strictness",
                "source_locator",
                "source_hash",
                "parse_confidence",
                "created_at",
            },
            "atomic_rules": {
                "id",
                "rubric_version_id",
                "criterion_id",
                "rule_code",
                "name",
                "rule_text",
                "direction",
                "effect_type",
                "max_points",
                "repeat_policy",
                "cap_points",
                "judge_type",
                "checker_key",
                "checker_params",
                "evidence_policy",
                "positive_example",
                "negative_example",
                "boundary_example",
                "strictness",
                "applies_to",
                "mutex_group",
                "depends_on_rule_codes",
                "status",
                "creation_method",
                "reviewed_by",
                "reviewed_at",
                "created_at",
            },
            "atomic_rule_source_rules": {"atomic_rule_id", "source_rule_id"},
            "rule_levels": {
                "id",
                "atomic_rule_id",
                "level_code",
                "points",
                "descriptor",
                "positive_example",
                "negative_example",
                "display_order",
                "created_at",
            },
            "rule_template_links": {
                "id",
                "rule_id",
                "template_item_id",
                "relationship_type",
                "match_method",
                "match_confidence",
                "rationale",
                "review_status",
                "reviewed_by",
                "reviewed_at",
                "created_at",
            },
        }
        for table, required in expected_columns.items():
            assert required.issubset({column["name"] for column in inspector.get_columns(table)})

        required_non_nullable = {
            "template_items": {
                "source_artifact_id",
                "item_code",
                "kind",
                "section_path",
                "raw_text",
                "strictness",
                "source_locator",
                "source_hash",
                "parse_confidence",
                "created_at",
            },
            "atomic_rules": {
                "rubric_version_id",
                "criterion_id",
                "rule_code",
                "name",
                "rule_text",
                "direction",
                "effect_type",
                "judge_type",
                "checker_params",
                "evidence_policy",
                "strictness",
                "applies_to",
                "depends_on_rule_codes",
                "status",
                "creation_method",
                "created_at",
            },
            "atomic_rule_source_rules": {"atomic_rule_id", "source_rule_id"},
            "rule_levels": {
                "atomic_rule_id",
                "level_code",
                "points",
                "descriptor",
                "display_order",
                "created_at",
            },
            "rule_template_links": {
                "rule_id",
                "template_item_id",
                "relationship_type",
                "match_method",
                "rationale",
                "review_status",
                "created_at",
            },
        }
        for table, required in required_non_nullable.items():
            columns = {column["name"]: column for column in inspector.get_columns(table)}
            assert all(columns[column]["nullable"] is False for column in required)
        template_columns = {column["name"]: column for column in inspector.get_columns("template_items")}
        assert template_columns["normalized_constraint"]["nullable"] is True
        expected_nullable = {
            "atomic_rules": {
                "max_points",
                "repeat_policy",
                "cap_points",
                "checker_key",
                "positive_example",
                "negative_example",
                "boundary_example",
                "mutex_group",
                "reviewed_by",
                "reviewed_at",
            },
            "rule_levels": {"positive_example", "negative_example"},
            "rule_template_links": {"match_confidence", "reviewed_by", "reviewed_at"},
        }
        for table, nullable_columns in expected_nullable.items():
            columns = {column["name"]: column for column in inspector.get_columns(table)}
            assert all(columns[column]["nullable"] is True for column in nullable_columns)

        json_columns = {
            "template_items": {"section_path", "normalized_constraint", "source_locator"},
            "atomic_rules": {"depends_on_rule_codes", "checker_params", "evidence_policy"},
        }
        for table, expected_json_columns in json_columns.items():
            columns = {column["name"]: column for column in inspector.get_columns(table)}
            assert all(isinstance(columns[column]["type"], JSON) for column in expected_json_columns)
        expected_json_defaults = {
            "template_items": {"section_path": [], "source_locator": {}},
            "atomic_rules": {
                "depends_on_rule_codes": [],
                "checker_params": {},
                "evidence_policy": {},
            },
        }
        with engine.connect() as connection:
            for table, defaulted_columns in expected_json_defaults.items():
                columns = {column["name"]: column for column in inspector.get_columns(table)}
                for column, expected_default in defaulted_columns.items():
                    default_sql = columns[column]["default"]
                    assert default_sql is not None
                    raw_default = connection.scalar(text(f"SELECT {default_sql}"))
                    parsed_default = json.loads(raw_default) if isinstance(raw_default, str) else raw_default
                    assert parsed_default == expected_default
        numeric_columns = {
            "template_items": {"parse_confidence"},
            "atomic_rules": {"max_points", "cap_points"},
            "rule_levels": {"points"},
            "rule_template_links": {"match_confidence"},
        }
        for table, expected_numeric_columns in numeric_columns.items():
            columns = {column["name"]: column for column in inspector.get_columns(table)}
            assert all(isinstance(columns[column]["type"], Numeric) for column in expected_numeric_columns)

        expected_foreign_keys = {
            ("template_items", "source_artifact_id"): "source_artifacts",
            ("atomic_rules", "rubric_version_id"): "rubric_versions",
            ("atomic_rules", "criterion_id"): "rubric_criteria",
            ("atomic_rules", "reviewed_by"): "users",
            ("atomic_rule_source_rules", "atomic_rule_id"): "atomic_rules",
            ("atomic_rule_source_rules", "source_rule_id"): "source_rules",
            ("rule_levels", "atomic_rule_id"): "atomic_rules",
            ("rule_template_links", "rule_id"): "atomic_rules",
            ("rule_template_links", "template_item_id"): "template_items",
            ("rule_template_links", "reviewed_by"): "users",
        }
        for (table, local_column), target_table in expected_foreign_keys.items():
            _assert_single_foreign_key(inspector, table, local_column, target_table)

        expected_unique_pairs = {
            "template_items": {"source_artifact_id", "item_code"},
            "atomic_rules": {"rubric_version_id", "rule_code"},
            "rule_levels": {"atomic_rule_id", "level_code"},
            "rule_template_links": {"rule_id", "template_item_id"},
        }
        for table, pair in expected_unique_pairs.items():
            assert frozenset(pair) in _unique_column_sets(inspector, table)
        for table in P1_02_TABLES - {"atomic_rule_source_rules"}:
            assert inspector.get_pk_constraint(table)["constrained_columns"] == ["id"]
        source_pk = inspector.get_pk_constraint("atomic_rule_source_rules")
        assert set(source_pk["constrained_columns"]) == {"atomic_rule_id", "source_rule_id"}

        check_sql = {
            table: " ".join(constraint["sqltext"] for constraint in inspector.get_check_constraints(table))
            for table in ("template_items", "atomic_rules", "rule_levels", "rule_template_links")
        }
        for token in ("kind", "strictness", "parse_confidence"):
            assert token in check_sql["template_items"]
        for token in ("direction", "effect_type", "repeat_policy", "judge_type", "max_points", "cap_points"):
            assert token in check_sql["atomic_rules"]
        for token in ("points", "display_order"):
            assert token in check_sql["rule_levels"]
        for token in ("relationship_type", "match_method", "review_status", "match_confidence"):
            assert token in check_sql["rule_template_links"]

        expected_enum_values = {
            "template_items": {
                "section",
                "content",
                "comment",
                "structure",
                "format",
                "required",
                "preferred",
                "unknown",
            },
            "atomic_rules": {
                "band",
                "deduct",
                "bonus",
                "none",
                "score",
                "review",
                "block_submission",
                "report_only",
                "once",
                "per_occurrence",
                "capped",
                "deterministic",
                "semantic",
                "required",
                "preferred",
                "unknown",
            },
            "rule_template_links": {
                "support",
                "constraint",
                "format_baseline",
                "exception",
                "exact",
                "heuristic",
                "llm_suggestion",
                "manual",
                "pending",
                "confirmed",
                "rejected",
            },
        }
        for table, values in expected_enum_values.items():
            assert all(value in check_sql[table] for value in values)
    finally:
        engine.dispose()


def test_0010_atomic_rules_migration_downgrades_to_0009_and_replays(monkeypatch, tmp_path):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "atomic-rules-replay.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    p1_01_revision, p1_02_revision = _p1_revisions(config)
    command.upgrade(config, p1_02_revision)

    user_id = "00000000-0000-0000-0000-000000000101"
    rubric_id = "00000000-0000-0000-0000-000000000102"
    compilation_id = "00000000-0000-0000-0000-000000000103"
    artifact_id = "00000000-0000-0000-0000-000000000104"
    source_rule_id = "00000000-0000-0000-0000-000000000105"
    version_id = "00000000-0000-0000-0000-000000000106"
    now = datetime(2026, 7, 16, 11, 0, 0)
    engine = create_engine(url)
    try:
        metadata = MetaData()
        users = Table("users", metadata, autoload_with=engine)
        rubrics = Table("rubrics", metadata, autoload_with=engine)
        compilations = Table("rubric_compilations", metadata, autoload_with=engine)
        artifacts = Table("source_artifacts", metadata, autoload_with=engine)
        source_rules = Table("source_rules", metadata, autoload_with=engine)
        versions = Table("rubric_versions", metadata, autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(
                users.insert().values(
                    id=user_id,
                    username="p102-migration-owner",
                    display_name="P1-02 迁移测试用户",
                    role="developer",
                    department=None,
                    password_hash=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                rubrics.insert().values(
                    id=rubric_id,
                    owner_id=user_id,
                    name="P1-02 迁移保留数据",
                    version="legacy-draft",
                    total_score=100,
                    status="draft",
                    description=None,
                    format_spec={},
                    created_by=user_id,
                    created_at=now,
                    published_at=None,
                )
            )
            connection.execute(
                compilations.insert().values(
                    id=compilation_id,
                    rubric_id=rubric_id,
                    status="validated",
                    parser_version="excel-v4/docx-v3",
                    compiler_version="atomic-v1",
                    model_provider=None,
                    model_name=None,
                    sampling_params={},
                    prompt_version="p102-migration",
                    raw_parse_output={},
                    raw_model_output={},
                    validation_result={"valid": True},
                    blockers=[],
                    warnings=[],
                    human_changes=[],
                    created_by=user_id,
                    reviewed_by=None,
                    reviewed_at=None,
                    published_at=None,
                    final_version_hash="9" * 64,
                    created_at=now,
                )
            )
            connection.execute(
                artifacts.insert().values(
                    id=artifact_id,
                    compilation_id=compilation_id,
                    artifact_type="excel",
                    file_name="迁移保留.xlsx",
                    file_hash="8" * 64,
                    file_size_bytes=1024,
                    uploaded_by=user_id,
                    created_at=now,
                )
            )
            connection.execute(
                source_rules.insert().values(
                    id=source_rule_id,
                    source_artifact_id=artifact_id,
                    source_rule_code="MIGRATION-KEEP",
                    sheet_name="原始规则",
                    row_number=2,
                    cell_locator="原始规则!A2:N2",
                    raw_text="0010 升降级不得改写这条 0009 原始规则。",
                    created_at=now,
                )
            )
            connection.execute(
                versions.insert().values(
                    id=version_id,
                    rubric_id=rubric_id,
                    compilation_id=compilation_id,
                    version="1.0.0",
                    workflow_profile="template_driven",
                    global_policy={},
                    version_hash="9" * 64,
                    created_by=user_id,
                    created_at=now,
                )
            )
    finally:
        engine.dispose()

    command.downgrade(config, p1_01_revision)
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert P1_02_TABLES.isdisjoint(tables)
        assert {"rubric_compilations", "source_artifacts", "source_rules", "rubric_versions"}.issubset(tables)
        _assert_p1_01_data_survives(engine, source_rule_id, version_id)
    finally:
        engine.dispose()

    command.upgrade(config, p1_02_revision)
    engine = create_engine(url)
    try:
        assert P1_02_TABLES.issubset(set(inspect(engine).get_table_names()))
        _assert_p1_01_data_survives(engine, source_rule_id, version_id)
    finally:
        engine.dispose()
