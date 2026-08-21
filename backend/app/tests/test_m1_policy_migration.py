"""M1 / 0012 frozen-policy migration contract tests.

The M1 implementation is intentionally split across several PRs.  Keeping this
contract in the tree before the migration exists makes the database boundary
reviewable without making the self-contained suite red: until an ``0012``
migration file is added every case is a *strict* expected failure.  Merely
adding that file automatically turns the contract on.

The A--G cases below are derived from ``docs/通用评分内核架构方案.md §3.3``
and ``代码改造计划.md §5.4``.  They deliberately exercise Alembic rather than
``Base.metadata.create_all()``.
"""

from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import JSON
from sqlalchemy import MetaData
from sqlalchemy import Numeric
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from backend.app.core.config import settings
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun
from backend.app.db.sqlite import enable_sqlite_foreign_keys


ROOT = Path(__file__).resolve().parents[3]
M0_HEAD = "0011_version_hash_on_update"
M1_FILES = tuple((ROOT / "alembic" / "versions").glob("0012*.py"))
M1_COLUMNS = {
    "scoring_runs": {"policy_snapshot", "policy_hash", "policy_schema_version"},
    "score_items": {"aggregation", "aggregation_schema_version", "auto_score_status"},
    "review_logs": {"policy_hash", "resolution_type"},
}

# This is intentionally xfail rather than skip: strict mode catches a test that
# stops depending on the migration gate and would otherwise silently XPASS.
pytestmark = pytest.mark.xfail(
    condition=not M1_FILES,
    reason="M1 migration 0012 has not been implemented yet",
    strict=True,
)


@pytest.fixture(autouse=True)
def _require_m1_migration_file():
    """Make every strict-xfail case fail during setup while 0012 is absent."""

    assert M1_FILES, "add the 0012 migration to activate the M1 database contract"


@pytest.fixture(scope="module")
def m1_revision():
    config = _bare_config()
    revisions = ScriptDirectory.from_config(config).walk_revisions()
    direct_children = []
    for revision in revisions:
        down_revisions = revision.down_revision
        if isinstance(down_revisions, str):
            down_revisions = (down_revisions,)
        if down_revisions and M0_HEAD in down_revisions:
            direct_children.append(revision.revision)

    assert len(direct_children) == 1, "M1 必须且只能新增一个 0011 的直接后继迁移"
    assert direct_children[0].startswith("0012"), "M1 policy 迁移必须使用 0012 revision"
    return direct_children[0]


def _bare_config():
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _database(monkeypatch, tmp_path, name):
    url = "sqlite+pysqlite:///%s" % (tmp_path / name)
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = _bare_config()
    # env.py reads settings.DATABASE_URL; keeping the Alembic option aligned
    # also makes this helper safe if env.py later adopts the conventional URL.
    config.set_main_option("sqlalchemy.url", url)
    return url, config


def _new_engine(url, *, foreign_keys=False):
    engine = create_engine(url)
    if foreign_keys:
        enable_sqlite_foreign_keys(engine)
    return engine


def _tables(engine, *names):
    metadata = MetaData()
    return {
        name: Table(name, metadata, autoload_with=engine)
        for name in names
    }


def _check_sql(inspector, table):
    return " ".join(
        str(constraint.get("sqltext") or "").lower()
        for constraint in inspector.get_check_constraints(table)
    )


def _current_revisions(engine):
    with engine.connect() as connection:
        return set(connection.scalars(text("SELECT version_num FROM alembic_version")))


def _full_rows(engine, *table_names):
    tables = _tables(engine, *table_names)
    projection = {}
    with engine.connect() as connection:
        for name, table in tables.items():
            rows = connection.execute(select(table).order_by(table.c.id)).mappings().all()
            projection[name] = [dict(row) for row in rows]
    return projection


