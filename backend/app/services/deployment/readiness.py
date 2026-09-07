"""Read-only operations readiness signals; never a Core release authority."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import shutil

from sqlalchemy import select
from sqlalchemy import text

from backend.app.core.config import deployment_security_issues
from backend.app.core.config import settings
from backend.app.db.models import BatchScoringJob
from backend.app.db.models import GradingBatch
from backend.app.db.models import utcnow


def _thresholds():
    return {
        "disk_free_gb_min": settings.OPS_DISK_FREE_GB_MIN,
        "database_size_gb_max": settings.OPS_DATABASE_SIZE_GB_MAX,
        "batch_stale_minutes": settings.OPS_BATCH_STALE_MINUTES,
        "llm_failure_rate_max": settings.OPS_LLM_FAILURE_RATE_MAX,
        "rto_minutes": settings.OPS_RTO_MINUTES,
        "rpo_minutes": settings.OPS_RPO_MINUTES,
    }


def _disk_signal():
    root = Path(settings.STORAGE_ROOT)
    probe = root if root.exists() else root.parent
    usage = shutil.disk_usage(probe)
    free_gb = Decimal(usage.free) / Decimal(1024**3)
    threshold = Decimal(str(settings.OPS_DISK_FREE_GB_MIN))
    return {
        "status": "pass" if free_gb >= threshold else "fail",
        "free_gb": format(free_gb.quantize(Decimal("0.001")), "f"),
        "minimum_free_gb": str(settings.OPS_DISK_FREE_GB_MIN),
    }


def _database_signal(db):
    dialect = db.get_bind().dialect.name
    db.execute(text("SELECT 1"))
    size_bytes = None
    if dialect == "postgresql":
        size_bytes = db.scalar(text("SELECT pg_database_size(current_database())"))
    elif dialect == "sqlite":
        database = getattr(db.get_bind().url, "database", None)
        if database and database != ":memory:":
            path = Path(database)
            if path.is_file():
                size_bytes = path.stat().st_size
    size_gb = (
        None
        if size_bytes is None
        else Decimal(int(size_bytes)) / Decimal(1024**3)
    )
    threshold = Decimal(str(settings.OPS_DATABASE_SIZE_GB_MAX))
    return {
        "status": (
            "pass" if size_gb is None or size_gb <= threshold else "fail"
        ),
        "dialect": dialect,
        "size_gb": (
            None
            if size_gb is None
            else format(size_gb.quantize(Decimal("0.001")), "f")
        ),
        "maximum_size_gb": str(settings.OPS_DATABASE_SIZE_GB_MAX),
    }


def _scoped(statement, organization_id):
    """Filter batch-scoring jobs to one organization *before* aggregating.

    Counting first and filtering afterwards would leak cross-organization
    totals into an organization-scoped view (frontend v2 plan §2.1).
    """
    if organization_id is None:
        return statement
    return statement.join(
        GradingBatch, GradingBatch.id == BatchScoringJob.grading_batch_id
    ).where(GradingBatch.organization_id == organization_id)


def _batch_signal(db, organization_id=None):
    jobs = db.scalars(
        _scoped(
            select(BatchScoringJob).where(
                BatchScoringJob.status.in_(("queued", "running", "cancel_requested"))
            ),
            organization_id,
        ).order_by(BatchScoringJob.created_at, BatchScoringJob.id)
    ).all()
    cutoff = utcnow() - timedelta(minutes=settings.OPS_BATCH_STALE_MINUTES)
    stale = []
    for job in jobs:
        activity = job.heartbeat_at or job.updated_at or job.created_at
        if activity < cutoff:
            stale.append(job.id)
    completed = db.scalars(
        _scoped(
            select(BatchScoringJob).where(
                BatchScoringJob.metrics_snapshot.is_not(None)
            ),
            organization_id,
        )
        .order_by(BatchScoringJob.finished_at.desc(), BatchScoringJob.id.desc())
        .limit(100)
    ).all()
    observed_rates = []
    for job in completed:
        value = (job.metrics_snapshot or {}).get("llm_failure_rate")
        if value is not None:
            observed_rates.append(Decimal(str(value)))
    maximum_rate = max(observed_rates) if observed_rates else None
    allowed = Decimal(str(settings.OPS_LLM_FAILURE_RATE_MAX))
    llm_status = (
        "unavailable"
        if maximum_rate is None
        else "pass" if maximum_rate <= allowed else "fail"
    )
    return {
        "status": "pass" if not stale else "fail",
        "active_count": len(jobs),
        "stale_count": len(stale),
        "stale_job_ids": stale,
        "stale_after_minutes": settings.OPS_BATCH_STALE_MINUTES,
        "llm_failure_rate": {
            "status": llm_status,
            "maximum_observed": (
                None if maximum_rate is None else format(maximum_rate, "f")
            ),
            "maximum_allowed": str(settings.OPS_LLM_FAILURE_RATE_MAX),
            "sampled_jobs": len(observed_rates),
        },
    }


def build_ops_readiness(db):
    signals = {
        "disk": _disk_signal(),
        "database": _database_signal(db),
        "batch_jobs": _batch_signal(db),
    }
    security_issues = deployment_security_issues(settings)
    signals["security"] = {
        "status": "pass" if not security_issues else "fail",
        "issue_codes": security_issues,
        "auth_enabled": bool(settings.AUTH_ENABLED),
        "raw_llm_debug_logging": bool(settings.LLM_DEBUG_LOG_ENABLED),
    }
    ready = all(value["status"] == "pass" for value in signals.values())
    llm_signal = signals["batch_jobs"]["llm_failure_rate"]
    if llm_signal["status"] == "fail":
        ready = False
    return {
        "schema_version": "ops-readiness@1",
        "ready_for_production": ready,
        "thresholds": _thresholds(),
        "signals": signals,
        # Operational readiness is necessary but not sufficient for GATE-03.
        "production_default_switch_authorized": False,
    }


def build_organization_readiness(db, organization_id):
    """Batch-scoring health for one organization (frontend v2 plan §2.1).

    Deliberately excludes the platform-wide signals carried by
    :func:`build_ops_readiness` — disk, database size and deployment security
    are host facts, not organization facts, and must not reach an organization
    administrator. Like the platform view, this is observation only and never a
    Core release authority.
    """
    batch_jobs = _batch_signal(db, organization_id=organization_id)
    return {
        "schema_version": "organization-readiness@1",
        "organization_id": organization_id,
        "thresholds": {
            "batch_stale_minutes": settings.OPS_BATCH_STALE_MINUTES,
            "llm_failure_rate_max": settings.OPS_LLM_FAILURE_RATE_MAX,
        },
        "signals": {"batch_jobs": batch_jobs},
        "production_default_switch_authorized": False,
    }


__all__ = ["build_ops_readiness", "build_organization_readiness"]
