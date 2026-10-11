"""Persistent, recoverable and bounded batch scoring jobs.

Observation policy values are supplied by an authorized user and stored with
the job.  This module validates and evaluates them but deliberately supplies no
production thresholds and never authorizes the final Core default switch.

Execution goes through the unified work queue (``services/work_queue``): one
claimed item is one paper, on Vercel and on the intranet/local worker alike.
This module is the ``batch_scoring`` work kind — it owns the job state machine,
the per-paper checkpoint and what counts as progress for a dead execution.
"""

from __future__ import annotations

from collections import Counter
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from decimal import InvalidOperation
import logging
from math import ceil
from time import monotonic

from sqlalchemy import case
from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import selectinload

from backend.app.db.models import BatchScoringItem
from backend.app.db.models import BatchScoringJob
from backend.app.db.models import GradingBatch
from backend.app.services.batches import state as batch_state
from backend.app.db.models import Paper
from backend.app.db.models import RuleScoringTask
from backend.app.db.models import ScoringRun
from backend.app.db.models import utcnow
from backend.app.services.ai_connections import AIConnectionBindingError
from backend.app.services.llm.errors import PlatformModelMissingError
from backend.app.services.llm.errors import ProviderCallError
from backend.app.services.scoring.decision_ledger import bypass_ledger_reads
from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.work_queue.kinds import ClaimedItem
from backend.app.services.work_queue.kinds import WorkKind
from backend.app.services.work_queue.kinds import register
from backend.app.services.work_queue.limits import ITEM_LEASE_SECONDS
from backend.app.services.work_queue.sources import resolve_source
from backend.app.services.work_queue.sources import source_key_for_connection


logger = logging.getLogger("batch-scoring-jobs")

KIND = "batch_scoring"
ACTIVE_JOB_STATUSES = ("queued", "running", "cancel_requested")
TERMINAL_JOB_STATUSES = (
    "completed",
    "completed_with_errors",
    "canceled",
    "failed",
)
RETRYABLE_ITEM_STATUSES = ("failed", "canceled", "running")
ITEM_STATUSES = ("pending", "running", "succeeded", "skipped", "failed", "canceled")
POLICY_SCHEMA_VERSION = "core-cutover-observation-policy@1"
METRICS_SCHEMA_VERSION = "batch-observation-metrics@1"
# 页面判断“执行中断”的租约：与条目心跳租约相同（统一执行模型后没有任务级执行者）。
RUNNER_LEASE_SECONDS = ITEM_LEASE_SECONDS
# 计入「模型失败率」的错误码；额度耗尽也是模型侧失败，不能算成检查器失败。
LLM_FAILURE_CODES = ("timeout", "rate_limited", "quota_exhausted", "llm_failure")
STALLED_ERROR_CODE = "WORK_ITEM_STALLED"
STALLED_ERROR_MESSAGE = (
    "多次执行都没能推进（超过运行时间上限或进程中止）；请重试失败项，"
    "仍然失败时检查这份材料或联系管理员。"
)


def batch_source_key(batch) -> str:
    """批次评分所用模型来源：私有连接按连接 ID，没有绑定连接的批次共用平台模型。"""

    return source_key_for_connection(getattr(batch, "ai_connection_id", None))


def _batch_connection_limit(session, batch):
    """批次所用模型声明的同时请求数；没有声明时返回 None。"""

    if batch is None:
        return None
    return resolve_source(session, batch_source_key(batch)).limit


ATTENTION_FAILURE_HOURS = 24

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


class IncompleteScoringResultError(ValueError):
    """Safe material-level projection of persisted rule execution failures."""

    def __init__(self, code, message, *, failure_kind):
        super().__init__(message)
        self.code = code
        self.failure_kind = failure_kind


def default_observation_policy(*, sample_size=1):
    """Return a fail-closed observation policy for ordinary scoring jobs.

    The policy exists because the durable job model predates the workbench and
    also carries Core cutover observations.  A normal teacher action must not
    have to invent release-gate thresholds; the unavailable paired comparison
    keeps this policy from authorizing a Core switch.
    """
    minimum = max(1, int(sample_size or 1))
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "minimum_sample_size": minimum,
        "observation_window": {"minimum_completed_items": minimum},
        "thresholds": {
            "max_abs_legacy_core_delta": "0.5",
            "max_invalid_evidence_rate": "0.05",
            "max_unauthorized_rule_rate": "0",
            "max_manual_review_rate": "1",
            "min_cache_hit_rate": "0",
            "max_checker_failure_rate": "1",
            "max_llm_failure_rate": "1",
            "max_retry_rate": "1",
            "max_p95_latency_ms": "600000",
        },
        "fallback_tolerance": {"max_abs_score_delta": "0.5"},
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


