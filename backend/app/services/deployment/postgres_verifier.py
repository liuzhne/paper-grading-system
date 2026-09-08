"""PostgreSQL-only M8 constraint and deterministic-order verification."""

from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.app.core.config import settings
from backend.app.db import models
from backend.app.services.batch_scoring.jobs import create_batch_scoring_job
from backend.app.services.dev_user import ensure_dev_user


MIGRATION_SEQUENCE = (
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
)
EXPECTED_HEAD = MIGRATION_SEQUENCE[-1]
ACTIVE_JOB_INDEX = "ix_batch_scoring_jobs_one_active_per_batch"


def stable_ordering_clause():
    return ("created_at", "id")


def verify_postgres(session):
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("Postgres operations verification requires postgresql")
    head = session.scalar(text("SELECT version_num FROM alembic_version"))
    if head != EXPECTED_HEAD:
        raise RuntimeError(
            "unexpected alembic head: expected %s, got %s" % (EXPECTED_HEAD, head)
        )
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    required_tables = {
        "rubric_versions",
        "release_gate_profiles",
        "evaluation_batches",
        "document_snapshots",
        "batch_scoring_jobs",
        "batch_scoring_items",
        "organizations",
        "organization_members",
        "ai_connections",
        "ai_usage_ledger",
        "rule_scoring_tasks",
        "manual_review_tasks",
    }
    missing = sorted(required_tables - tables)
    if missing:
        raise RuntimeError("missing required tables: %s" % ", ".join(missing))
    indexes = {
        value["name"]: value
        for value in inspector.get_indexes("batch_scoring_jobs")
    }
    active_index = indexes.get(ACTIVE_JOB_INDEX)
    if active_index is None or not active_index.get("unique"):
        raise RuntimeError("active batch job partial unique index is missing")
    predicate_value = (active_index.get("dialect_options") or {}).get(
        "postgresql_where"
    )
    predicate = "" if predicate_value is None else str(predicate_value)
    if "queued" not in predicate or "running" not in predicate:
        raise RuntimeError("active batch job index predicate is incomplete")
    rubric_indexes = {
        value["name"]: value for value in inspector.get_indexes("rubrics")
    }
    for index_name, visibility in (
        ("uq_rubrics_system_name_version", "system"),
        ("uq_rubrics_organization_name_version", "organization"),
        ("uq_rubrics_private_name_version", "private"),
    ):
        index = rubric_indexes.get(index_name)
        predicate = str(
            ((index or {}).get("dialect_options") or {}).get("postgresql_where") or ""
        )
        if index is None or not index.get("unique") or visibility not in predicate:
            raise RuntimeError("rubric visibility partial unique index is incomplete: %s" % index_name)
    connection_checks = {
        value["name"] for value in inspector.get_check_constraints("ai_connections")
    }
    if not {
        "ck_ai_connections_private_scope",
        "ck_ai_connections_provider_type",
        "ck_ai_connections_status",
    }.issubset(connection_checks):
        raise RuntimeError("AI connection security constraints are incomplete")
    runtime_role_exists = bool(
        session.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgs_app')")
        )
    )
    if runtime_role_exists:
        for table in ("rule_scoring_tasks", "manual_review_tasks"):
            access = session.execute(
                text(
                    "SELECT c.relrowsecurity, "
                    "has_table_privilege('pgs_app', :table, "
                    "'SELECT,INSERT,UPDATE,DELETE'), "
                    "EXISTS (SELECT 1 FROM pg_policies p "
                    "WHERE p.schemaname = 'public' AND p.tablename = :table "
                    "AND p.policyname = 'pgs_app_dml') "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relname = :table"
                ),
                {"table": table},
            ).one_or_none()
            if access != (True, True, True):
                raise RuntimeError(
                    "runtime role access/RLS is incomplete for %s" % table
                )
    ordered_rows = session.execute(
        text(
            "SELECT id, created_at FROM batch_scoring_items "
            "ORDER BY created_at, id LIMIT 100"
        )
    ).all()
    ordered_pairs = [(row.created_at, row.id) for row in ordered_rows]
    if ordered_pairs != sorted(ordered_pairs):
        raise RuntimeError("batch scoring item ordering is not deterministic")
    return {
        "schema_version": "postgres-ops-verification@1",
        "dialect": "postgresql",
        "migration_head": head,
        "migration_sequence": list(MIGRATION_SEQUENCE),
        "active_job_index": ACTIVE_JOB_INDEX,
        "rubric_visibility_indexes": sorted(
            name for name in rubric_indexes if name.startswith("uq_rubrics_")
        ),
        "stable_ordering": list(stable_ordering_clause()),
        "sampled_item_count": len(ordered_rows),
        "production_default_switch_authorized": False,
    }