def _schema_fingerprint(engine, *table_names):
    inspector = inspect(engine)
    result = {}
    for name in table_names:
        result[name] = {
            "columns": tuple(
                (
                    column["name"],
                    str(column["type"]),
                    column["nullable"],
                    column.get("default"),
                )
                for column in inspector.get_columns(name)
            ),
            "checks": tuple(
                sorted(str(item.get("sqltext") or "") for item in inspector.get_check_constraints(name))
            ),
            "foreign_keys": tuple(
                sorted(
                    (
                        tuple(item["constrained_columns"]),
                        item["referred_table"],
                        tuple(item["referred_columns"]),
                        tuple(sorted((item.get("options") or {}).items())),
                    )
                    for item in inspector.get_foreign_keys(name)
                )
            ),
            "primary_key": tuple(inspector.get_pk_constraint(name).get("constrained_columns") or ()),
            "unique_constraints": tuple(
                sorted(
                    (
                        tuple(item.get("column_names") or ()),
                        item.get("name") or "",
                    )
                    for item in inspector.get_unique_constraints(name)
                )
            ),
            "indexes": tuple(
                sorted(
                    (
                        tuple(item.get("column_names") or ()),
                        bool(item.get("unique")),
                        item.get("name") or "",
                    )
                    for item in inspector.get_indexes(name)
                )
            ),
        }
    return result