def list_attention_batch_scoring_jobs(session, *, organization_id=None):
    """List the latest active or failed execution for each visible batch."""
    recent_failure_cutoff = utcnow() - timedelta(hours=ATTENTION_FAILURE_HOURS)
    latest_generation = (
        select(
            BatchScoringJob.grading_batch_id.label("batch_id"),
            func.max(BatchScoringJob.generation).label("generation"),
        )
        .group_by(BatchScoringJob.grading_batch_id)
        .subquery()
    )
    query = (
        select(BatchScoringJob)
        .join(
            latest_generation,
            (BatchScoringJob.grading_batch_id == latest_generation.c.batch_id)
            & (BatchScoringJob.generation == latest_generation.c.generation),
        )
        .join(GradingBatch, GradingBatch.id == BatchScoringJob.grading_batch_id)
        .where(
            BatchScoringJob.status.in_(ACTIVE_JOB_STATUSES)
            | (
                BatchScoringJob.status.in_(("completed_with_errors", "failed"))
                & (BatchScoringJob.updated_at >= recent_failure_cutoff)
            )
        )
        .options(selectinload(BatchScoringJob.items))
        .order_by(BatchScoringJob.updated_at.desc(), BatchScoringJob.id.desc())
    )
    if organization_id is not None:
        query = query.where(GradingBatch.organization_id == organization_id)
    return list(session.scalars(query).all())




def _shift_counts(session, job_id, **deltas):
    """条目状态变化时增量更新任务计数（原子 SQL，不加载全部条目）。

    计数变化与条目变化在同一事务里、都在任务行锁之下提交，所以任何已提交的
    条目状态都已计入计数；巡检的重新计数据此可以安全地校正漂移。
    """

    values = {}
    for status, delta in deltas.items():
        if not delta:
            continue
        column = getattr(BatchScoringJob, "%s_count" % status)
        # 历史数据的计数可能与条目不一致：不让减法撞上非负约束，漂移由收敛时的
        # 重新计数校正。
        values[column] = case((column + delta < 0, 0), else_=column + delta)
    if values:
        session.execute(
            update(BatchScoringJob)
            .where(BatchScoringJob.id == job_id)
            .values(values)
            .execution_options(synchronize_session=False)
        )


def _recount(session, job_id):
    """按条目重新计数（取消、重试、巡检收敛用）；返回各状态的条数。

    计数没变就不写：写入会刷新 updated_at，而运维页按它判断任务是否停滞。
    """

    counts = {status: 0 for status in ITEM_STATUSES}
    for status, value in session.execute(
        select(BatchScoringItem.status, func.count(BatchScoringItem.id))
        .where(BatchScoringItem.job_id == job_id)
        .group_by(BatchScoringItem.status)
    ):
        counts[status] = int(value)
    values = {
        "total_items": sum(counts.values()),
        **{"%s_count" % status: value for status, value in counts.items()},
    }
    current = session.execute(
        select(*(getattr(BatchScoringJob, name) for name in values)).where(
            BatchScoringJob.id == job_id
        )
    ).one_or_none()
    if current is not None and tuple(current) != tuple(values.values()):
        session.execute(
            update(BatchScoringJob)
            .where(BatchScoringJob.id == job_id)
            .values(values)
            .execution_options(synchronize_session=False)
        )
    return counts


