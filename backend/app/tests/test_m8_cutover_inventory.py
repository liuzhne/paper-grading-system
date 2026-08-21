from copy import deepcopy
import json

from sqlalchemy import select
from sqlalchemy import func
from sqlalchemy import update
from typer.testing import CliRunner

from backend.app.cli import db as cli_db
from backend.app.cli import main as cli_main
from backend.app.cli.main import app
from backend.app.core.config import settings
from backend.app.db import models
from backend.app.services.deployment.cutover_inventory import (
    build_core_cutover_inventory,
)
from backend.app.services.dev_user import ensure_dev_user
from backend.app.tests.test_m7_technical_proposal_e2e import _create_batch
from backend.app.tests.test_m7_technical_proposal_e2e import _publish_fixture


def _legacy_batch(client, *, executable=True):
    with client.session_factory() as db:
        user = ensure_dev_user(db)
        rubric = models.Rubric(
            name=(
                "Executable legacy rubric"
                if executable
                else "Unmigratable legacy rubric"
            ),
            version="legacy-v1",
            total_score=100,
            status="published",
            published_at=models.utcnow(),
            created_by=user.id,
            owner_id=user.id,
        )
        rubric.criteria.append(
            models.RubricCriterion(
                code="LEGACY_DIRECT",
                name="Legacy direct criterion",
                max_score=100,
                criterion_type="llm_judgment" if executable else "hybrid",
                scoring_mode="llm_direct" if executable else "hybrid",
                sub_checks=[] if not executable else None,
                applies_to="global",
                display_order=0,
            )
        )
        db.add(rubric)
        db.flush()
        batch = models.GradingBatch(
            name="Legacy active batch",
            rubric_id=rubric.id,
            rubric_version_id=None,
            status="draft",
            owner_id=user.id,
            created_by=user.id,
        )
        db.add(batch)
        db.commit()
        return rubric.id, batch.id


def _target(report, rubric_id):
    return next(
        item for item in report["targets"] if item["rubric_id"] == rubric_id
    )


def test_empty_inventory_is_clear_but_never_grants_production_release(client):
    with client.session_factory() as db:
        before = (len(db.new), len(db.dirty), len(db.deleted))
        report = build_core_cutover_inventory(db)
        after = (len(db.new), len(db.dirty), len(db.deleted))

    assert report == {
        "schema_version": "core-cutover-inventory@1",
        "authorization_scope": "rubric_inventory_only",
        "summary": {
            "active_grading_batches": 0,
            "active_evaluation_batches": 0,
            "targets": 0,
            "ready_targets": 0,
            "blocked_targets": 0,
            "blockers": 0,
        },
        "targets": [],
        "blockers": [],
        "inventory_clear": True,
        "cutover_authorized": True,
        "production_default_switch_authorized": False,
    }
    assert before == after == (0, 0, 0)


def test_published_active_target_recomputes_hash_and_builds_exact_plan(client):
    rubric_id, version_id = _publish_fixture(client)
    batch = _create_batch(client, version_id)
    with client.session_factory() as db:
        before_counts = {
            table: db.scalar(select(func.count()).select_from(model))
            for table, model in (
                ("rubrics", models.Rubric),
                ("versions", models.RubricVersion),
                ("batches", models.EvaluationBatch),
            )
        }
        report = build_core_cutover_inventory(db)
        after_counts = {
            table: db.scalar(select(func.count()).select_from(model))
            for table, model in (
                ("rubrics", models.Rubric),
                ("versions", models.RubricVersion),
                ("batches", models.EvaluationBatch),
            )
        }

    assert before_counts == after_counts
    assert report["inventory_clear"] is True
    assert report["production_default_switch_authorized"] is False
    assert report["blockers"] == []
    assert report["summary"] == {
        "active_grading_batches": 0,
        "active_evaluation_batches": 1,
        "targets": 1,
        "ready_targets": 1,
        "blocked_targets": 0,
        "blockers": 0,
    }
    target = _target(report, rubric_id)
    assert target["status"] == "ready"
    assert target["rubric_source_kind"] == "published_version"
    assert target["rubric_version_id"] == version_id
    assert target["affected_batches"] == [
        {
            "batch_kind": "evaluation_batch",
            "batch_id": batch["id"],
            "status": "active",
            "owner_id": target["owner_ids"][0],
        }
    ]
    assert target["identity"]["business_profile_key"] == "technical_proposal"
    assert target["identity"]["business_profile_version"] == (
        "technical-proposal-profile@1"
    )
    assert target["identity"]["rubric_version_hash"]
    assert target["identity"]["rubric_snapshot_hash"]
    assert target["identity"]["policy_hash"]
    assert target["identity"]["plan_hash"]
    assert target["identity"]["plan_schema_version"] == "rule-execution-plan@2"
    assert set(target["identity"]["checker_manifest"]) == {
        "core.required_sections.v1",
        "core.text_length_range.v1",
        "generic.hybrid.required.v1",
    }