def _seed_legacy_graph(engine):
    """Insert one fully linked, pre-0012 run/item/review graph."""

    tables = _tables(
        engine,
        "users",
        "rubrics",
        "rubric_criteria",
        "grading_batches",
        "papers",
        "scoring_runs",
        "score_items",
        "review_logs",
    )
    now = datetime(2026, 7, 18, 9, 30, 0)
    with engine.begin() as connection:
        connection.execute(
            tables["users"].insert().values(
                id="m1-user",
                username="m1-policy-migration-user",
                display_name="M1 迁移测试用户",
                role="reviewer",
                department=None,
                password_hash=None,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            tables["rubrics"].insert().values(
                id="m1-rubric",
                owner_id="m1-user",
                name="M1 历史评分标准",
                version="legacy-v1",
                total_score=100,
                status="published",
                description=None,
                format_spec={},
                created_by="m1-user",
                created_at=now,
                published_at=now,
            )
        )
        connection.execute(
            tables["rubric_criteria"].insert().values(
                id="m1-criterion",
                rubric_id="m1-rubric",
                code="M1-C01",
                name="历史评分项",
                max_score=20,
                weight=None,
                description=None,
                evidence_hints=[],
                deduction_rules=[],
                display_order=1,
                created_at=now,
                criterion_type="llm_judgment",
                scoring_mode="llm_direct",
                applies_to="global",
                rubric_levels=[],
                sub_checks=[],
                dimension=None,
                deduction_rules_structured=[],
            )
        )
        connection.execute(
            tables["grading_batches"].insert().values(
                id="m1-batch",
                owner_id="m1-user",
                name="M1 历史批次",
                department=None,
                major=None,
                academic_year="2025-2026",
                paper_type="thesis",
                rubric_id="m1-rubric",
                status="completed",
                created_by="m1-user",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            tables["papers"].insert().values(
                id="m1-paper",
                owner_id="m1-user",
                batch_id="m1-batch",
                student_id="M1001",
                student_name="历史学生",
                title="M1 迁移保留论文",
                department=None,
                major=None,
                advisor=None,
                file_name="m1-legacy.docx",
                file_path="/legacy/m1-legacy.docx",
                parsed_text_path=None,
                parse_quality=None,
                status="scored",
                error_message=None,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            tables["scoring_runs"].insert().values(
                id="m1-run",
                owner_id="m1-user",
                paper_id="m1-paper",
                rubric_id="m1-rubric",
                model_provider="mock",
                model_name="mock-criterion-scorer",
                model_version="v1",
                status="completed",
                ai_total_score=16,
                final_total_score=17,
                grade="B",
                need_manual_review=False,
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                coherence_findings=[],
                format_findings=[],
                started_at=now,
                finished_at=now,
                created_at=now,
            )
        )
        connection.execute(
            tables["score_items"].insert().values(
                id="m1-item",
                scoring_run_id="m1-run",
                criterion_id="m1-criterion",
                max_score=20,
                ai_score=16,
                final_score=17,
                evidence_sufficient=True,
                reason="历史自动评分结果",
                deductions=[],
                deduction_items=[],
                evidence=[],
                band_selection=None,
                sub_results=None,
                suggestion=None,
                confidence=None,
                need_manual_review=False,
                raw_model_output=None,
                created_at=now,
            )
        )
        connection.execute(
            tables["review_logs"].insert().values(
                id="m1-review",
                scoring_run_id="m1-run",
                score_item_id="m1-item",
                reviewer_id="m1-user",
                before_score=16,
                after_score=17,
                reason="历史人工复核",
                created_at=now,
            )
        )


def _assert_integrity_error(engine, statement):
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(statement)


def _m1_projection(engine):
    tables = _tables(engine, "scoring_runs", "score_items", "review_logs")
    with engine.connect() as connection:
        run = connection.execute(
            select(
                tables["scoring_runs"].c.id,
                tables["scoring_runs"].c.policy_snapshot,
                tables["scoring_runs"].c.policy_hash,
                tables["scoring_runs"].c.policy_schema_version,
            ).where(tables["scoring_runs"].c.id == "m1-run")
        ).mappings().one()
        item = connection.execute(
            select(
                tables["score_items"].c.id,
                tables["score_items"].c.ai_score,
                tables["score_items"].c.aggregation,
                tables["score_items"].c.aggregation_schema_version,
                tables["score_items"].c.auto_score_status,
            ).where(tables["score_items"].c.id == "m1-item")
        ).mappings().one()
        review = connection.execute(
            select(
                tables["review_logs"].c.id,
                tables["review_logs"].c.policy_hash,
                tables["review_logs"].c.resolution_type,
            ).where(tables["review_logs"].c.id == "m1-review")
        ).mappings().one()
    return dict(run), dict(item), dict(review)


def test_m1_a_0012_upgrade_adds_typed_nullable_columns_and_sql_constraints(
    monkeypatch,
    tmp_path,
    m1_revision,
):
    """A. 0012 exposes the frozen policy/aggregation/review schema."""

    url, config = _database(monkeypatch, tmp_path, "m1-a-schema.db")
    command.upgrade(config, m1_revision)

    engine = _new_engine(url)
    try:
        inspector = inspect(engine)
        for table, names in M1_COLUMNS.items():
            columns = {column["name"]: column for column in inspector.get_columns(table)}
            assert names.issubset(columns)
            # Every new column must accept old rows without inventing identity.
            assert all(columns[name]["nullable"] is True for name in names)
            assert all(columns[name]["default"] is None for name in names)

        run_columns = {
            column["name"]: column for column in inspector.get_columns("scoring_runs")
        }
        item_columns = {
            column["name"]: column for column in inspector.get_columns("score_items")
        }
        review_columns = {
            column["name"]: column for column in inspector.get_columns("review_logs")
        }
        assert isinstance(run_columns["policy_snapshot"]["type"], JSON)
        assert isinstance(item_columns["aggregation"]["type"], JSON)
        # SQL JSON ``null`` would violate the all-or-none SQL checks while
        # looking like Python None on reads.  ORM writes must emit SQL NULL.
        assert ScoringRun.__table__.c.policy_snapshot.type.none_as_null is True
        assert ScoreItem.__table__.c.aggregation.type.none_as_null is True
        for columns, name in (
            (run_columns, "policy_hash"),
            (run_columns, "policy_schema_version"),
            (item_columns, "aggregation_schema_version"),
            (item_columns, "auto_score_status"),
            (review_columns, "policy_hash"),
            (review_columns, "resolution_type"),
        ):
            assert isinstance(columns[name]["type"], String)
        assert run_columns["policy_hash"]["type"].length == 64
        assert review_columns["policy_hash"]["type"].length == 64
        assert isinstance(item_columns["ai_score"]["type"], Numeric)
        assert item_columns["ai_score"]["nullable"] is True

        run_checks = _check_sql(inspector, "scoring_runs")
        for token in ("policy_snapshot", "policy_hash", "policy_schema_version"):
            assert token in run_checks
        item_checks = _check_sql(inspector, "score_items")
        for token in (
            "aggregation",
            "aggregation_schema_version",
            "auto_score_status",
            "calculated",
            "invalid",
            "blocked",
            "ai_score",
        ):
            assert token in item_checks
        review_checks = _check_sql(inspector, "review_logs")
        for token in (
            "policy_hash",
            "resolution_type",
            "ordinary_override",
            "resolve_validation",
            "resolve_block",
            "rescore",
        ):
            assert token in review_checks
    finally:
        engine.dispose()


def test_m1_b_0012_upgrade_preserves_legacy_values_and_leaves_new_identity_null(
    monkeypatch,
    tmp_path,
    m1_revision,
):
    """B. An upgrade does not fabricate policy or aggregation for history."""

    url, config = _database(monkeypatch, tmp_path, "m1-b-history.db")
    command.upgrade(config, M0_HEAD)
    engine = _new_engine(url, foreign_keys=True)
    try:
        _seed_legacy_graph(engine)
    finally:
        engine.dispose()

    command.upgrade(config, m1_revision)
    engine = _new_engine(url, foreign_keys=True)
    try:
        run, item, review = _m1_projection(engine)
        assert run == {
            "id": "m1-run",
            "policy_snapshot": None,
            "policy_hash": None,
            "policy_schema_version": None,
        }
        assert item["id"] == "m1-item"
        assert item["ai_score"] == 16
        assert item["aggregation"] is None
        assert item["aggregation_schema_version"] is None
        assert item["auto_score_status"] is None
        assert review == {
            "id": "m1-review",
            "policy_hash": None,
            "resolution_type": None,
        }
    finally:
        engine.dispose()


def test_m1_c_0012_database_constraints_accept_only_coherent_new_states(
    monkeypatch,
    tmp_path,
    m1_revision,
):
    """C. SQLite itself rejects partial identity and impossible statuses."""

    url, config = _database(monkeypatch, tmp_path, "m1-c-constraints.db")
    command.upgrade(config, M0_HEAD)
    engine = _new_engine(url, foreign_keys=True)
    try:
        _seed_legacy_graph(engine)
    finally:
        engine.dispose()
    command.upgrade(config, m1_revision)

    engine = _new_engine(url, foreign_keys=True)
    try:
        tables = _tables(engine, "scoring_runs", "score_items", "review_logs")
        policy_hash = "a" * 64
        policy = {
            "schema_version": "scoring-policy@1",
            "aggregation_mode": "weighted_normalized",
            "rounding": {"mode": "half_up", "digits": 2},
        }
        aggregation = {
            "raw": "16.00",
            "max": "20.00",
            "weight": None,
            "contribution": "16.00",
        }

        run_identity = {
            "policy_snapshot": policy,
            "policy_hash": policy_hash,
            "policy_schema_version": "scoring-policy@1",
        }
        run_identity_names = tuple(run_identity)
        for mask in range(1, (1 << len(run_identity_names)) - 1):
            partial = {
                name: run_identity[name]
                for index, name in enumerate(run_identity_names)
                if mask & (1 << index)
            }
            _assert_integrity_error(
                engine,
                update(tables["scoring_runs"])
                .where(tables["scoring_runs"].c.id == "m1-run")
                .values(**partial),
            )
        _assert_integrity_error(
            engine,
            update(tables["scoring_runs"])
            .where(tables["scoring_runs"].c.id == "m1-run")
            .values(
                policy_snapshot=policy,
                policy_hash="too-short",
                policy_schema_version="scoring-policy@1",
            ),
        )
        _assert_integrity_error(
            engine,
            update(tables["scoring_runs"])
            .where(tables["scoring_runs"].c.id == "m1-run")
            .values(
                policy_snapshot=policy,
                policy_hash="g" * 64,
                policy_schema_version="scoring-policy@1",
            ),
        )
        _assert_integrity_error(
            engine,
            update(tables["scoring_runs"])
            .where(tables["scoring_runs"].c.id == "m1-run")
            .values(
                policy_snapshot=policy,
                policy_hash="A" * 64,
                policy_schema_version="scoring-policy@1",
            ),
        )
        _assert_integrity_error(
            engine,
            update(tables["scoring_runs"])
            .where(tables["scoring_runs"].c.id == "m1-run")
            .values(
                policy_snapshot=policy,
                policy_hash=policy_hash,
                policy_schema_version="",
            ),
        )
        with engine.begin() as connection:
            connection.execute(
                update(tables["scoring_runs"])
                .where(tables["scoring_runs"].c.id == "m1-run")
                .values(
                    policy_snapshot=policy,
                    policy_hash=policy_hash,
                    policy_schema_version="scoring-policy@1",
                )
            )

        item_identity = {
            "aggregation": aggregation,
            "aggregation_schema_version": "score-item-aggregation@1",
            "auto_score_status": "calculated",
        }
        item_identity_names = tuple(item_identity)
        for mask in range(1, (1 << len(item_identity_names)) - 1):
            partial = {
                name: item_identity[name]
                for index, name in enumerate(item_identity_names)
                if mask & (1 << index)
            }
            _assert_integrity_error(
                engine,
                update(tables["score_items"])
                .where(tables["score_items"].c.id == "m1-item")
                .values(**partial),
            )
        _assert_integrity_error(
            engine,
            update(tables["score_items"])
            .where(tables["score_items"].c.id == "m1-item")
            .values(ai_score=None),
        )
        _assert_integrity_error(
            engine,
            update(tables["score_items"])
            .where(tables["score_items"].c.id == "m1-item")
            .values(auto_score_status="made_up_status"),
        )
        _assert_integrity_error(
            engine,
            update(tables["score_items"])
            .where(tables["score_items"].c.id == "m1-item")
            .values(
                aggregation=aggregation,
                aggregation_schema_version="score-item-aggregation@1",
                auto_score_status="calculated",
                ai_score=None,
            ),
        )
        _assert_integrity_error(
            engine,
            update(tables["score_items"])
            .where(tables["score_items"].c.id == "m1-item")
            .values(
                aggregation={
                    "raw": None,
                    "max": "20.00",
                    "weight": None,
                    "contribution": None,
                },
                aggregation_schema_version="score-item-aggregation@1",
                auto_score_status="invalid",
                ai_score=16,
            ),
        )
        _assert_integrity_error(
            engine,
            update(tables["score_items"])
            .where(tables["score_items"].c.id == "m1-item")
            .values(
                aggregation=aggregation,
                aggregation_schema_version="",
                auto_score_status="calculated",
                ai_score=16,
            ),
        )
        with engine.begin() as connection:
            connection.execute(
                update(tables["score_items"])
                .where(tables["score_items"].c.id == "m1-item")
                .values(
                    aggregation=aggregation,
                    aggregation_schema_version="score-item-aggregation@1",
                    auto_score_status="calculated",
                    ai_score=16,
                )
            )
        with engine.begin() as connection:
            connection.execute(
                update(tables["score_items"])
                .where(tables["score_items"].c.id == "m1-item")
                .values(
                    aggregation={
                        "raw": None,
                        "max": "20.00",
                        "weight": None,
                        "contribution": None,
                    },
                    aggregation_schema_version="score-item-aggregation@1",
                    auto_score_status="blocked",
                    ai_score=None,
                    final_score=None,
                )
            )
            connection.execute(
                update(tables["scoring_runs"])
                .where(tables["scoring_runs"].c.id == "m1-run")
                .values(
                    ai_total_score=None,
                    final_total_score=None,
                    grade=None,
                    need_manual_review=True,
                )
            )

        _assert_integrity_error(
            engine,
            update(tables["review_logs"])
            .where(tables["review_logs"].c.id == "m1-review")
            .values(resolution_type="ordinary_override"),
        )
        _assert_integrity_error(
            engine,
            update(tables["review_logs"])
            .where(tables["review_logs"].c.id == "m1-review")
            .values(policy_hash=policy_hash),
        )
        _assert_integrity_error(
            engine,
            update(tables["review_logs"])
            .where(tables["review_logs"].c.id == "m1-review")
            .values(policy_hash=policy_hash, resolution_type="erase_validation"),
        )
        _assert_integrity_error(
            engine,
            update(tables["review_logs"])
            .where(tables["review_logs"].c.id == "m1-review")
            .values(policy_hash="A" * 64, resolution_type="ordinary_override"),
        )
        _assert_integrity_error(
            engine,
            update(tables["review_logs"])
            .where(tables["review_logs"].c.id == "m1-review")
            .values(policy_hash="too-short", resolution_type="ordinary_override"),
        )
        _assert_integrity_error(
            engine,
            update(tables["review_logs"])
            .where(tables["review_logs"].c.id == "m1-review")
            .values(policy_hash="g" * 64, resolution_type="ordinary_override"),
        )
        for resolution_type in (
            "ordinary_override",
            "resolve_validation",
            "resolve_block",
            "rescore",
        ):
            with engine.begin() as connection:
                connection.execute(
                    update(tables["review_logs"])
                    .where(tables["review_logs"].c.id == "m1-review")
                    .values(policy_hash=policy_hash, resolution_type=resolution_type)
                )
            with engine.connect() as connection:
                assert connection.scalar(
                    select(tables["review_logs"].c.resolution_type).where(
                        tables["review_logs"].c.id == "m1-review"
                    )
                ) == resolution_type
    finally:
        engine.dispose()


def test_m1_d_0012_empty_upgrade_downgrade_and_replay(
    monkeypatch,
    tmp_path,
    m1_revision,
):
    """D. An empty 0011 database can cross the boundary in both directions."""

    url, config = _database(monkeypatch, tmp_path, "m1-d-empty-replay.db")
    command.upgrade(config, m1_revision)
    command.downgrade(config, M0_HEAD)

    engine = _new_engine(url)
    try:
        inspector = inspect(engine)
        assert _current_revisions(engine) == {M0_HEAD}
        for table, added_columns in M1_COLUMNS.items():
            downgraded_columns = {
                column["name"] for column in inspector.get_columns(table)
            }
            assert downgraded_columns.isdisjoint(added_columns)
        score_columns = {
            column["name"]: column for column in inspector.get_columns("score_items")
        }
        assert score_columns["ai_score"]["nullable"] is False
    finally:
        engine.dispose()

    command.upgrade(config, m1_revision)
    engine = _new_engine(url)
    try:
        assert _current_revisions(engine) == {m1_revision}
        assert "policy_snapshot" in {
            column["name"] for column in inspect(engine).get_columns("scoring_runs")
        }
    finally:
        engine.dispose()


def test_m1_e_0012_legacy_null_data_downgrades_without_loss_and_replays(
    monkeypatch,
    tmp_path,
    m1_revision,
):
    """E. Representable legacy rows survive an 0012 -> 0011 -> 0012 replay."""

    url, config = _database(monkeypatch, tmp_path, "m1-e-legacy-replay.db")
    command.upgrade(config, M0_HEAD)
    engine = _new_engine(url, foreign_keys=True)
    try:
        _seed_legacy_graph(engine)
        legacy_before = _full_rows(engine, "scoring_runs", "score_items", "review_logs")
        legacy_schema_before = _schema_fingerprint(
            engine,
            "scoring_runs",
            "score_items",
            "review_logs",
        )
    finally:
        engine.dispose()
    command.upgrade(config, m1_revision)
    before = None
    engine = _new_engine(url, foreign_keys=True)
    try:
        before = _m1_projection(engine)
    finally:
        engine.dispose()

    command.downgrade(config, M0_HEAD)
    engine = _new_engine(url, foreign_keys=True)
    try:
        assert _full_rows(engine, "scoring_runs", "score_items", "review_logs") == legacy_before
        assert _schema_fingerprint(
            engine,
            "scoring_runs",
            "score_items",
            "review_logs",
        ) == legacy_schema_before
    finally:
        engine.dispose()

    command.upgrade(config, m1_revision)
    engine = _new_engine(url, foreign_keys=True)
    try:
        assert _m1_projection(engine) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "scenario",
    ("frozen_policy", "aggregation", "invalid_score", "blocked_score", "review_resolution"),
)
def test_m1_f_0012_rejects_downgrade_for_unrepresentable_state_without_damage(
    monkeypatch,
    tmp_path,
    m1_revision,
    scenario,
):
    """F. 0012 refuses lossy downgrade before making any schema changes."""

    url, config = _database(monkeypatch, tmp_path, f"m1-f-{scenario}.db")
    command.upgrade(config, M0_HEAD)
    engine = _new_engine(url, foreign_keys=True)
    try:
        _seed_legacy_graph(engine)
    finally:
        engine.dispose()
    command.upgrade(config, m1_revision)

    policy_hash = "b" * 64
    aggregation = {
        "raw": "16.00",
        "max": "20.00",
        "weight": None,
        "contribution": "16.00",
    }
    engine = _new_engine(url, foreign_keys=True)
    try:
        tables = _tables(engine, "scoring_runs", "score_items", "review_logs")
        with engine.begin() as connection:
            if scenario == "frozen_policy":
                connection.execute(
                    update(tables["scoring_runs"])
                    .where(tables["scoring_runs"].c.id == "m1-run")
                    .values(
                        policy_snapshot={"schema_version": "scoring-policy@1"},
                        policy_hash=policy_hash,
                        policy_schema_version="scoring-policy@1",
                    )
                )
            elif scenario == "aggregation":
                connection.execute(
                    update(tables["score_items"])
                    .where(tables["score_items"].c.id == "m1-item")
                    .values(
                        aggregation=aggregation,
                        aggregation_schema_version="score-item-aggregation@1",
                        auto_score_status="calculated",
                    )
                )
            elif scenario in {"invalid_score", "blocked_score"}:
                connection.execute(
                    update(tables["score_items"])
                    .where(tables["score_items"].c.id == "m1-item")
                    .values(
                        ai_score=None,
                        aggregation={
                            "raw": None,
                            "max": "20.00",
                            "weight": None,
                            "contribution": None,
                        },
                        aggregation_schema_version="score-item-aggregation@1",
                        auto_score_status=(
                            "invalid" if scenario == "invalid_score" else "blocked"
                        ),
                        final_score=None,
                    )
                )
                connection.execute(
                    update(tables["scoring_runs"])
                    .where(tables["scoring_runs"].c.id == "m1-run")
                    .values(
                        ai_total_score=None,
                        final_total_score=None,
                        grade=None,
                        need_manual_review=True,
                    )
                )
            else:
                connection.execute(
                    update(tables["review_logs"])
                    .where(tables["review_logs"].c.id == "m1-review")
                    .values(policy_hash=policy_hash, resolution_type="rescore")
                )
        before = _full_rows(engine, "scoring_runs", "score_items", "review_logs")
        before_schema = _schema_fingerprint(
            engine,
            "scoring_runs",
            "score_items",
            "review_logs",
        )
    finally:
        engine.dispose()

    with pytest.raises(Exception) as exc_info:
        command.downgrade(config, M0_HEAD)
    message = f"{type(exc_info.value).__name__}: {exc_info.value}".lower()
    assert any(token in message for token in ("export", "导出", "downgrade", "降级"))

    engine = _new_engine(url, foreign_keys=True)
    try:
        assert _current_revisions(engine) == {m1_revision}
        assert _schema_fingerprint(
            engine,
            "scoring_runs",
            "score_items",
            "review_logs",
        ) == before_schema
        assert _full_rows(engine, "scoring_runs", "score_items", "review_logs") == before
    finally:
        engine.dispose()


def test_m1_g_0012_is_on_head_lineage_and_preserves_sqlite_foreign_keys(
    monkeypatch,
    tmp_path,
    m1_revision,
):
    """G. 0012 stays on the head lineage and its table rebuilds retain FKs."""

    url, config = _database(monkeypatch, tmp_path, "m1-g-head-fk.db")
    command.upgrade(config, m1_revision)

    script = ScriptDirectory.from_config(config)
    heads = set(script.get_heads())
    assert heads
    assert any(
        m1_revision
        in {revision.revision for revision in script.iterate_revisions(head, "base")}
        for head in heads
    )

    engine = _new_engine(url, foreign_keys=True)
    try:
        inspector = inspect(engine)
        assert _current_revisions(engine) == {m1_revision}

        def foreign_key_pairs(table):
            return {
                (
                    tuple(foreign_key["constrained_columns"]),
                    foreign_key["referred_table"],
                    tuple(foreign_key["referred_columns"]),
                )
                for foreign_key in inspector.get_foreign_keys(table)
            }

        assert {
            (("paper_id",), "papers", ("id",)),
            (("rubric_id",), "rubrics", ("id",)),
        }.issubset(foreign_key_pairs("scoring_runs"))
        assert {
            (("scoring_run_id",), "scoring_runs", ("id",)),
            (("criterion_id",), "rubric_criteria", ("id",)),
        }.issubset(foreign_key_pairs("score_items"))
        assert {
            (("scoring_run_id",), "scoring_runs", ("id",)),
            (("score_item_id",), "score_items", ("id",)),
            (("reviewer_id",), "users", ("id",)),
        }.issubset(foreign_key_pairs("review_logs"))

        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1

        _seed_legacy_graph(engine)
        score_items = _tables(engine, "score_items")["score_items"]
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            with engine.begin() as connection:
                connection.execute(
                    score_items.insert().values(
                        id="m1-orphan-item",
                        scoring_run_id="missing-run",
                        criterion_id="m1-criterion",
                        max_score=20,
                        ai_score=10,
                        final_score=None,
                        evidence_sufficient=True,
                        reason="不得写入的孤儿项",
                        deductions=[],
                        deduction_items=[],
                        evidence=[],
                        band_selection=None,
                        sub_results=None,
                        suggestion=None,
                        confidence=None,
                        need_manual_review=False,
                        raw_model_output=None,
                        created_at=datetime(2026, 7, 18, 10, 0, 0),
                    )
                )
    finally:
        engine.dispose()
