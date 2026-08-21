"""M3 / 0013 version, replay identity and idempotency migration contract.

The migration is deliberately exercised through Alembic instead of
``Base.metadata.create_all``.  Until 0013 exists, every case is a strict
expected failure caused only by :class:`M3CapabilityUnavailable`.  Adding an
0013 file activates the complete contract; migration and assertion failures
then remain ordinary failures.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import JSON
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import Text
from sqlalchemy import create_engine
from sqlalchemy import delete
from sqlalchemy import inspect
from sqlalchemy import null
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.core.config import settings
from backend.app.db.sqlite import enable_sqlite_foreign_keys


ROOT = Path(__file__).resolve().parents[3]
M2_HEAD = "0012_frozen_scoring_policy"
M3_FILES = tuple(sorted((ROOT / "alembic" / "versions").glob("0013*.py")))


class M3CapabilityUnavailable(RuntimeError):
    """Raised solely while the 0013 migration capability is absent."""


pytestmark = pytest.mark.xfail(
    condition=not M3_FILES,
    reason="M3 migration 0013 has not been implemented yet",
    raises=M3CapabilityUnavailable,
    strict=True,
)


@pytest.fixture(autouse=True)
def _require_m3_migration_file():
    if not M3_FILES:
        raise M3CapabilityUnavailable("add migration 0013 to activate the M3 DB contract")


@pytest.fixture(scope="module")
def m3_revision():
    # Module-scoped fixtures are initialized before function-scoped autouse
    # fixtures, so the missing capability must be gated here as well.
    if not M3_FILES:
        raise M3CapabilityUnavailable("add migration 0013 to activate the M3 DB contract")
    assert len(M3_FILES) == 1, "M3 必须且只能新增一个 0013 迁移文件"
    config = _bare_config()
    revisions = ScriptDirectory.from_config(config).walk_revisions()
    direct_children = []
    for revision in revisions:
        down_revisions = revision.down_revision
        if isinstance(down_revisions, str):
            down_revisions = (down_revisions,)
        if down_revisions and M2_HEAD in down_revisions:
            direct_children.append(revision.revision)

    assert len(direct_children) == 1, "M3 必须且只能新增一个 0012 的直接后继迁移"
    assert direct_children[0].startswith("0013"), "M3 迁移必须使用 0013 revision"
    return direct_children[0]


M3_RUN_COLUMNS = {
    "rubric_source_kind",
    "rubric_snapshot_hash",
    "rubric_version_id",
    "rubric_version_hash",
    "rubric_hash_scheme",
    "business_profile_key",
    "workflow_profile",
    "execution_plan_snapshot",
    "execution_plan_hash",
    "plan_schema_version",
    "checker_manifest",
    "source_artifact_hash",
    "normalized_content_hash",
    "document_snapshot_ref",
    "document_snapshot_hash",
    "document_schema_version",
    "engine_version",
    "rescore_generation",
    "idempotency_key",
}

CORE_REQUIRED_FIELDS = (
    "rubric_source_kind",
    "rubric_snapshot_hash",
    "business_profile_key",
    "workflow_profile",
    "execution_plan_snapshot",
    "execution_plan_hash",
    "plan_schema_version",
    "checker_manifest",
    "source_artifact_hash",
    "normalized_content_hash",
    "document_snapshot_ref",
    "document_snapshot_hash",
    "document_schema_version",
    "engine_version",
    "rescore_generation",
    "idempotency_key",
    # 0012 introduced policy identity, but a new M3 Core run must bind it to
    # the rest of the replay identity rather than accepting the all-NULL
    # historical branch of the older check constraint.
    "policy_snapshot",
    "policy_hash",
    "policy_schema_version",
)

CORE_DIGEST_FIELDS = (
    "rubric_snapshot_hash",
    "execution_plan_hash",
    "source_artifact_hash",
    "normalized_content_hash",
    "document_snapshot_hash",
    "idempotency_key",
    "policy_hash",
)

CORE_NONEMPTY_TEXT_FIELDS = (
    "business_profile_key",
    "workflow_profile",
    "plan_schema_version",
    "document_snapshot_ref",
    "document_schema_version",
    "engine_version",
    "policy_schema_version",
)


def _bare_config():
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _database(monkeypatch, tmp_path, name):
    url = "sqlite+pysqlite:///%s" % (tmp_path / name)
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = _bare_config()
    config.set_main_option("sqlalchemy.url", url)
    return url, config


def _new_engine(url, *, foreign_keys=False):
    engine = create_engine(url)
    if foreign_keys:
        enable_sqlite_foreign_keys(engine)
    return engine


def _tables(engine, *names):
    metadata = MetaData()
    return {name: Table(name, metadata, autoload_with=engine) for name in names}


def _current_revisions(engine):
    with engine.connect() as connection:
        return set(connection.scalars(text("SELECT version_num FROM alembic_version")))


def _check_sql(inspector, table_name):
    return " ".join(
        str(item.get("sqltext") or "").lower()
        for item in inspector.get_check_constraints(table_name)
    )


def _schema_fingerprint(engine, *table_names):
    inspector = inspect(engine)
    return {
        name: {
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
                sorted(
                    str(item.get("sqltext") or "")
                    for item in inspector.get_check_constraints(name)
                )
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
            "unique_constraints": tuple(
                sorted(
                    (tuple(item.get("column_names") or ()), item.get("name") or "")
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
        for name in table_names
    }


def _full_rows(engine, *table_names):
    tables = _tables(engine, *table_names)
    with engine.connect() as connection:
        return {
            name: [
                dict(row)
                for row in connection.execute(select(table).order_by(table.c.id))
                .mappings()
                .all()
            ]
            for name, table in tables.items()
        }


def _assert_integrity_error(engine, statement):
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(statement)


def _seed_graph(engine, *, two_runs=False):
    """Seed a published v1 version plus legacy batch/run rows at 0012."""

    tables = _tables(
        engine,
        "users",
        "rubrics",
        "rubric_criteria",
        "rubric_compilations",
        "rubric_versions",
        "grading_batches",
        "papers",
        "scoring_runs",
        "score_items",
    )
    now = datetime(2026, 7, 19, 9, 30, 0)
    version_hashes = {"one": "a" * 64, "two": "b" * 64}
    with engine.begin() as connection:
        connection.execute(
            tables["users"].insert().values(
                id="m3-user",
                username="m3-migration-user",
                display_name="M3 migration user",
                role="reviewer",
                department=None,
                password_hash=None,
                created_at=now,
                updated_at=now,
            )
        )
        for suffix in ("one", "two"):
            connection.execute(
                tables["rubrics"].insert().values(
                    id=f"m3-rubric-{suffix}",
                    owner_id="m3-user",
                    name=f"M3 rubric {suffix}",
                    version="v1",
                    total_score=100,
                    status="published",
                    description=None,
                    format_spec={},
                    created_by="m3-user",
                    created_at=now,
                    published_at=now,
                )
            )
            connection.execute(
                tables["rubric_criteria"].insert().values(
                    id=f"m3-criterion-{suffix}",
                    rubric_id=f"m3-rubric-{suffix}",
                    code=f"M3-{suffix.upper()}",
                    name=f"criterion {suffix}",
                    max_score=100,
                    weight=None,
                    description=None,
                    evidence_hints=[],
                    deduction_rules=[],
                    display_order=0,
                    criterion_type="llm_judgment",
                    scoring_mode="llm_direct",
                    applies_to="global",
                    rubric_levels=[],
                    sub_checks=[],
                    dimension=None,
                    deduction_rules_structured=[],
                    created_at=now,
                )
            )
            connection.execute(
                tables["rubric_compilations"].insert().values(
                    id=f"m3-compilation-{suffix}",
                    rubric_id=f"m3-rubric-{suffix}",
                    status="published",
                    parser_version="parser@1",
                    compiler_version="compiler@1",
                    model_provider=None,
                    model_name=None,
                    sampling_params={},
                    prompt_version="prompt@1",
                    raw_parse_output={},
                    raw_model_output={},
                    validation_result={"valid": True},
                    blockers=[],
                    warnings=[],
                    human_changes=[],
                    created_by="m3-user",
                    reviewed_by="m3-user",
                    reviewed_at=now,
                    published_at=now,
                    final_version_hash=version_hashes[suffix],
                    created_at=now,
                )
            )
            connection.execute(
                tables["rubric_versions"].insert().values(
                    id=f"m3-version-{suffix}",
                    rubric_id=f"m3-rubric-{suffix}",
                    compilation_id=f"m3-compilation-{suffix}",
                    version="v1",
                    workflow_profile="thesis",
                    global_policy={"rounding": {"mode": "half_up", "digits": 2}},
                    version_hash=version_hashes[suffix],
                    created_by="m3-user",
                    created_at=now,
                )
            )

        connection.execute(
            tables["grading_batches"].insert().values(
                id="m3-batch",
                owner_id="m3-user",
                name="M3 legacy batch",
                department=None,
                major=None,
                academic_year="2025-2026",
                paper_type="thesis",
                rubric_id="m3-rubric-one",
                status="completed",
                created_by="m3-user",
                created_at=now,
                updated_at=now,
            )
        )
        run_ids = ("m3-run-one", "m3-run-two") if two_runs else ("m3-run-one",)
        for index, run_id in enumerate(run_ids, start=1):
            paper_id = f"m3-paper-{index}"
            connection.execute(
                tables["papers"].insert().values(
                    id=paper_id,
                    owner_id="m3-user",
                    batch_id="m3-batch",
                    student_id=f"M3-{index:03d}",
                    student_name=f"student {index}",
                    title=f"legacy paper {index}",
                    department=None,
                    major=None,
                    advisor=None,
                    file_name=f"m3-{index}.docx",
                    file_path=f"/legacy/m3-{index}.docx",
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
                    id=run_id,
                    owner_id="m3-user",
                    paper_id=paper_id,
                    rubric_id="m3-rubric-one",
                    model_provider="mock",
                    model_name="mock-criterion-scorer",
                    model_version="v1",
                    status="completed",
                    ai_total_score=80,
                    final_total_score=80,
                    grade="B",
                    need_manual_review=False,
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                    coherence_findings=[],
                    format_findings=[],
                    policy_snapshot=None,
                    policy_hash=None,
                    policy_schema_version=None,
                    started_at=now,
                    finished_at=now,
                    created_at=now,
                )
            )
        connection.execute(
            tables["score_items"].insert().values(
                id="m3-item-one",
                scoring_run_id="m3-run-one",
                criterion_id="m3-criterion-one",
                max_score=100,
                ai_score=80,
                final_score=80,
                evidence_sufficient=True,
                reason="legacy result",
                deductions=[],
                deduction_items=[],
                evidence=[],
                band_selection=None,
                sub_results=None,
                suggestion=None,
                confidence=None,
                need_manual_review=False,
                raw_model_output=None,
                aggregation=None,
                aggregation_schema_version=None,
                auto_score_status=None,
                created_at=now,
            )
        )
    return {
        "rubric_id": "m3-rubric-one",
        "other_rubric_id": "m3-rubric-two",
        "version_id": "m3-version-one",
        "other_version_id": "m3-version-two",
        "version_hash": version_hashes["one"],
        "other_version_hash": version_hashes["two"],
        "batch_id": "m3-batch",
        "run_id": "m3-run-one",
        "other_run_id": "m3-run-two" if two_runs else None,
        "item_id": "m3-item-one",
    }


def _core_identity(ids, *, published=False, generation=0, idempotency_key=None):
    value = {
        "rubric_source_kind": (
            "published_version" if published else "legacy_unversioned"
        ),
        "rubric_snapshot_hash": "c" * 64,
        "rubric_version_id": ids["version_id"] if published else None,
        "rubric_version_hash": ids["version_hash"] if published else None,
        "rubric_hash_scheme": "rubric-content-v1" if published else None,
        "business_profile_key": "thesis",
        "workflow_profile": "thesis",
        "execution_plan_snapshot": {
            "schema_version": "rule-execution-plan@2",
            "nodes": [],
        },
        "execution_plan_hash": "d" * 64,
        "plan_schema_version": "rule-execution-plan@2",
        "checker_manifest": {},
        "source_artifact_hash": "e" * 64,
        "normalized_content_hash": "f" * 64,
        "document_snapshot_ref": "objects/sha256/" + "1" * 64 + ".json",
        "document_snapshot_hash": "1" * 64,
        "document_schema_version": "document-snapshot@1",
        "engine_version": "scoring-core@1",
        "rescore_generation": generation,
        "idempotency_key": idempotency_key or ("2" * 64),
        "policy_snapshot": {"schema_version": "scoring-policy@1"},
        "policy_hash": "3" * 64,
        "policy_schema_version": "scoring-policy@1",
    }
    return value


def test_m3_a_0013_upgrade_adds_typed_audit_columns_and_constraints(
    monkeypatch,
    tmp_path,
    m3_revision,
):
    url, config = _database(monkeypatch, tmp_path, "m3-a-schema.db")
    command.upgrade(config, m3_revision)

    engine = _new_engine(url)
    try:
        inspector = inspect(engine)
        version_columns = {
            item["name"]: item for item in inspector.get_columns("rubric_versions")
        }
        assert {"business_profile_key", "hash_scheme"}.issubset(version_columns)
        assert version_columns["business_profile_key"]["nullable"] is False
        assert version_columns["hash_scheme"]["nullable"] is False
        assert isinstance(version_columns["business_profile_key"]["type"], String)
        assert isinstance(version_columns["hash_scheme"]["type"], String)
        # thesis/v1 are migration backfills, not defaults for future writes.
        # Leaving a server default here would silently misclassify new profiles.
        assert version_columns["business_profile_key"].get("default") is None
        assert version_columns["hash_scheme"].get("default") is None

        batch_columns = {
            item["name"]: item for item in inspector.get_columns("grading_batches")
        }
        assert "rubric_version_id" in batch_columns
        assert batch_columns["rubric_version_id"]["nullable"] is True
        assert batch_columns["rubric_version_id"].get("default") is None

        run_columns = {
            item["name"]: item for item in inspector.get_columns("scoring_runs")
        }
        assert M3_RUN_COLUMNS.issubset(run_columns)
        assert all(run_columns[name]["nullable"] is True for name in M3_RUN_COLUMNS)
        assert all(run_columns[name].get("default") is None for name in M3_RUN_COLUMNS)
        for name in (
            "execution_plan_snapshot",
            "checker_manifest",
        ):
            assert isinstance(run_columns[name]["type"], JSON)
        assert isinstance(run_columns["document_snapshot_ref"]["type"], (String, Text))
        assert isinstance(run_columns["rescore_generation"]["type"], Integer)
        for name in (
            "rubric_snapshot_hash",
            "rubric_version_hash",
            "execution_plan_hash",
            "source_artifact_hash",
            "normalized_content_hash",
            "document_snapshot_hash",
            "idempotency_key",
        ):
            assert isinstance(run_columns[name]["type"], String)
            assert run_columns[name]["type"].length == 64

        item_columns = {
            item["name"]: item for item in inspector.get_columns("score_items")
        }
        assert isinstance(item_columns["rule_results"]["type"], JSON)
        assert item_columns["rule_results"]["nullable"] is True
        assert isinstance(item_columns["rule_results_schema_version"]["type"], String)
        assert item_columns["rule_results_schema_version"]["nullable"] is True

        run_checks = _check_sql(inspector, "scoring_runs")
        for token in (
            "rubric_source_kind",
            "document_snapshot_ref",
            "document_snapshot_hash",
            "rescore_generation",
            "idempotency_key",
            "published_version",
            "legacy_unversioned",
        ):
            assert token in run_checks
        item_checks = _check_sql(inspector, "score_items")
        assert "rule_results" in item_checks
        assert "rule_results_schema_version" in item_checks

        unique_sets = {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints("scoring_runs")
        } | {
            tuple(item.get("column_names") or ())
            for item in inspector.get_indexes("scoring_runs")
            if item.get("unique")
        }
        assert ("idempotency_key",) in unique_sets
    finally:
        engine.dispose()


def test_m3_b_upgrade_backfills_versions_but_does_not_invent_historical_run_identity(
    monkeypatch,
    tmp_path,
    m3_revision,
):
    url, config = _database(monkeypatch, tmp_path, "m3-b-backfill.db")
    command.upgrade(config, M2_HEAD)
    engine = _new_engine(url, foreign_keys=True)
    try:
        ids = _seed_graph(engine)
    finally:
        engine.dispose()

    command.upgrade(config, m3_revision)
    engine = _new_engine(url, foreign_keys=True)
    try:
        tables = _tables(
            engine, "rubric_versions", "grading_batches", "scoring_runs", "score_items"
        )
        with engine.connect() as connection:
            version = connection.execute(
                select(tables["rubric_versions"]).where(
                    tables["rubric_versions"].c.id == ids["version_id"]
                )
            ).mappings().one()
            assert version["business_profile_key"] == "thesis"
            assert version["hash_scheme"] == "rubric-content-v1"
            assert version["version_hash"] == ids["version_hash"]
            assert connection.scalar(
                select(tables["grading_batches"].c.rubric_version_id).where(
                    tables["grading_batches"].c.id == ids["batch_id"]
                )
            ) is None
            run = connection.execute(
                select(*(tables["scoring_runs"].c[name] for name in sorted(M3_RUN_COLUMNS))).where(
                    tables["scoring_runs"].c.id == ids["run_id"]
                )
            ).one()
            assert all(value is None for value in run)
            item = connection.execute(
                select(
                    tables["score_items"].c.rule_results,
                    tables["score_items"].c.rule_results_schema_version,
                ).where(tables["score_items"].c.id == ids["item_id"])
            ).one()
            assert item == (None, None)
    finally:
        engine.dispose()


def test_m3_c_core_identity_is_atomic_and_document_hash_without_ref_is_never_valid(
    monkeypatch,
    tmp_path,
    m3_revision,
):
    url, config = _database(monkeypatch, tmp_path, "m3-c-atomic.db")
    command.upgrade(config, M2_HEAD)
    engine = _new_engine(url, foreign_keys=True)
    try:
        ids = _seed_graph(engine)
    finally:
        engine.dispose()
    command.upgrade(config, m3_revision)

    engine = _new_engine(url, foreign_keys=True)
    try:
        runs = _tables(engine, "scoring_runs")["scoring_runs"]
        identity = _core_identity(ids)

        # Any isolated M3 identity value is invalid, including an orphaned
        # DocumentSnapshot hash with no content-addressed reference.
        for field in CORE_REQUIRED_FIELDS:
            _assert_integrity_error(
                engine,
                update(runs).where(runs.c.id == ids["run_id"]).values(**{field: identity[field]}),
            )

        with engine.begin() as connection:
            connection.execute(
                update(runs).where(runs.c.id == ids["run_id"]).values(**identity)
            )

        for field in CORE_REQUIRED_FIELDS:
            _assert_integrity_error(
                engine,
                update(runs).where(runs.c.id == ids["run_id"]).values(**{field: null()}),
            )
        # The older 0012 policy check independently permits all three values
        # to be NULL for history.  M3 must close that branch whenever a Core
        # identity is present.
        _assert_integrity_error(
            engine,
            update(runs)
            .where(runs.c.id == ids["run_id"])
            .values(
                policy_snapshot=null(),
                policy_hash=null(),
                policy_schema_version=null(),
            ),
        )
        for field in CORE_DIGEST_FIELDS:
            _assert_integrity_error(
                engine,
                update(runs)
                .where(runs.c.id == ids["run_id"])
                .values(**{field: "G" * 64}),
            )
        for field in CORE_NONEMPTY_TEXT_FIELDS:
            _assert_integrity_error(
                engine,
                update(runs)
                .where(runs.c.id == ids["run_id"])
                .values(**{field: ""}),
            )
        _assert_integrity_error(
            engine,
            update(runs)
            .where(runs.c.id == ids["run_id"])
            .values(rescore_generation=-1),
        )
        _assert_integrity_error(
            engine,
            update(runs)
            .where(runs.c.id == ids["run_id"])
            .values(rubric_source_kind="invented_source"),
        )
    finally:
        engine.dispose()


def test_m3_d_published_and_legacy_rubric_identity_are_mutually_exclusive(
    monkeypatch,
    tmp_path,
    m3_revision,
):
    url, config = _database(monkeypatch, tmp_path, "m3-d-source-kind.db")
    command.upgrade(config, M2_HEAD)
    engine = _new_engine(url, foreign_keys=True)
    try:
        ids = _seed_graph(engine)
    finally:
        engine.dispose()
    command.upgrade(config, m3_revision)

    engine = _new_engine(url, foreign_keys=True)
    try:
        runs = _tables(engine, "scoring_runs")["scoring_runs"]
        published = _core_identity(ids, published=True)
        for field in ("rubric_version_id", "rubric_version_hash", "rubric_hash_scheme"):
            bad = dict(published)
            bad[field] = None
            _assert_integrity_error(
                engine,
                update(runs).where(runs.c.id == ids["run_id"]).values(**bad),
            )
        bad = dict(published)
        bad["rubric_version_hash"] = "9" * 64
        _assert_integrity_error(
            engine,
            update(runs).where(runs.c.id == ids["run_id"]).values(**bad),
        )
        bad = dict(published)
        bad["rubric_hash_scheme"] = "rubric-content-v2"
        _assert_integrity_error(
            engine,
            update(runs).where(runs.c.id == ids["run_id"]).values(**bad),
        )
        with engine.begin() as connection:
            connection.execute(
                update(runs).where(runs.c.id == ids["run_id"]).values(**published)
            )

        for formal_field, value in (
            ("rubric_version_id", ids["version_id"]),
            ("rubric_version_hash", ids["version_hash"]),
            ("rubric_hash_scheme", "rubric-content-v1"),
        ):
            bad = _core_identity(ids, published=False, idempotency_key="4" * 64)
            bad[formal_field] = value
            _assert_integrity_error(
                engine,
                update(runs).where(runs.c.id == ids["run_id"]).values(**bad),
            )
    finally:
        engine.dispose()


def test_m3_e_batch_and_run_version_references_must_belong_to_the_same_rubric(
    monkeypatch,
    tmp_path,
    m3_revision,
):
    url, config = _database(monkeypatch, tmp_path, "m3-e-same-rubric.db")
    command.upgrade(config, M2_HEAD)
    engine = _new_engine(url, foreign_keys=True)
    try:
        ids = _seed_graph(engine)
    finally:
        engine.dispose()
    command.upgrade(config, m3_revision)

    engine = _new_engine(url, foreign_keys=True)
    try:
        tables = _tables(engine, "grading_batches", "scoring_runs")
        with engine.begin() as connection:
            connection.execute(
                update(tables["grading_batches"])
                .where(tables["grading_batches"].c.id == ids["batch_id"])
                .values(rubric_version_id=ids["version_id"])
            )
            connection.execute(
                update(tables["scoring_runs"])
                .where(tables["scoring_runs"].c.id == ids["run_id"])
                .values(**_core_identity(ids, published=True))
            )

        _assert_integrity_error(
            engine,
            update(tables["grading_batches"])
            .where(tables["grading_batches"].c.id == ids["batch_id"])
            .values(rubric_version_id=ids["other_version_id"]),
        )
        cross_identity = _core_identity(
            ids, published=True, idempotency_key="5" * 64
        )
        cross_identity.update(
            rubric_version_id=ids["other_version_id"],
            rubric_version_hash=ids["other_version_hash"],
        )
        _assert_integrity_error(
            engine,
            update(tables["scoring_runs"])
            .where(tables["scoring_runs"].c.id == ids["run_id"])
            .values(**cross_identity),
        )
    finally:
        engine.dispose()


def test_m3_f_database_uniqueness_arbitrates_idempotency_and_rescore_generation(
    monkeypatch,
    tmp_path,
    m3_revision,
):
    url, config = _database(monkeypatch, tmp_path, "m3-f-idempotency.db")
    command.upgrade(config, M2_HEAD)
    engine = _new_engine(url, foreign_keys=True)
    try:
        ids = _seed_graph(engine, two_runs=True)
    finally:
        engine.dispose()
    command.upgrade(config, m3_revision)

    engine = _new_engine(url, foreign_keys=True)
    try:
        runs = _tables(engine, "scoring_runs")["scoring_runs"]
        first = _core_identity(ids, generation=0, idempotency_key="6" * 64)
        with engine.connect() as connection:
            template = dict(
                connection.execute(
                    select(runs).where(runs.c.id == ids["run_id"])
                ).mappings().one()
            )

        # Race two independent INSERT transactions.  A sequential UPDATE only
        # proves uniqueness in the easy path and can hide an implementation
        # that performs a preflight SELECT instead of relying on DB arbitration.
        barrier = Barrier(2)

        def competing_insert(run_id):
            values = dict(template)
            values["id"] = run_id
            values.update(first)
            barrier.wait()
            try:
                with engine.begin() as connection:
                    connection.execute(runs.insert().values(**values))
            except IntegrityError:
                return "conflict"
            return "committed"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(
                executor.map(
                    competing_insert,
                    ("m3-race-run-one", "m3-race-run-two"),
                )
            )
        assert sorted(outcomes) == ["committed", "conflict"]

        rescore_values = dict(template)
        rescore_values["id"] = "m3-rescore-run"
        rescore_values.update(
            _core_identity(ids, generation=1, idempotency_key="7" * 64)
        )
        with engine.begin() as connection:
            connection.execute(runs.insert().values(**rescore_values))
        with engine.connect() as connection:
            rows = connection.execute(
                select(runs.c.rescore_generation, runs.c.idempotency_key)
                .where(runs.c.idempotency_key.is_not(None))
                .order_by(runs.c.idempotency_key)
            ).all()
        assert rows == [(0, "6" * 64), (1, "7" * 64)]
    finally:
        engine.dispose()


def test_m3_g_rule_results_snapshot_is_all_or_none_and_history_remains_nullable(
    monkeypatch,
    tmp_path,
    m3_revision,
):
    url, config = _database(monkeypatch, tmp_path, "m3-g-rule-results.db")
    command.upgrade(config, M2_HEAD)
    engine = _new_engine(url, foreign_keys=True)
    try:
        ids = _seed_graph(engine)
    finally:
        engine.dispose()
    command.upgrade(config, m3_revision)

    engine = _new_engine(url, foreign_keys=True)
    try:
        items = _tables(engine, "score_items")["score_items"]
        with engine.connect() as connection:
            assert connection.execute(
                select(items.c.rule_results, items.c.rule_results_schema_version).where(
                    items.c.id == ids["item_id"]
                )
            ).one() == (None, None)
        _assert_integrity_error(
            engine,
            update(items)
            .where(items.c.id == ids["item_id"])
            .values(rule_results=[]),
        )
        _assert_integrity_error(
            engine,
            update(items)
            .where(items.c.id == ids["item_id"])
            .values(rule_results_schema_version="rule-results@1"),
        )
        _assert_integrity_error(
            engine,
            update(items)
            .where(items.c.id == ids["item_id"])
            .values(rule_results=[], rule_results_schema_version=""),
        )
        with engine.begin() as connection:
            connection.execute(
                update(items)
                .where(items.c.id == ids["item_id"])
                .values(
                    rule_results=[{"rule_code": "M3-ONE", "status": "applied"}],
                    rule_results_schema_version="rule-results@1",
                )
            )
    finally:
        engine.dispose()


def test_m3_h_version_references_use_restrict_delete_semantics(
    monkeypatch,
    tmp_path,
    m3_revision,
):
    url, config = _database(monkeypatch, tmp_path, "m3-h-restrict.db")
    command.upgrade(config, M2_HEAD)
    engine = _new_engine(url, foreign_keys=True)
    try:
        ids = _seed_graph(engine)
    finally:
        engine.dispose()
    command.upgrade(config, m3_revision)

    engine = _new_engine(url, foreign_keys=True)
    try:
        tables = _tables(
            engine, "grading_batches", "rubric_versions", "scoring_runs"
        )
        with engine.begin() as connection:
            connection.execute(
                update(tables["grading_batches"])
                .where(tables["grading_batches"].c.id == ids["batch_id"])
                .values(rubric_version_id=ids["version_id"])
            )
        _assert_integrity_error(
            engine,
            delete(tables["rubric_versions"]).where(
                tables["rubric_versions"].c.id == ids["version_id"]
            ),
        )
        # Exercise the run reference independently; reflection alone is not
        # enough because SQLite only enforces FKs when PRAGMA is enabled.
        with engine.begin() as connection:
            connection.execute(
                update(tables["grading_batches"])
                .where(tables["grading_batches"].c.id == ids["batch_id"])
                .values(rubric_version_id=None)
            )
            connection.execute(
                update(tables["scoring_runs"])
                .where(tables["scoring_runs"].c.id == ids["run_id"])
                .values(**_core_identity(ids, published=True))
            )
        _assert_integrity_error(
            engine,
            delete(tables["rubric_versions"]).where(
                tables["rubric_versions"].c.id == ids["version_id"]
            ),
        )

        version_fks_by_table = {
            table_name: [
                item
                for item in inspect(engine).get_foreign_keys(table_name)
                if "rubric_version_id" in item["constrained_columns"]
            ]
            for table_name in ("grading_batches", "scoring_runs")
        }
        assert all(version_fks_by_table.values())
        assert all(
            (item.get("options") or {}).get("ondelete") == "RESTRICT"
            for items in version_fks_by_table.values()
            for item in items
        )
    finally:
        engine.dispose()


def test_m3_i_empty_and_backfill_only_databases_downgrade_losslessly_and_replay(
    monkeypatch,
    tmp_path,
    m3_revision,
):
    # Empty database boundary.
    empty_url, empty_config = _database(monkeypatch, tmp_path, "m3-i-empty.db")
    command.upgrade(empty_config, m3_revision)
    command.downgrade(empty_config, M2_HEAD)
    empty_engine = _new_engine(empty_url)
    try:
        assert _current_revisions(empty_engine) == {M2_HEAD}
        assert "business_profile_key" not in {
            item["name"] for item in inspect(empty_engine).get_columns("rubric_versions")
        }
    finally:
        empty_engine.dispose()
    command.upgrade(empty_config, m3_revision)

    # A database containing only migration backfills remains representable by
    # 0012 and may also cross the boundary in both directions.
    url, config = _database(monkeypatch, tmp_path, "m3-i-backfill.db")
    command.upgrade(config, M2_HEAD)
    engine = _new_engine(url, foreign_keys=True)
    try:
        ids = _seed_graph(engine)
        before = _full_rows(
            engine,
            "rubrics",
            "rubric_compilations",
            "rubric_versions",
            "grading_batches",
            "scoring_runs",
            "score_items",
        )
        before_schema = _schema_fingerprint(
            engine,
            "rubric_versions",
            "grading_batches",
            "scoring_runs",
            "score_items",
        )
    finally:
        engine.dispose()
    command.upgrade(config, m3_revision)
    command.downgrade(config, M2_HEAD)
    engine = _new_engine(url, foreign_keys=True)
    try:
        assert _current_revisions(engine) == {M2_HEAD}
        assert _full_rows(
            engine,
            "rubrics",
            "rubric_compilations",
            "rubric_versions",
            "grading_batches",
            "scoring_runs",
            "score_items",
        ) == before
        assert _schema_fingerprint(
            engine,
            "rubric_versions",
            "grading_batches",
            "scoring_runs",
            "score_items",
        ) == before_schema
    finally:
        engine.dispose()
    command.upgrade(config, m3_revision)
    engine = _new_engine(url)
    try:
        versions = _tables(engine, "rubric_versions")["rubric_versions"]
        with engine.connect() as connection:
            row = connection.execute(
                select(versions.c.business_profile_key, versions.c.hash_scheme).where(
                    versions.c.id == ids["version_id"]
                )
            ).one()
        assert row == ("thesis", "rubric-content-v1")
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "scenario",
    (
        "new_profile",
        "v2_hash_scheme",
        "batch_version_pin",
        "core_run_identity",
        "rule_results",
    ),
)
def test_m3_j_downgrade_rejects_every_unrepresentable_state_before_ddl(
    monkeypatch,
    tmp_path,
    m3_revision,
    scenario,
):
    url, config = _database(monkeypatch, tmp_path, f"m3-j-{scenario}.db")
    command.upgrade(config, M2_HEAD)
    engine = _new_engine(url, foreign_keys=True)
    try:
        ids = _seed_graph(engine)
    finally:
        engine.dispose()
    command.upgrade(config, m3_revision)

    engine = _new_engine(url, foreign_keys=True)
    tables = _tables(
        engine,
        "rubric_versions",
        "grading_batches",
        "scoring_runs",
        "score_items",
    )
    try:
        with engine.begin() as connection:
            if scenario == "new_profile":
                # The second rubric/version has no batch, paper, run or score
                # item.  This is genuinely published-but-never-scored data;
                # using version_id here would only retest a historical run.
                connection.execute(
                    update(tables["rubric_versions"])
                    .where(
                        tables["rubric_versions"].c.id
                        == ids["other_version_id"]
                    )
                    .values(business_profile_key="technical_proposal")
                )
            elif scenario == "v2_hash_scheme":
                connection.execute(
                    update(tables["rubric_versions"])
                    .where(
                        tables["rubric_versions"].c.id
                        == ids["other_version_id"]
                    )
                    .values(hash_scheme="rubric-content-v2")
                )
            elif scenario == "batch_version_pin":
                connection.execute(
                    update(tables["grading_batches"])
                    .where(tables["grading_batches"].c.id == ids["batch_id"])
                    .values(rubric_version_id=ids["version_id"])
                )
            elif scenario == "core_run_identity":
                connection.execute(
                    update(tables["scoring_runs"])
                    .where(tables["scoring_runs"].c.id == ids["run_id"])
                    .values(**_core_identity(ids))
                )
            else:
                connection.execute(
                    update(tables["score_items"])
                    .where(tables["score_items"].c.id == ids["item_id"])
                    .values(
                        rule_results=[{"rule_code": "M3-ONE", "status": "applied"}],
                        rule_results_schema_version="rule-results@1",
                    )
                )
        before_rows = _full_rows(
            engine,
            "rubric_versions",
            "grading_batches",
            "scoring_runs",
            "score_items",
        )
        before_schema = _schema_fingerprint(
            engine, "rubric_versions", "grading_batches", "scoring_runs", "score_items"
        )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError) as exc_info:
        command.downgrade(config, M2_HEAD)
    message = f"{type(exc_info.value).__name__}: {exc_info.value}".lower()
    assert any(token in message for token in ("export", "导出", "downgrade", "降级"))

    engine = _new_engine(url, foreign_keys=True)
    try:
        assert _current_revisions(engine) == {m3_revision}
        assert _schema_fingerprint(
            engine, "rubric_versions", "grading_batches", "scoring_runs", "score_items"
        ) == before_schema
        assert _full_rows(
            engine,
            "rubric_versions",
            "grading_batches",
            "scoring_runs",
            "score_items",
        ) == before_rows
    finally:
        engine.dispose()


_FROZEN_V1_EMPTY_GRAPH_HASH = (
    "8ce648dcef2b0e715a9f7b84ccf8e2e7cd1cdf81b0af503d677c287f55c92eb4"
)


def _empty_graph_payload(*, business_profile_key=None):
    payload = {
        "rubric": {
            "total_score": "100.00",
            "description": None,
            "format_spec": {},
        },
        "criteria": [],
        "compilation": {
            "parser_version": "parser@1",
            "compiler_version": "compiler@1",
            "model_provider": None,
            "model_name": None,
            "sampling_params": {},
            "prompt_version": "prompt@1",
            "raw_parse_output": {},
            "raw_model_output": {},
            "validation_result": {"valid": True},
            "blockers": [],
            "warnings": [],
        },
        "artifacts": [],
        "version": {
            "workflow_profile": "thesis",
            "global_policy": {"rounding": {"mode": "half_up", "digits": 2}},
        },
        "rules": [],
    }
    if business_profile_key is not None:
        payload["version"]["business_profile_key"] = business_profile_key
    return payload


def _payload_hash(payload):
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _v1_empty_graph_hash():
    """Independent frozen pre-0013 projection for one deliberately small graph."""

    digest = _payload_hash(_empty_graph_payload())
    assert digest == _FROZEN_V1_EMPTY_GRAPH_HASH
    return digest


def _v2_empty_graph_hash(business_profile_key):
    """Frozen v2 projection: v1 graph plus the explicit profile field only."""

    return _payload_hash(
        _empty_graph_payload(business_profile_key=business_profile_key)
    )


def _database_canonical_value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {
            str(key): _database_canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_database_canonical_value(item) for item in value]
    return value


def _independent_v1_hash_from_database(engine):
    """Rebuild the 0011 whitelist from reflected rows, never stored hashes."""

    tables = _tables(
        engine,
        "rubrics",
        "rubric_criteria",
        "rubric_compilations",
        "source_artifacts",
        "rubric_versions",
        "atomic_rules",
    )
    with engine.connect() as connection:
        rubric = connection.execute(
            select(tables["rubrics"]).where(
                tables["rubrics"].c.id == "m3-hash-rubric"
            )
        ).mappings().one()
        compilation = connection.execute(
            select(tables["rubric_compilations"]).where(
                tables["rubric_compilations"].c.id == "m3-hash-compilation"
            )
        ).mappings().one()
        version = connection.execute(
            select(tables["rubric_versions"]).where(
                tables["rubric_versions"].c.id == "m3-hash-version"
            )
        ).mappings().one()
        criteria = connection.execute(
            select(tables["rubric_criteria"].c.id).where(
                tables["rubric_criteria"].c.rubric_id == "m3-hash-rubric"
            )
        ).all()
        artifacts = connection.execute(
            select(tables["source_artifacts"].c.id).where(
                tables["source_artifacts"].c.compilation_id
                == "m3-hash-compilation"
            )
        ).all()
        rules = connection.execute(
            select(tables["atomic_rules"].c.id).where(
                tables["atomic_rules"].c.rubric_version_id
                == "m3-hash-version"
            )
        ).all()
    assert criteria == []
    assert artifacts == []
    assert rules == []
    payload = {
        "rubric": {
            "total_score": _database_canonical_value(rubric["total_score"]),
            "description": rubric["description"],
            "format_spec": _database_canonical_value(rubric["format_spec"]),
        },
        "criteria": [],
        "compilation": {
            name: _database_canonical_value(compilation[name])
            for name in (
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
            )
        },
        "artifacts": [],
        "version": {
            "workflow_profile": version["workflow_profile"],
            "global_policy": _database_canonical_value(version["global_policy"]),
        },
        "rules": [],
    }
    return _payload_hash(payload)


def _seed_hash_fixture(engine, version_hash):
    tables = _tables(engine, "users", "rubrics", "rubric_compilations", "rubric_versions")
    now = datetime(2026, 7, 19, 12, 0, 0)
    with engine.begin() as connection:
        connection.execute(
            tables["users"].insert().values(
                id="m3-hash-user",
                username="m3-hash-user",
                display_name="M3 hash user",
                role="admin",
                department=None,
                password_hash=None,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            tables["rubrics"].insert().values(
                id="m3-hash-rubric",
                owner_id="m3-hash-user",
                name="M3 hash fixture",
                version="v1",
                total_score=100,
                status="published",
                description=None,
                format_spec={},
                created_by="m3-hash-user",
                created_at=now,
                published_at=now,
            )
        )
        connection.execute(
            tables["rubric_compilations"].insert().values(
                id="m3-hash-compilation",
                rubric_id="m3-hash-rubric",
                status="published",
                parser_version="parser@1",
                compiler_version="compiler@1",
                model_provider=None,
                model_name=None,
                sampling_params={},
                prompt_version="prompt@1",
                raw_parse_output={},
                raw_model_output={},
                validation_result={"valid": True},
                blockers=[],
                warnings=[],
                human_changes=[],
                created_by="m3-hash-user",
                reviewed_by="m3-hash-user",
                reviewed_at=now,
                published_at=now,
                final_version_hash=version_hash,
                created_at=now,
            )
        )
        connection.execute(
            tables["rubric_versions"].insert().values(
                id="m3-hash-version",
                rubric_id="m3-hash-rubric",
                compilation_id="m3-hash-compilation",
                version="v1",
                workflow_profile="thesis",
                global_policy={"rounding": {"mode": "half_up", "digits": 2}},
                version_hash=version_hash,
                created_by="m3-hash-user",
                created_at=now,
            )
        )


def test_m3_k_v1_hash_is_byte_stable_across_0011_0012_0013_and_v2_is_profile_sensitive(
    monkeypatch,
    tmp_path,
    m3_revision,
):
    url, config = _database(monkeypatch, tmp_path, "m3-k-hash-schemes.db")
    expected_v1 = _v1_empty_graph_hash()
    command.upgrade(config, "0011_version_hash_on_update")
    engine = _new_engine(url, foreign_keys=True)
    try:
        _seed_hash_fixture(engine, expected_v1)
        versions = _tables(engine, "rubric_versions")["rubric_versions"]
        with engine.connect() as connection:
            assert connection.scalar(select(versions.c.version_hash)) == expected_v1
        assert _independent_v1_hash_from_database(engine) == expected_v1
    finally:
        engine.dispose()

    command.upgrade(config, M2_HEAD)
    engine = _new_engine(url)
    try:
        versions = _tables(engine, "rubric_versions")["rubric_versions"]
        with engine.connect() as connection:
            assert connection.scalar(select(versions.c.version_hash)) == expected_v1
        assert _independent_v1_hash_from_database(engine) == expected_v1
    finally:
        engine.dispose()

    command.upgrade(config, m3_revision)
    engine = _new_engine(url, foreign_keys=True)
    try:
        versions = _tables(engine, "rubric_versions")["rubric_versions"]
        with engine.connect() as connection:
            stored = connection.execute(select(versions)).mappings().one()
        assert stored["version_hash"] == expected_v1
        assert stored["hash_scheme"] == "rubric-content-v1"
        assert stored["business_profile_key"] == "thesis"
        assert _independent_v1_hash_from_database(engine) == expected_v1

        # Exercise the production graph hasher, but keep the expected v1 value
        # independently frozen above so implementation code is not its own oracle.
        from backend.app.db import models
        from backend.app.services.rubrics import lifecycle

        db = sessionmaker(bind=engine, autoflush=False)()
        try:
            rubric = db.get(models.Rubric, "m3-hash-rubric")
            compilation = db.get(models.RubricCompilation, "m3-hash-compilation")
            version = db.get(models.RubricVersion, "m3-hash-version")
            assert lifecycle._canonical_version_hash(
                db, rubric, compilation, version
            ) == expected_v1

            version.business_profile_key = "technical_proposal"
            assert lifecycle._canonical_version_hash(
                db, rubric, compilation, version
            ) == expected_v1

            version.hash_scheme = "rubric-content-v2"
            version.business_profile_key = "thesis"
            v2_thesis = lifecycle._canonical_version_hash(db, rubric, compilation, version)
            version.business_profile_key = "technical_proposal"
            v2_technical = lifecycle._canonical_version_hash(
                db, rubric, compilation, version
            )
            assert v2_thesis == _v2_empty_graph_hash("thesis")
            assert v2_technical == _v2_empty_graph_hash("technical_proposal")
            assert v2_thesis != v2_technical
        finally:
            db.close()
    finally:
        engine.dispose()


def test_m3_l_0013_is_on_head_lineage_and_retains_existing_foreign_keys(
    monkeypatch,
    tmp_path,
    m3_revision,
):
    url, config = _database(monkeypatch, tmp_path, "m3-l-lineage.db")
    command.upgrade(config, m3_revision)
    script = ScriptDirectory.from_config(config)
    assert any(
        m3_revision
        in {revision.revision for revision in script.iterate_revisions(head, "base")}
        for head in script.get_heads()
    )

    engine = _new_engine(url, foreign_keys=True)
    try:
        assert _current_revisions(engine) == {m3_revision}

        def fk_pairs(table_name):
            return {
                (
                    tuple(item["constrained_columns"]),
                    item["referred_table"],
                    tuple(item["referred_columns"]),
                )
                for item in inspect(engine).get_foreign_keys(table_name)
            }

        assert {
            (("paper_id",), "papers", ("id",)),
            (("rubric_id",), "rubrics", ("id",)),
        }.issubset(fk_pairs("scoring_runs"))
        assert {
            (("scoring_run_id",), "scoring_runs", ("id",)),
            (("criterion_id",), "rubric_criteria", ("id",)),
        }.issubset(fk_pairs("score_items"))
        assert {
            (
                ("compilation_id", "rubric_id"),
                "rubric_compilations",
                ("id", "rubric_id"),
            ),
            (
                ("compilation_id", "version_hash"),
                "rubric_compilations",
                ("id", "final_version_hash"),
            ),
        }.issubset(fk_pairs("rubric_versions"))
        assert (("rubric_id",), "rubrics", ("id",)) in fk_pairs(
            "grading_batches"
        )
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
    finally:
        engine.dispose()