def exercise_postgres_constraints(session):
    """Create an explicit CI-only fixture and prove the active-job constraint."""

    if session.get_bind().dialect.name != "postgresql":
        raise RuntimeError("constraint exercise requires postgresql")
    user = ensure_dev_user(session)
    organization_id = session.scalar(
        text("SELECT id FROM organizations WHERE name = 'Default Organization'")
    )
    if organization_id is None:
        raise RuntimeError("default organization migration backfill is missing")
    rubric = models.Rubric(
        name="PGS-12 Postgres CI rubric",
        version=models.new_id(),
        total_score=10,
        status="published",
        created_by=user.id,
        owner_id=user.id,
        organization_id=organization_id,
    )
    batch = models.GradingBatch(
        name="PGS-12 Postgres CI batch",
        rubric=rubric,
        status="draft",
        created_by=user.id,
        owner_id=user.id,
        organization_id=organization_id,
    )
    session.add_all([rubric, batch])
    session.flush()
    for ordinal in range(2):
        session.add(
            models.Paper(
                batch=batch,
                file_name="ops-%s.docx" % ordinal,
                file_path="/ci/ops-%s.docx" % ordinal,
                parsed_text_path="/ci/ops-%s.json" % ordinal,
                status="parsed",
                owner_id=user.id,
                organization_id=organization_id,
            )
        )
    session.commit()
    policy = {
        "schema_version": "core-cutover-observation-policy@1",
        "minimum_sample_size": 2,
        "observation_window": {"minimum_completed_items": 2},
        "thresholds": {
            "max_abs_legacy_core_delta": "0.5",
            "max_invalid_evidence_rate": "0.01",
            "max_unauthorized_rule_rate": "0",
            "max_manual_review_rate": "0.2",
            "min_cache_hit_rate": "0.5",
            "max_checker_failure_rate": "0.01",
            "max_llm_failure_rate": "0.01",
            "max_retry_rate": "0.1",
            "max_p95_latency_ms": "1000",
        },
        "fallback_tolerance": {"max_abs_score_delta": "0.5"},
    }
    job, _created = create_batch_scoring_job(
        session,
        batch_id=batch.id,
        rescore=False,
        max_workers=2,
        observation_policy=policy,
        actor_id=settings.DEFAULT_DEV_USER_ID,
    )
    duplicate = models.BatchScoringJob(
        grading_batch_id=batch.id,
        generation=job.generation + 1,
        rescore=False,
        max_workers=1,
        status="queued",
        total_items=0,
        observation_policy=job.observation_policy,
        observation_policy_hash=job.observation_policy_hash,
        created_by=settings.DEFAULT_DEV_USER_ID,
    )
    session.add(duplicate)
    uniqueness_enforced = False
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        uniqueness_enforced = True
    if not uniqueness_enforced:
        raise RuntimeError("Postgres did not enforce one active job per batch")
    return {
        "schema_version": "postgres-ops-constraint-exercise@1",
        "batch_id": batch.id,
        "job_id": job.id,
        "one_active_job_enforced": True,
        "production_default_switch_authorized": False,
    }


__all__ = [
    "MIGRATION_SEQUENCE",
    "exercise_postgres_constraints",
    "stable_ordering_clause",
    "verify_postgres",
]