def _lock_job(session, job_id, *, with_items=False, skip_locked=False):
    statement = (
        select(BatchScoringJob)
        .where(BatchScoringJob.id == job_id)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
    if with_items:
        statement = statement.options(selectinload(BatchScoringJob.items))
    return session.scalar(statement)


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
    # 同步执行入口（/run）按 max_workers 开线程。模型声明了同时请求数就以它为准：
    # 免费档调低到 1，Bedrock 等按配额调高；没声明时沿用请求里的值（工作台默认 2）。
    # 真正的并发上限在领取时按来源检查，跨任务、跨实例都生效。
    connection_limit = _batch_connection_limit(session, batch)
    if connection_limit is not None:
        max_workers = connection_limit
    policy = validate_observation_policy(
        observation_policy
        if observation_policy is not None
        else default_observation_policy(sample_size=len(batch.papers))
    )
    policy_hash = canonical_sha256(policy)

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
    source_key = batch_source_key(batch)
    owner_id = actor_id or batch.owner_id
    papers = sorted(batch.papers, key=lambda value: (value.created_at, value.id))
    for ordinal, paper in enumerate(papers):
        session.add(
            BatchScoringItem(
                job_id=job.id,
                paper_id=paper.id,
                status="pending",
                source_key=source_key,
                owner_id=owner_id,
                ordinal=ordinal,
            )
        )
    session.commit()
    return get_batch_scoring_job(session, job.id), True


def cancel_batch_scoring_job(session, job_id):
    """取消：未开始的条目直接取消，进行中的条目跑完后任务收尾为“已取消”。"""

    job = session.get(BatchScoringJob, job_id)
    if job is None:
        raise ValueError("batch scoring job not found")
    if job.status in TERMINAL_JOB_STATUSES:
        return get_batch_scoring_job(session, job_id)
    now = utcnow()
    # 先改条目再锁任务行：与领取（条目 → 任务）的加锁顺序一致，避免死锁。
    session.execute(
        update(BatchScoringItem)
        .where(BatchScoringItem.job_id == job_id, BatchScoringItem.status == "pending")
        .values(status="canceled", finished_at=now)
        .execution_options(synchronize_session=False)
    )
    job = _lock_job(session, job_id)
    if job.status not in TERMINAL_JOB_STATUSES:
        job.cancel_requested_at = now
        job.status = "cancel_requested"
        session.flush()
        _recount(session, job_id)
        _finalize_if_settled(session, job_id, now)
    session.commit()
    return get_batch_scoring_job(session, job_id)


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
        item.heartbeat_at = None
        item.stall_count = 0
        item.not_before = None
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
    semantic_tasks = session.scalars(
        select(RuleScoringTask).where(
            RuleScoringTask.scoring_run_id == run.id,
            RuleScoringTask.judge_type == "semantic",
        )
    ).all()
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
        # Rule decision ledger: replayed vs freshly judged semantic rules, and
        # the provider usage this run actually paid for.
        "decision_ledger_reused": sum(
            1 for task in semantic_tasks if task.decision_reused
        ),
        "decision_ledger_judged": sum(
            1
            for task in semantic_tasks
            if not task.decision_reused and task.status != "skipped"
        ),
        "prompt_tokens": int(run.prompt_tokens or 0),
        "completion_tokens": int(run.completion_tokens or 0),
        "checker_failures": 0,
        "llm_failures": 0,
        "latency_ms": int(latency_ms),
        # Comparison artifacts are non-authoritative and are not inferred from
        # a single run.  A gate remains closed until a paired delta is supplied.
        "legacy_core_delta": None,
        "profile_key": run.business_profile_key or "legacy",
        "rubric_version_id": run.rubric_version_id or "legacy-unversioned",
    }


def _run_has_complete_scores(session, run):
    """A persisted run is reusable only when every rubric item formed a score."""
    run = session.scalar(
        select(ScoringRun)
        .where(ScoringRun.id == run.id)
        .options(selectinload(ScoringRun.items))
    )
    items = list(run.items)
    if not items:
        # Historical/manual runs may only carry an authoritative total.
        return run.final_total_score is not None
    return all(
        item.final_score is not None
        and item.auto_score_status not in ("invalid", "blocked")
        for item in items
    )