def test_published_graph_tampering_is_a_stable_fail_closed_blocker(client):
    rubric_id, version_id = _publish_fixture(client)
    batch = _create_batch(client, version_id)
    with client.session_factory() as db:
        rule_id = db.scalar(
            select(models.AtomicRule.id).where(
                models.AtomicRule.rubric_version_id == version_id
            )
        )
        db.execute(
            update(models.AtomicRule)
            .where(models.AtomicRule.id == rule_id)
            .values(rule_text="tampered after publication")
        )
        db.commit()
        first = build_core_cutover_inventory(db)
        second = build_core_cutover_inventory(db)

    assert first == second
    assert first["inventory_clear"] is False
    assert first["cutover_authorized"] is False
    assert first["production_default_switch_authorized"] is False
    assert first["summary"]["blockers"] == 1
    blocker = first["blockers"][0]
    assert blocker == {
        "code": "PUBLISHED_VERSION_INVALID",
        "rubric_id": rubric_id,
        "rubric_version_id": version_id,
        "affected_batch_ids": [batch["id"]],
        "owner_ids": _target(first, rubric_id)["owner_ids"],
        "message": "published rubric version failed immutable provenance validation",
        "remediation": (
            "deep-clone the rubric, repair/review its provenance graph, publish "
            "a new immutable version, then repin affected batches"
        ),
    }


def test_legacy_direct_builds_compatibility_plan_but_invalid_hybrid_blocks(client):
    valid_rubric_id, valid_batch_id = _legacy_batch(client, executable=True)
    invalid_rubric_id, invalid_batch_id = _legacy_batch(
        client,
        executable=False,
    )
    with client.session_factory() as db:
        report = build_core_cutover_inventory(db)

    assert report["inventory_clear"] is False
    valid = _target(report, valid_rubric_id)
    invalid = _target(report, invalid_rubric_id)
    assert valid["status"] == "ready"
    assert valid["rubric_source_kind"] == "legacy_unversioned"
    assert valid["affected_batches"][0]["batch_id"] == valid_batch_id
    assert valid["identity"]["business_profile_key"] == "thesis"
    assert valid["identity"]["plan_schema_version"] == "rule-execution-plan@3"
    assert valid["identity"]["compatibility_node_kinds"] == [
        "legacy_direct_criterion"
    ]
    assert invalid["status"] == "blocked"
    assert invalid["affected_batches"][0]["batch_id"] == invalid_batch_id
    assert report["blockers"] == [
        {
            "code": "LEGACY_NOT_EXECUTABLE",
            "rubric_id": invalid_rubric_id,
            "rubric_version_id": None,
            "affected_batch_ids": [invalid_batch_id],
            "owner_ids": invalid["owner_ids"],
            "message": "legacy rubric cannot be represented by approved compatibility nodes",
            "remediation": (
                "migrate the rubric through import/review/publish or repair its "
                "explicit direct/composite compatibility definition"
            ),
        }
    ]


def test_inventory_sorting_is_independent_of_batch_insertion_order(client):
    rubric_id, first_batch_id = _legacy_batch(client, executable=True)
    with client.session_factory() as db:
        rubric = db.get(models.Rubric, rubric_id)
        first = db.get(models.GradingBatch, first_batch_id)
        second = models.GradingBatch(
            name="Earlier lexicographic identity",
            rubric_id=rubric.id,
            rubric_version_id=None,
            status="scoring",
            owner_id=first.owner_id,
            created_by=first.created_by,
        )
        second.id = "00000000-0000-0000-0000-000000000001"
        db.add(second)
        db.commit()
        report = build_core_cutover_inventory(db)

    target = _target(report, rubric_id)
    assert [item["batch_id"] for item in target["affected_batches"]] == sorted(
        [first_batch_id, second.id]
    )
    assert report["targets"] == sorted(
        deepcopy(report["targets"]),
        key=lambda item: (
            item["rubric_id"],
            item["rubric_version_id"] or "",
        ),
    )


def test_cli_cutover_audit_uses_exit_code_as_a_deployment_gate(tmp_path):
    runner = CliRunner()
    database = tmp_path / "inventory.db"
    storage = tmp_path / "storage"
    args = ["--db", str(database), "--storage", str(storage)]
    saved = (settings.DATABASE_URL, settings.STORAGE_ROOT)
    try:
        initialized = runner.invoke(app, ["init", *args])
        assert initialized.exit_code == 0, initialized.output
        clear = runner.invoke(app, ["core-cutover-audit", "--json", *args])
        assert clear.exit_code == 0, clear.output
        assert json.loads(clear.output)["inventory_clear"] is True

        cli_main._bootstrap(database, storage)
        with cli_db.cli_session() as db:
            user = ensure_dev_user(db)
            rubric = models.Rubric(
                name="CLI invalid legacy rubric",
                version="legacy-v1",
                total_score=100,
                status="published",
                published_at=models.utcnow(),
                created_by=user.id,
                owner_id=user.id,
            )
            rubric.criteria.append(
                models.RubricCriterion(
                    code="BROKEN",
                    name="Broken hybrid",
                    max_score=100,
                    criterion_type="hybrid",
                    scoring_mode="hybrid",
                    sub_checks=[],
                    applies_to="global",
                    display_order=0,
                )
            )
            db.add(rubric)
            db.flush()
            db.add(
                models.GradingBatch(
                    name="CLI blocked batch",
                    rubric_id=rubric.id,
                    status="draft",
                    owner_id=user.id,
                    created_by=user.id,
                )
            )
            db.commit()

        blocked = runner.invoke(
            app,
            ["core-cutover-audit", "--json", *args],
        )
        assert blocked.exit_code == 1, blocked.output
        blocked_report = json.loads(blocked.output)
        assert blocked_report["inventory_clear"] is False
        assert blocked_report["blockers"][0]["code"] == (
            "LEGACY_NOT_EXECUTABLE"
        )
    finally:
        settings.DATABASE_URL, settings.STORAGE_ROOT = saved
