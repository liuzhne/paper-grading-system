"""Persistent, recoverable and bounded batch scoring jobs.

Observation policy values are supplied by an authorized user and stored with
the job.  This module validates and evaluates them but deliberately supplies no
production thresholds and never authorizes the final Core default switch.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait
from copy import deepcopy
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from decimal import InvalidOperation
from math import ceil
from time import monotonic
import uuid

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.db.models import BatchScoringItem
from backend.app.db.models import BatchScoringJob
from backend.app.db.models import GradingBatch
from backend.app.services.batches import state as batch_state
from backend.app.db.models import Paper
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun
from backend.app.db.models import utcnow
from backend.app.services.llm.errors import ProviderCallError
from backend.app.services.scoring.core.canonical import canonical_sha256


ACTIVE_JOB_STATUSES = ("queued", "running", "cancel_requested")
TERMINAL_JOB_STATUSES = (
    "completed",
    "completed_with_errors",
    "canceled",
    "failed",
)
RETRYABLE_ITEM_STATUSES = ("failed", "canceled", "running")
POLICY_SCHEMA_VERSION = "core-cutover-observation-policy@1"
METRICS_SCHEMA_VERSION = "batch-observation-metrics@1"
RUNNER_LEASE_SECONDS = 120
RUNNER_HEARTBEAT_SECONDS = 15

_THRESHOLD_DIRECTIONS = {
    "max_abs_legacy_core_delta": "max",
    "max_invalid_evidence_rate": "max",
    "max_unauthorized_rule_rate": "max",
    "max_manual_review_rate": "max",
    "min_cache_hit_rate": "min",
    "max_checker_failure_rate": "max",
    "max_llm_failure_rate": "max",
    "max_retry_rate": "max",
    "max_p95_latency_ms": "max",
}
_RATE_THRESHOLDS = {
    name
    for name in _THRESHOLD_DIRECTIONS
    if name.endswith("_rate")
}


def _decimal(value, *, field):
    if isinstance(value, bool):
        raise ValueError("%s must be a finite decimal" % field)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("%s must be a finite decimal" % field) from exc
    if not result.is_finite():
        raise ValueError("%s must be a finite decimal" % field)
    return result


def _decimal_text(value):
    value = Decimal(value)
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def validate_observation_policy(policy):
    if not isinstance(policy, dict):
        raise ValueError("observation_policy must be an object")
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ValueError(
            "observation_policy.schema_version must be %s"
            % POLICY_SCHEMA_VERSION
        )
    minimum = policy.get("minimum_sample_size")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        raise ValueError("minimum_sample_size must be a positive integer")
    window = policy.get("observation_window")
    if not isinstance(window, dict):
        raise ValueError("observation_window must be an object")
    completed = window.get("minimum_completed_items")
    if isinstance(completed, bool) or not isinstance(completed, int) or completed < 1:
        raise ValueError(
            "observation_window.minimum_completed_items must be a positive integer"
        )
    thresholds = policy.get("thresholds")
    if not isinstance(thresholds, dict):
        raise ValueError("thresholds must be an object")
    missing = sorted(set(_THRESHOLD_DIRECTIONS) - set(thresholds))
    if missing:
        raise ValueError("missing observation thresholds: %s" % ", ".join(missing))
    normalized = deepcopy(policy)
    for field in _THRESHOLD_DIRECTIONS:
        value = _decimal(thresholds[field], field="thresholds.%s" % field)
        if value < 0:
            raise ValueError("thresholds.%s must be nonnegative" % field)
        if field in _RATE_THRESHOLDS and value > 1:
            raise ValueError("thresholds.%s must be between 0 and 1" % field)
        normalized["thresholds"][field] = _decimal_text(value)
    fallback = policy.get("fallback_tolerance")
    if not isinstance(fallback, dict) or "max_abs_score_delta" not in fallback:
        raise ValueError("fallback_tolerance.max_abs_score_delta is required")
    fallback_value = _decimal(
        fallback["max_abs_score_delta"],
        field="fallback_tolerance.max_abs_score_delta",
    )
    if fallback_value < 0:
        raise ValueError("fallback_tolerance.max_abs_score_delta must be nonnegative")
    normalized["fallback_tolerance"]["max_abs_score_delta"] = _decimal_text(
        fallback_value
    )
    return normalized


def _counter_values(items):
    counts = {
        "pending": 0,
        "running": 0,
        "succeeded": 0,
        "skipped": 0,
        "failed": 0,
        "canceled": 0,
    }
    for item in items:
        counts[item.status] += 1
    return counts


def _set_job_counts(job):
    counts = _counter_values(job.items)
    job.total_items = len(job.items)
    for status, value in counts.items():
        setattr(job, "%s_count" % status, value)


def get_batch_scoring_job(session, job_id):
    return session.scalar(
        select(BatchScoringJob)
        .where(BatchScoringJob.id == job_id)
        .options(selectinload(BatchScoringJob.items))
    )


def get_latest_batch_scoring_job(session, batch_id):
    return session.scalar(
        select(BatchScoringJob)
        .where(BatchScoringJob.grading_batch_id == batch_id)
        .order_by(BatchScoringJob.generation.desc())
        .options(selectinload(BatchScoringJob.items))
    )


def create_batch_scoring_job(
    session,
    *,
    batch_id,
    rescore,
    max_workers,
    observation_policy,
    actor_id,
):
    if isinstance(max_workers, bool) or not isinstance(max_workers, int):
        raise ValueError("max_workers must be an integer")
    if max_workers < 1 or max_workers > 16:
        raise ValueError("max_workers must be between 1 and 16")
    policy = validate_observation_policy(observation_policy)
    policy_hash = canonical_sha256(policy)
    batch = session.scalar(
        select(GradingBatch)
        .where(GradingBatch.id == batch_id)
        .options(selectinload(GradingBatch.papers))
        .with_for_update()
    )
    if batch is None:
        raise ValueError("batch not found")
    if not batch.papers:
        raise ValueError("batch has no papers")

    active = session.scalar(
        select(BatchScoringJob)
        .where(
            BatchScoringJob.grading_batch_id == batch_id,
            BatchScoringJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(BatchScoringJob.generation.desc())
        .options(selectinload(BatchScoringJob.items))
        .with_for_update()
    )
    if active is not None:
        if (
            bool(active.rescore) == bool(rescore)
            and active.max_workers == max_workers
            and active.observation_policy_hash == policy_hash
        ):
            return active, False
        raise ValueError(
            "batch already has an active scoring job with a different request identity"
        )

    generation = session.scalar(
        select(func.max(BatchScoringJob.generation)).where(
            BatchScoringJob.grading_batch_id == batch_id
        )
    )
    job = BatchScoringJob(
        grading_batch_id=batch_id,
        generation=int(generation or 0) + 1,
        rescore=bool(rescore),
        max_workers=max_workers,
        status="queued",
        total_items=len(batch.papers),
        pending_count=len(batch.papers),
        observation_policy=policy,
        observation_policy_hash=policy_hash,
        created_by=actor_id,
    )
    session.add(job)
    session.flush()
    for paper in sorted(batch.papers, key=lambda value: (value.created_at, value.id)):
        session.add(
            BatchScoringItem(job_id=job.id, paper_id=paper.id, status="pending")
        )
    session.commit()
    return get_batch_scoring_job(session, job.id), True


def cancel_batch_scoring_job(session, job_id):
    job = session.scalar(
        select(BatchScoringJob)
        .where(BatchScoringJob.id == job_id)
        .options(selectinload(BatchScoringJob.items))
        .with_for_update()
    )
    if job is None:
        raise ValueError("batch scoring job not found")
    if job.status in TERMINAL_JOB_STATUSES:
        return job
    now = utcnow()
    job.cancel_requested_at = now
    if job.status == "queued":
        for item in job.items:
            if item.status in ("pending", "running"):
                item.status = "canceled"
                item.finished_at = now
        job.status = "canceled"
        job.finished_at = now
    else:
        job.status = "cancel_requested"
    _set_job_counts(job)
    session.commit()
    return get_batch_scoring_job(session, job.id)


def retry_batch_scoring_job(session, job_id):
    job = session.scalar(
        select(BatchScoringJob)
        .where(BatchScoringJob.id == job_id)
        .options(selectinload(BatchScoringJob.items))
        .with_for_update()
    )
    if job is None:
        raise ValueError("batch scoring job not found")
    if job.status not in ("canceled", "completed_with_errors", "failed"):
        raise ValueError("only failed or canceled batch scoring jobs can be retried")
    retryable = [item for item in job.items if item.status in RETRYABLE_ITEM_STATUSES]
    if not retryable:
        raise ValueError("batch scoring job has no retryable items")
    for item in retryable:
        item.status = "pending"
        item.scoring_run_id = None
        item.error_code = None
        item.error_message = None
        item.telemetry = None
        item.started_at = None
        item.finished_at = None
    job.status = "queued"
    job.cancel_requested_at = None
    job.finished_at = None
    job.metrics_snapshot = None
    _set_job_counts(job)
    session.commit()
    return get_batch_scoring_job(session, job.id)


def _signal(actual, threshold, *, direction):
    if actual is None:
        return {
            "status": "unavailable",
            "actual": None,
            "threshold": _decimal_text(threshold),
        }
    actual = _decimal(actual, field="metric")
    passed = actual <= threshold if direction == "max" else actual >= threshold
    return {
        "status": "pass" if passed else "fail",
        "actual": _decimal_text(actual),
        "threshold": _decimal_text(threshold),
    }


def evaluate_observation_policy(policy, metrics):
    policy = validate_observation_policy(policy)
    metrics = metrics if isinstance(metrics, dict) else {}
    thresholds = policy["thresholds"]
    signals = {}

    sample_size = metrics.get("sample_size")
    completed_items = metrics.get("completed_items")
    signals["minimum_sample_size"] = _signal(
        sample_size,
        Decimal(policy["minimum_sample_size"]),
        direction="min",
    )
    signals["observation_window"] = _signal(
        completed_items,
        Decimal(policy["observation_window"]["minimum_completed_items"]),
        direction="min",
    )

    comparison = metrics.get("legacy_core_delta")
    if not isinstance(comparison, dict) or not comparison.get("available"):
        signals["legacy_core_delta"] = {
            "status": "unavailable",
            "actual": None,
            "threshold": thresholds["max_abs_legacy_core_delta"],
            "fallback_tolerance": policy["fallback_tolerance"][
                "max_abs_score_delta"
            ],
        }
    else:
        values = comparison.get("values")
        if not isinstance(values, list) or not values:
            signals["legacy_core_delta"] = {
                "status": "unavailable",
                "actual": None,
                "threshold": thresholds["max_abs_legacy_core_delta"],
                "fallback_tolerance": policy["fallback_tolerance"][
                    "max_abs_score_delta"
                ],
            }
        else:
            maximum = max(
                abs(_decimal(value, field="legacy_core_delta")) for value in values
            )
            allowed = min(
                _decimal(
                    thresholds["max_abs_legacy_core_delta"],
                    field="max_abs_legacy_core_delta",
                ),
                _decimal(
                    policy["fallback_tolerance"]["max_abs_score_delta"],
                    field="max_abs_score_delta",
                ),
            )
            signals["legacy_core_delta"] = _signal(
                maximum, allowed, direction="max"
            )

    metric_by_threshold = {
        "max_invalid_evidence_rate": "invalid_evidence_rate",
        "max_unauthorized_rule_rate": "unauthorized_rule_rate",
        "max_manual_review_rate": "manual_review_rate",
        "min_cache_hit_rate": "cache_hit_rate",
        "max_checker_failure_rate": "checker_failure_rate",
        "max_llm_failure_rate": "llm_failure_rate",
        "max_retry_rate": "retry_rate",
        "max_p95_latency_ms": "p95_latency_ms",
    }
    for threshold_name, metric_name in metric_by_threshold.items():
        signals[metric_name] = _signal(
            metrics.get(metric_name),
            _decimal(thresholds[threshold_name], field=threshold_name),
            direction=_THRESHOLD_DIRECTIONS[threshold_name],
        )

    ready = all(signal["status"] == "pass" for signal in signals.values())
    return {
        "schema_version": "core-cutover-observation-report@1",
        "ready_for_gate": ready,
        "rollback_required": not ready,
        # PGS-6 is observational.  Only the later approved GATE-03 run may
        # authorize a production default switch.
        "production_default_switch_authorized": False,
        "signals": signals,
    }


def _contains_unknown_rule(value):
    if isinstance(value, dict):
        if any(item == "UNKNOWN_RULE" for item in value.values()):
            return True
        return any(_contains_unknown_rule(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_unknown_rule(item) for item in value)
    return False


def _count_cache_observations(value):
    if isinstance(value, dict):
        hits = 1 if value.get("cache_hit") is True else 0
        misses = 1 if value.get("cache_hit") is False else 0
        for item in value.values():
            child_hits, child_misses = _count_cache_observations(item)
            hits += child_hits
            misses += child_misses
        return hits, misses
    if isinstance(value, list):
        hits = misses = 0
        for item in value:
            child_hits, child_misses = _count_cache_observations(item)
            hits += child_hits
            misses += child_misses
        return hits, misses
    return 0, 0


def _telemetry_for_run(session, run, *, latency_ms):
    run = session.scalar(
        select(ScoringRun)
        .where(ScoringRun.id == run.id)
        .options(selectinload(ScoringRun.items))
    )
    score_items = list(run.items)
    rule_results = [value for item in score_items for value in (item.rule_results or [])]
    cache_hits = cache_misses = 0
    for item in score_items:
        hits, misses = _count_cache_observations(item.raw_model_output)
        cache_hits += hits
        cache_misses += misses
    return {
        "score_item_count": len(score_items),
        "invalid_evidence_count": sum(
            1
            for item in score_items
            if not item.evidence_sufficient
            or item.auto_score_status in ("invalid", "blocked")
        ),
        "rule_decision_count": len(rule_results),
        "unauthorized_rule_count": sum(
            1 for value in rule_results if _contains_unknown_rule(value)
        ),
        "manual_review": bool(run.need_manual_review),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "checker_failures": 0,
        "llm_failures": 0,
        "latency_ms": int(latency_ms),
        # Comparison artifacts are non-authoritative and are not inferred from
        # a single run.  A gate remains closed until a paired delta is supplied.
        "legacy_core_delta": None,
        "profile_key": run.business_profile_key or "legacy",
        "rubric_version_id": run.rubric_version_id or "legacy-unversioned",
    }


def _default_score_item(session, *, paper_id, job_id):
    from backend.app.services.scoring.engine import retry_score_paper
    from backend.app.services.scoring.engine import score_paper

    job = session.get(BatchScoringJob, job_id)
    job_item = session.scalar(
        select(BatchScoringItem).where(
            BatchScoringItem.job_id == job_id,
            BatchScoringItem.paper_id == paper_id,
        )
    )
    paper = session.scalar(
        select(Paper)
        .where(Paper.id == paper_id)
        .options(selectinload(Paper.scoring_runs))
    )
    if job is None or job_item is None or paper is None:
        raise ValueError("batch scoring job or paper not found")
    if paper.status == "failed" or not paper.parsed_text_path:
        raise ValueError(paper.error_message or "paper is not parsed")
    previous = max(
        paper.scoring_runs,
        key=lambda value: (value.created_at, value.id),
        default=None,
    )
    if (
        job_item.attempt_count > 1
        and previous is not None
        and previous.id != job_item.baseline_scoring_run_id
    ):
        return {
            "status": "succeeded",
            "run_id": previous.id,
            "telemetry": _telemetry_for_run(session, previous, latency_ms=0),
        }
    if previous is not None and not job.rescore:
        return {
            "status": "skipped",
            "run_id": previous.id,
            "telemetry": _telemetry_for_run(session, previous, latency_ms=0),
        }
    started = monotonic()
    if previous is not None:
        run = retry_score_paper(session, previous.id)
    else:
        run = score_paper(session, paper.id)
    latency_ms = max(0, int((monotonic() - started) * 1000))
    return {
        "status": "succeeded",
        "run_id": run.id,
        "telemetry": _telemetry_for_run(session, run, latency_ms=latency_ms),
    }


def _worker(session_factory, score_item, *, paper_id, job_id):
    with session_factory() as session:
        return score_item(session, paper_id=paper_id, job_id=job_id)


def _classify_failure(exc):
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ProviderCallError):
            return current.error.code, "llm"
        current = current.__cause__ or current.__context__
    text = str(exc).lower()
    name = type(exc).__name__.lower()
    if "429" in text or "rate limit" in text or "ratelimit" in name:
        return "rate_limited", "llm"
    if "timeout" in text or "timeout" in name:
        return "timeout", "llm"
    if "llm" in text or "model" in text:
        return "llm_failure", "llm"
    if "checker" in text:
        return "checker_failure", "checker"
    return "scoring_failure", "checker"


def _checkpoint_started(session_factory, item_id):
    with session_factory() as session:
        item = session.scalar(
            select(BatchScoringItem)
            .where(
                BatchScoringItem.id == item_id,
                BatchScoringItem.status == "pending",
            )
            .with_for_update()
        )
        if item is None:
            return False
        if item.attempt_count == 0:
            item.baseline_scoring_run_id = session.scalar(
                select(ScoringRun.id)
                .where(ScoringRun.paper_id == item.paper_id)
                .order_by(ScoringRun.created_at.desc(), ScoringRun.id.desc())
                .limit(1)
            )
        item.status = "running"
        item.attempt_count += 1
        item.started_at = utcnow()
        item.finished_at = None
        session.commit()
        return True


def _checkpoint_result(session_factory, item_id, *, result=None, error=None):
    with session_factory() as session:
        item = session.scalar(
            select(BatchScoringItem)
            .where(BatchScoringItem.id == item_id)
            .with_for_update()
        )
        if item is None:
            return
        item.finished_at = utcnow()
        if error is None:
            result = result if isinstance(result, dict) else {}
            status = result.get("status", "succeeded")
            if status not in ("succeeded", "skipped"):
                raise ValueError("score_item returned an unsupported status")
            item.status = status
            item.scoring_run_id = result.get("run_id")
            item.telemetry = result.get("telemetry") or {}
            item.error_code = None
            item.error_message = None
            history_entry = {
                "attempt": item.attempt_count,
                "status": status,
                "error_code": None,
                "scoring_run_id": item.scoring_run_id,
                "latency_ms": int(item.telemetry.get("latency_ms") or 0),
            }
        else:
            code, failure_kind = _classify_failure(error)
            item.status = "failed"
            item.error_code = code
            item.error_message = str(error)[:4000]
            item.telemetry = {
                "score_item_count": 0,
                "invalid_evidence_count": 0,
                "rule_decision_count": 0,
                "unauthorized_rule_count": 0,
                "manual_review": False,
                "cache_hits": 0,
                "cache_misses": 0,
                "checker_failures": 1 if failure_kind == "checker" else 0,
                "llm_failures": 1 if failure_kind == "llm" else 0,
                "latency_ms": 0,
                "legacy_core_delta": None,
                "profile_key": "unknown",
                "rubric_version_id": "unknown",
            }
            history_entry = {
                "attempt": item.attempt_count,
                "status": "failed",
                "error_code": code,
                "failure_kind": failure_kind,
                "scoring_run_id": None,
                "latency_ms": 0,
            }
        history = list(item.attempt_history or [])
        history.append(history_entry)
        item.attempt_history = history
        session.commit()


def _rate(numerator, denominator):
    if denominator <= 0:
        return None
    return _decimal_text(Decimal(numerator) / Decimal(denominator))


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(Decimal(str(value)) for value in values)
    index = max(0, ceil((percentile / 100) * len(ordered)) - 1)
    return _decimal_text(ordered[index])


def _aggregate_metrics(job):
    completed = [
        item
        for item in job.items
        if item.status in ("succeeded", "skipped", "failed")
    ]
    successful = [
        item for item in completed if item.status in ("succeeded", "skipped")
    ]
    telemetry = [item.telemetry or {} for item in completed]
    score_items = sum(int(value.get("score_item_count") or 0) for value in telemetry)
    invalid = sum(
        int(value.get("invalid_evidence_count") or 0) for value in telemetry
    )
    decisions = sum(
        int(value.get("rule_decision_count") or 0) for value in telemetry
    )
    unauthorized = sum(
        int(value.get("unauthorized_rule_count") or 0) for value in telemetry
    )
    hits = sum(int(value.get("cache_hits") or 0) for value in telemetry)
    misses = sum(int(value.get("cache_misses") or 0) for value in telemetry)
    retries = sum(max(int(item.attempt_count) - 1, 0) for item in job.items)
    attempts = sum(int(item.attempt_count) for item in job.items)
    attempts_history = [
        attempt
        for item in job.items
        for attempt in (item.attempt_history or [])
    ]
    latencies = [
        value.get("latency_ms")
        for value in attempts_history
        if value.get("latency_ms") is not None
    ]
    attempt_error_counts = {
        code: sum(
            1
            for attempt in attempts_history
            if attempt.get("error_code") == code
        )
        for code in sorted(
            {
                attempt.get("error_code")
                for attempt in attempts_history
                if attempt.get("error_code")
            }
        )
    }
    llm_failure_count = sum(
        attempt_error_counts.get(code, 0)
        for code in ("timeout", "rate_limited", "llm_failure")
    )
    checker_failure_count = sum(
        value
        for code, value in attempt_error_counts.items()
        if code not in ("timeout", "rate_limited", "llm_failure")
    )
    deltas = [
        value.get("legacy_core_delta")
        for value in (item.telemetry or {} for item in successful)
    ]
    delta_available = bool(successful) and all(value is not None for value in deltas)
    cache_by_identity = {}
    for value in telemetry:
        key = "%s:%s" % (
            value.get("profile_key") or "unknown",
            value.get("rubric_version_id") or "unknown",
        )
        group = cache_by_identity.setdefault(key, {"hits": 0, "misses": 0})
        group["hits"] += int(value.get("cache_hits") or 0)
        group["misses"] += int(value.get("cache_misses") or 0)
    for group in cache_by_identity.values():
        group["hit_rate"] = _rate(
            group["hits"], group["hits"] + group["misses"]
        )

    metrics = {
        "sample_size": len(successful),
        "completed_items": len(completed),
        "invalid_evidence_rate": _rate(invalid, score_items),
        "unauthorized_rule_rate": _rate(unauthorized, decisions),
        "manual_review_rate": _rate(
            sum(bool(value.get("manual_review")) for value in telemetry),
            len(successful),
        ),
        "cache_hit_rate": _rate(hits, hits + misses),
        "cache_by_profile_and_rubric_version": cache_by_identity,
        "checker_failure_rate": _rate(
            checker_failure_count,
            len(attempts_history),
        ),
        "llm_failure_rate": _rate(
            llm_failure_count,
            len(attempts_history),
        ),
        "retry_rate": _rate(retries, attempts),
        "p50_latency_ms": _percentile(latencies, 50),
        "p95_latency_ms": _percentile(latencies, 95),
        "legacy_core_delta": {
            "available": delta_available,
            "values": deltas if delta_available else [],
        },
        "error_counts": {
            code: sum(1 for item in job.items if item.error_code == code)
            for code in sorted(
                {item.error_code for item in job.items if item.error_code}
            )
        },
        "attempt_error_counts": attempt_error_counts,
    }
    return metrics


def _cancel_pending_items(session_factory, job_id):
    with session_factory() as session:
        job = session.scalar(
            select(BatchScoringJob)
            .where(BatchScoringJob.id == job_id)
            .options(selectinload(BatchScoringJob.items))
            .with_for_update()
        )
        if job is None or job.status != "cancel_requested":
            return False
        now = utcnow()
        for item in job.items:
            if item.status == "pending":
                item.status = "canceled"
                item.finished_at = now
        _set_job_counts(job)
        session.commit()
        return True


def _is_cancel_requested(session_factory, job_id):
    with session_factory() as session:
        status = session.scalar(
            select(BatchScoringJob.status).where(BatchScoringJob.id == job_id)
        )
        return status == "cancel_requested"


def _heartbeat_job(session_factory, job_id, runner_token):
    with session_factory() as session:
        job = session.scalar(
            select(BatchScoringJob)
            .where(BatchScoringJob.id == job_id)
            .with_for_update()
        )
        if job is None or job.runner_token != runner_token:
            raise RuntimeError("batch scoring job runner lease was lost")
        if job.status not in ("running", "cancel_requested"):
            raise RuntimeError("batch scoring job is no longer executable")
        job.heartbeat_at = utcnow()
        session.commit()


def run_batch_scoring_job(
    session_factory,
    *,
    job_id,
    score_item=None,
    executor_factory=ThreadPoolExecutor,
):
    score_item = score_item or _default_score_item
    runner_token = str(uuid.uuid4())
    with session_factory() as session:
        job = session.scalar(
            select(BatchScoringJob)
            .where(BatchScoringJob.id == job_id)
            .options(selectinload(BatchScoringJob.items))
            .with_for_update()
        )
        if job is None:
            raise ValueError("batch scoring job not found")
        now = utcnow()
        if job.status == "running":
            lease_cutoff = now - timedelta(seconds=RUNNER_LEASE_SECONDS)
            if job.heartbeat_at is not None and job.heartbeat_at > lease_cutoff:
                raise ValueError("batch scoring job is already running")
            for item in job.items:
                if item.status == "running":
                    item.status = "pending"
                    item.finished_at = None
        elif job.status != "queued":
            raise ValueError("batch scoring job is not queued")
        job.status = "running"
        job.runner_token = runner_token
        job.heartbeat_at = now
        job.started_at = job.started_at or now
        job.finished_at = None
        # 批次业务阶段随执行进入 scoring。恢复既有 running 任务时阶段已经是
        # scoring，重复触发会被状态机判为非法转移，因此只在需要时推进。
        batch = session.get(GradingBatch, job.grading_batch_id)
        if batch is not None and batch.status != "scoring":
            batch_state.apply_event(session, batch, "start_scoring")
        pending = [(item.id, item.paper_id) for item in job.items if item.status == "pending"]
        session.commit()
        max_workers = job.max_workers

    queue = iter(pending)
    in_flight = {}
    canceled = False
    with executor_factory(max_workers=max_workers) as executor:
        while True:
            while not canceled and len(in_flight) < max_workers:
                try:
                    item_id, paper_id = next(queue)
                except StopIteration:
                    break
                if not _checkpoint_started(session_factory, item_id):
                    continue
                future = executor.submit(
                    _worker,
                    session_factory,
                    score_item,
                    paper_id=paper_id,
                    job_id=job_id,
                )
                in_flight[future] = item_id
            if not in_flight:
                break
            completed, _ = wait(
                tuple(in_flight),
                timeout=RUNNER_HEARTBEAT_SECONDS,
                return_when=FIRST_COMPLETED,
            )
            _heartbeat_job(session_factory, job_id, runner_token)
            for future in completed:
                item_id = in_flight.pop(future)
                try:
                    result = future.result()
                except Exception as exc:  # isolated paper failure is persisted
                    _checkpoint_result(session_factory, item_id, error=exc)
                else:
                    _checkpoint_result(session_factory, item_id, result=result)
            canceled = _is_cancel_requested(session_factory, job_id)
            if canceled:
                _cancel_pending_items(session_factory, job_id)

    with session_factory() as session:
        job = session.scalar(
            select(BatchScoringJob)
            .where(BatchScoringJob.id == job_id)
            .options(selectinload(BatchScoringJob.items))
            .with_for_update()
        )
        _set_job_counts(job)
        if job.canceled_count:
            job.status = "canceled"
        elif job.failed_count:
            job.status = "completed_with_errors"
        elif job.succeeded_count or job.skipped_count:
            job.status = "completed"
        else:
            job.status = "failed"
        job.finished_at = utcnow()
        job.runner_token = None
        job.heartbeat_at = utcnow()
        metrics = _aggregate_metrics(job)
        job.metrics_snapshot = {
            "schema_version": METRICS_SCHEMA_VERSION,
            **metrics,
            "gate": evaluate_observation_policy(job.observation_policy, metrics),
        }
        batch = session.get(GradingBatch, job.grading_batch_id)
        if batch is not None:
            if job.status == "completed":
                batch_state.apply_event(session, batch, "finish_scoring", outcome="scored")
            elif job.status == "completed_with_errors":
                batch_state.apply_event(
                    session, batch, "finish_scoring", outcome="scored_with_errors"
                )
        session.commit()
        job_id = job.id
    with session_factory() as session:
        return get_batch_scoring_job(session, job_id)


__all__ = [
    "cancel_batch_scoring_job",
    "create_batch_scoring_job",
    "evaluate_observation_policy",
    "get_batch_scoring_job",
    "get_latest_batch_scoring_job",
    "retry_batch_scoring_job",
    "run_batch_scoring_job",
    "validate_observation_policy",
]