def _incomplete_scoring_error(rule_tasks):
    """Prefer an actionable, non-sensitive rule checkpoint error."""

    failed = [task for task in rule_tasks if task.status == "failed_exhausted"]
    provider_codes = [
        str(task.provider_error.get("code") or "").strip().upper()
        for task in failed
        if isinstance(task.provider_error, dict)
    ]
    provider_codes = [code for code in provider_codes if code]
    if "TOKEN_BUDGET_UNSATISFIABLE" in provider_codes:
        return IncompleteScoringResultError(
            "TOKEN_BUDGET_UNSATISFIABLE",
            (
                "评分输入超过模型上下文预算；请检查评分上下文、输出预留和证据块大小后重试。"
            ),
            failure_kind="llm",
        )
    if provider_codes:
        # PROVIDER_CIRCUIT_OPEN is a consequence of earlier failures, never
        # their cause.  Alphabetical order used to pick it over the real
        # PROVIDER_INVALID_REQUEST and sent operators to check connectivity.
        # Prefer the most frequent root-cause code; ties stay deterministic.
        root_codes = [
            code for code in provider_codes if code != "PROVIDER_CIRCUIT_OPEN"
        ] or provider_codes
        counts = Counter(root_codes)
        code = min(counts, key=lambda value: (-counts[value], value))
        if code == "TOKEN_BUDGET_EXCEEDED":
            message = (
                "本篇实际输入 token 已达到单篇上限（SCORING_MAX_INPUT_TOKENS_PER_PAPER），"
                "其余规则未发送；调高上限后重试，已成功的规则会直接复用。"
            )
        elif code == "PROVIDER_OUTPUT_TRUNCATED":
            message = (
                "模型输出达到输出 token 上限被截断（推理模型的思考过程可能耗尽了额度）；"
                "请调大该 AI 连接的输出 token 上限（max_output_tokens / max_tokens），"
                "或关闭思考、降低思考强度后重试。"
            )
        elif code == "PROVIDER_OUTPUT_REFUSED":
            message = (
                "模型按其安全策略拒绝评判部分规则；这些规则需要人工复核，"
                "或检查论文内容后换一个模型重试。"
            )
        else:
            message = f"评分模型调用失败（{code}）；请检查模型连接后重试。"
        return IncompleteScoringResultError(code, message, failure_kind="llm")
    if failed:
        return IncompleteScoringResultError(
            "RULE_EXECUTION_FAILED",
            "评分规则执行失败；请查看规则任务错误码或 Worker 安全日志后重试。",
            failure_kind="checker",
        )
    return IncompleteScoringResultError(
        "SCORING_RESULT_INCOMPLETE",
        "评分结果不完整：模型未形成全部评分项的有效分数。",
        failure_kind="checker",
    )


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
        if _run_has_complete_scores(session, previous):
            return {
                "status": "succeeded",
                "run_id": previous.id,
                "telemetry": _telemetry_for_run(session, previous, latency_ms=0),
            }
    if previous is not None and not job.rescore and _run_has_complete_scores(session, previous):
        return {
            "status": "skipped",
            "run_id": previous.id,
            "telemetry": _telemetry_for_run(session, previous, latency_ms=0),
        }
    started = monotonic()
    # Explicit rescoring must judge every rule again; ordinary first runs and
    # failure retries reuse validated decisions from the rule decision ledger.
    with bypass_ledger_reads() if job.rescore else nullcontext():
        if previous is not None:
            run = retry_score_paper(session, previous.id)
        else:
            run = score_paper(session, paper.id)
    if not _run_has_complete_scores(session, run):
        rule_tasks = session.scalars(
            select(RuleScoringTask).where(RuleScoringTask.scoring_run_id == run.id)
        ).all()
        raise _incomplete_scoring_error(rule_tasks)
    latency_ms = max(0, int((monotonic() - started) * 1000))
    return {
        "status": "succeeded",
        "run_id": run.id,
        "telemetry": _telemetry_for_run(session, run, latency_ms=latency_ms),
    }


def _classify_failure(exc):
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        direct_code = getattr(current, "code", None)
        failure_kind = getattr(current, "failure_kind", None)
        # Only our explicit application projection owns both fields.  Several
        # libraries expose an unrelated ``code`` attribute (SQLAlchemy uses
        # short documentation-link codes such as ``gkpj``); those must not be
        # mistaken for stable business/provider error codes.
        if (
            isinstance(direct_code, str)
            and direct_code.strip()
            and failure_kind in {"llm", "checker"}
        ):
            return direct_code.strip(), failure_kind
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


def _safe_failure_message(exc, code):
    """Return a bounded public diagnostic without SQL, payloads or document text."""

    if isinstance(exc, (IncompleteScoringResultError, ProviderCallError)):
        return str(exc)[:1000]
    if isinstance(exc, AIConnectionBindingError):
        changed = "密钥" if exc.code == "AI_CONNECTION_KEY_CHANGED" else "配置"
        return f"评分任务创建后，绑定的 AI 连接{changed}已变更；请重新创建评分任务后再评分。"
    if isinstance(exc, PlatformModelMissingError):
        return str(exc)
    exception_type = type(exc).__name__
    return f"评分执行失败（{code}; exception_type={exception_type}）"


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
        for code in LLM_FAILURE_CODES
    )
    checker_failure_count = sum(
        value
        for code, value in attempt_error_counts.items()
        if code not in LLM_FAILURE_CODES
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




def _failure_telemetry(failure_kind):
    return {
        "score_item_count": 0,
        "invalid_evidence_count": 0,
        "rule_decision_count": 0,
        "unauthorized_rule_count": 0,
        "manual_review": False,
        "cache_hits": 0,
        "cache_misses": 0,
        "checker_failures": 1 if failure_kind in ("checker", "worker") else 0,
        "llm_failures": 1 if failure_kind == "llm" else 0,
        "latency_ms": 0,
        "legacy_core_delta": None,
        "profile_key": "unknown",
        "rubric_version_id": "unknown",
    }


def _finalize_if_settled(session, job_id, now, *, touch=True):
    """所有条目都结束时收尾任务：状态、观察指标、批次阶段。调用方已在事务里。

    ``touch`` 表示这次调用伴随真实的执行活动（领取、写结果），顺带刷新任务心跳；
    巡检收敛时不刷新，否则卡住的任务在运维页上永远显得“刚有活动”。
    """

    job = _lock_job(session, job_id)
    if job is None or job.status in TERMINAL_JOB_STATUSES:
        return job
    if job.pending_count or job.running_count:
        if touch:
            job.heartbeat_at = now
        return job
    # 计数说已经结束：这时才加载全部条目（核对计数、汇总观察指标），每个任务一次。
    job = _lock_job(session, job_id, with_items=True)
    actual = Counter(item.status for item in job.items)
    if actual["pending"] or actual["running"]:
        # 计数漂移：以条目为准校正，不能在还有条目的时候收尾。
        _set_job_counts(job)
        return job
    _set_job_counts(job)
    if job.canceled_count:
        job.status = "canceled"
    elif job.failed_count:
        job.status = "completed_with_errors"
    elif job.succeeded_count or job.skipped_count:
        job.status = "completed"
    else:
        job.status = "failed"
    job.finished_at = now
    job.runner_token = None
    job.heartbeat_at = now
    metrics = _aggregate_metrics(job)
    job.metrics_snapshot = {
        "schema_version": METRICS_SCHEMA_VERSION,
        **metrics,
        "gate": evaluate_observation_policy(job.observation_policy, metrics),
    }
    batch = session.get(GradingBatch, job.grading_batch_id)
    # 只推进仍处在评分阶段的批次：巡检收敛历史任务时，批次可能早已被别的操作移走，
    # 非法转移会让这个任务每次巡检都失败、永远收不了尾。
    if batch is not None and batch.status == "scoring":
        if job.status == "completed":
            batch_state.apply_event(session, batch, "finish_scoring", outcome="scored")
        elif job.status == "completed_with_errors":
            batch_state.apply_event(
                session, batch, "finish_scoring", outcome="scored_with_errors"
            )
        elif job.status in ("canceled", "failed"):
            batch_state.apply_event(session, batch, "cancel")
    logger.info(
        "batch_scoring_job_finished job_id=%s status=%s succeeded=%s skipped=%s failed=%s canceled=%s",
        job.id,
        job.status,
        job.succeeded_count,
        job.skipped_count,
        job.failed_count,
        job.canceled_count,
    )
    return job


def _begin_item(session, item, now):
    """领取钩子：锁任务行，校验任务仍在执行，把条目置为 running。"""

    job = _lock_job(session, item.job_id)
    if job is None or job.status not in ("queued", "running"):
        # 待处理条目只应出现在活动任务里；取消中或已结束任务的条目直接取消。
        changed = session.execute(
            update(BatchScoringItem)
            .where(BatchScoringItem.id == item.id, BatchScoringItem.status == "pending")
            .values(status="canceled", finished_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
        if changed and job is not None:
            _shift_counts(session, job.id, pending=-1, canceled=1)
            _finalize_if_settled(session, job.id, now)
        return None
    attempt = int(item.attempt_count or 0) + 1
    changed = session.execute(
        update(BatchScoringItem)
        .where(
            BatchScoringItem.id == item.id,
            BatchScoringItem.status == "pending",
            BatchScoringItem.attempt_count == item.attempt_count,
        )
        .values(
            status="running",
            attempt_count=attempt,
            started_at=now,
            heartbeat_at=now,
            finished_at=None,
            not_before=None,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        # SQLite 没有行锁：另一个线程先领走了它。
        return None
    if attempt == 1:
        baseline = session.scalar(
            select(ScoringRun.id)
            .where(ScoringRun.paper_id == item.paper_id)
            .order_by(ScoringRun.created_at.desc(), ScoringRun.id.desc())
            .limit(1)
        )
        session.execute(
            update(BatchScoringItem)
            .where(BatchScoringItem.id == item.id)
            .values(baseline_scoring_run_id=baseline)
            .execution_options(synchronize_session=False)
        )
    _shift_counts(session, job.id, pending=-1, running=1)
    job.status = "running"
    job.started_at = job.started_at or now
    job.finished_at = None
    job.heartbeat_at = now
    batch = session.get(GradingBatch, job.grading_batch_id)
    if batch is not None and batch.status != "scoring":
        batch_state.apply_event(session, batch, "start_scoring")
    return ClaimedItem(
        kind=KIND,
        item_id=item.id,
        parent_id=job.id,
        source_key=item.source_key,
        attempt=attempt,
        payload={"paper_id": item.paper_id},
    )


def _finish_item(session_factory, claimed, *, result=None, error=None):
    """写一篇的结果。带围栏：条目已被巡检重置或被另一次执行领走时丢弃结果。"""

    with session_factory() as session:
        item = session.scalar(
            select(BatchScoringItem)
            .where(
                BatchScoringItem.id == claimed.item_id,
                BatchScoringItem.status == "running",
                BatchScoringItem.attempt_count == claimed.attempt,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if item is None:
            logger.warning(
                "work_item_result_discarded kind=%s item_id=%s attempt=%s",
                KIND,
                claimed.item_id,
                claimed.attempt,
            )
            return False
        now = utcnow()
        item.finished_at = now
        item.stall_count = 0
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
            status = "failed"
            item.status = status
            item.error_code = code
            item.error_message = _safe_failure_message(error, code)
            item.telemetry = _failure_telemetry(failure_kind)
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
        error_code = item.error_code
        session.flush()
        _shift_counts(session, item.job_id, running=-1, **{status: 1})
        _finalize_if_settled(session, item.job_id, now)
        session.commit()
    if error is None:
        logger.info(
            "work_item_finished kind=%s item_id=%s status=%s", KIND, claimed.item_id, status
        )
    else:
        logger.info(
            "work_item_failed kind=%s item_id=%s code=%s", KIND, claimed.item_id, error_code
        )
    return True


def _execute_item(session_factory, claimed, *, score_item=None, **_options):
    """评一篇：沿用规则检查点，超过平台时长上限的由下一次执行续评。"""

    score_item = score_item or _default_score_item
    try:
        with session_factory() as session:
            result = score_item(
                session,
                paper_id=claimed.payload["paper_id"],
                job_id=claimed.parent_id,
            )
    except Exception as exc:  # paper/provider failures are durable item results
        _finish_item(session_factory, claimed, error=exc)
    else:
        _finish_item(session_factory, claimed, result=result)


def _item_made_progress(session, item):
    """这次执行有没有写入新的检查点：本篇有规则在本次领取之后判完。"""

    if item.started_at is None:
        return False
    return bool(
        session.scalar(
            select(
                exists().where(
                    RuleScoringTask.scoring_run_id == ScoringRun.id,
                    ScoringRun.paper_id == item.paper_id,
                    RuleScoringTask.status == "succeeded",
                    RuleScoringTask.finished_at >= item.started_at,
                )
            )
        )
    )


def _abandon_item(session, item, now, *, progressed, exhausted):
    """巡检发现这次执行已死：续评，或连续无进展达到上限后标为失败。"""

    job = _lock_job(session, item.job_id)
    history = list(item.attempt_history or [])
    if exhausted:
        item.status = "failed"
        item.error_code = STALLED_ERROR_CODE
        item.error_message = STALLED_ERROR_MESSAGE
        item.telemetry = _failure_telemetry("worker")
        item.finished_at = now
        history.append(
            {
                "attempt": item.attempt_count,
                "status": "failed",
                "error_code": STALLED_ERROR_CODE,
                "failure_kind": "worker",
                "scoring_run_id": None,
                "latency_ms": 0,
            }
        )
    else:
        history.append(
            {
                "attempt": item.attempt_count,
                "status": "abandoned",
                "error_code": None,
                "progressed": bool(progressed),
                "scoring_run_id": None,
                "latency_ms": 0,
            }
        )
        if job is not None and job.status == "cancel_requested":
            item.status = "canceled"
            item.finished_at = now
        else:
            # 租约过期 ≠ 条目失败：重置为待处理，由下一次执行按检查点续评。
            item.status = "pending"
            item.finished_at = None
    item.heartbeat_at = None
    item.attempt_history = history
    session.flush()
    if job is not None:
        # 巡检路径很少走到，直接按条目重新计数，顺带校正历史数据的漂移。
        _recount(session, job.id)
        _finalize_if_settled(session, job.id, now, touch=False)


def _converge_jobs(session_factory, *, parent_ids=None, now=None, limit=200):
    """收敛活动任务：校正计数漂移、取消“取消中”任务剩下的待处理条目、收尾已结束的任务。"""

    now = now or utcnow()
    with session_factory() as session:
        statement = (
            select(BatchScoringJob.id)
            .where(BatchScoringJob.status.in_(ACTIVE_JOB_STATUSES))
            .order_by(BatchScoringJob.updated_at, BatchScoringJob.id)
            .limit(limit)
        )
        if parent_ids is not None:
            statement = statement.where(BatchScoringJob.id.in_(parent_ids))
        job_ids = list(session.scalars(statement))
    converged = 0
    for job_id in job_ids:
        with session_factory() as session:
            job = _lock_job(session, job_id, skip_locked=True)
            if job is None or job.status in TERMINAL_JOB_STATUSES:
                session.rollback()
                continue
            before = (job.pending_count, job.running_count, job.status)
            if job.status == "cancel_requested":
                session.execute(
                    update(BatchScoringItem)
                    .where(
                        BatchScoringItem.job_id == job_id,
                        BatchScoringItem.status == "pending",
                    )
                    .values(status="canceled", finished_at=now)
                    .execution_options(synchronize_session=False)
                )
            counts = _recount(session, job_id)
            finalized = _finalize_if_settled(session, job_id, now, touch=False)
            after = (counts["pending"], counts["running"], finalized.status if finalized else None)
            session.commit()
            if after != before:
                converged += 1
    return converged


def _job_source(session, job_id):
    return session.scalar(
        select(BatchScoringItem.source_key).where(BatchScoringItem.job_id == job_id).limit(1)
    )


BATCH_SCORING_KIND = register(
    WorkKind(
        name=KIND,
        priority=20,
        model=BatchScoringItem,
        parent_model=BatchScoringJob,
        parent_column="job_id",
        begin=_begin_item,
        execute=_execute_item,
        abandon=_abandon_item,
        made_progress=_item_made_progress,
        converge=_converge_jobs,
        parent_source=_job_source,
    )
)


def run_batch_scoring_job(
    session_factory,
    *,
    job_id,
    score_item=None,
    executor_factory=ThreadPoolExecutor,
):
    """在当前进程里把一个任务跑完（本地 /run 入口与测试用）。

    与 Vercel、worker 走同一个领取函数，只是把领取限制在这个任务内；名额检查不变，
    来源满了的线程会提前退出，剩下的条目由仍在运行的线程继续领取。
    """

    from backend.app.services.work_queue.runner import execute_next
    from backend.app.services.work_queue.sweep import sweep_stale_items

    with session_factory() as session:
        job = session.get(BatchScoringJob, job_id)
        if job is None:
            raise ValueError("batch scoring job not found")
        lanes = max(1, int(job.max_workers or 1))
    # 先接手已死的执行（租约过期的条目重置后续评）并收敛“取消中”的任务。
    sweep_stale_items(session_factory, parent_id=job_id, wake=False)

    def lane():
        while execute_next(session_factory, parent_id=job_id, score_item=score_item) is not None:
            pass

    with executor_factory(max_workers=lanes) as executor:
        futures = [executor.submit(lane) for _ in range(lanes)]
        for future in futures:
            future.result()
    with session_factory() as session:
        return get_batch_scoring_job(session, job_id)


__all__ = [
    "BATCH_SCORING_KIND",
    "batch_source_key",
    "cancel_batch_scoring_job",
    "create_batch_scoring_job",
    "default_observation_policy",
    "evaluate_observation_policy",
    "get_batch_scoring_job",
    "get_latest_batch_scoring_job",
    "list_attention_batch_scoring_jobs",
    "retry_batch_scoring_job",
    "run_batch_scoring_job",
    "validate_observation_policy",
]
