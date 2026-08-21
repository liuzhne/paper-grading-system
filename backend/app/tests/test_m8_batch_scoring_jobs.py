from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Lock
import importlib
import time

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from backend.app.core.config import settings
from backend.app.db import models
from backend.app.services.dev_user import ensure_dev_user


ROOT = Path(__file__).resolve().parents[3]


def _policy(*, minimum_sample_size: int = 1):
    return {
        "schema_version": "core-cutover-observation-policy@1",
        "minimum_sample_size": minimum_sample_size,
        "observation_window": {"minimum_completed_items": minimum_sample_size},
        "thresholds": {
            "max_abs_legacy_core_delta": "0.50",
            "max_invalid_evidence_rate": "0.01",
            "max_unauthorized_rule_rate": "0",
            "max_manual_review_rate": "0.20",
            "min_cache_hit_rate": "0.50",
            "max_checker_failure_rate": "0.01",
            "max_llm_failure_rate": "0.01",
            "max_retry_rate": "0.10",
            "max_p95_latency_ms": "1000",
        },
        "fallback_tolerance": {"max_abs_score_delta": "0.50"},
    }


def _seed_batch(session, *, count: int, name: str):
    user = ensure_dev_user(session)
    rubric = models.Rubric(
        name=f"{name} rubric",
        version="v1",
        total_score=10,
        status="published",
        created_by=user.id,
        owner_id=user.id,
    )
    batch = models.GradingBatch(
        name=name,
        rubric=rubric,
        status="draft",
        created_by=user.id,
        owner_id=user.id,
    )
    session.add_all([rubric, batch])
    session.flush()
    papers = []
    for index in range(count):
        paper = models.Paper(
            batch=batch,
            file_name=f"paper-{index:03d}.docx",
            file_path=f"/private/tmp/paper-{index:03d}.docx",
            parsed_text_path=f"/private/tmp/paper-{index:03d}.json",
            status="parsed",
        )
        session.add(paper)
        papers.append(paper)
    session.commit()
    return batch.id, rubric.id, [paper.id for paper in papers]


def _jobs_module():
    return importlib.import_module("backend.app.services.batch_scoring.jobs")


def test_0017_migration_and_models_define_durable_job_item_relationships(
    monkeypatch,
    tmp_path,
):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "m8-jobs-migration.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        assert {"batch_scoring_jobs", "batch_scoring_items"}.issubset(
            inspector.get_table_names()
        )
        job_columns = {
            item["name"] for item in inspector.get_columns("batch_scoring_jobs")
        }
        assert {
            "generation",
            "status",
            "max_workers",
            "observation_policy",
            "observation_policy_hash",
            "metrics_snapshot",
        }.issubset(job_columns)
        assert hasattr(models, "BatchScoringJob")
        assert hasattr(models, "BatchScoringItem")
    finally:
        engine.dispose()


def test_api_start_is_idempotent_and_cancel_retry_progress_is_persistent(client):
    with client.session_factory() as session:
        batch_id, _rubric_id, paper_ids = _seed_batch(
            session, count=3, name="PGS-6 API"
        )

    created = client.post(
        f"/api/batches/{batch_id}/score-jobs",
        json={
            "rescore": False,
            "max_workers": 2,
            "observation_policy": _policy(minimum_sample_size=3),
        },
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["status"] == "queued"
    assert payload["total_items"] == 3
    assert {item["paper_id"] for item in payload["items"]} == set(paper_ids)

    duplicate = client.post(
        f"/api/batches/{batch_id}/score-jobs",
        json={
            "rescore": False,
            "max_workers": 2,
            "observation_policy": _policy(minimum_sample_size=3),
        },
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["id"] == payload["id"]

    canceled = client.post(f"/api/batch-scoring-jobs/{payload['id']}/cancel")
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "canceled"
    assert canceled.json()["canceled_count"] == 3

    retried = client.post(f"/api/batch-scoring-jobs/{payload['id']}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "queued"
    assert retried.json()["pending_count"] == 3
    assert all(item["attempt_count"] == 0 for item in retried.json()["items"])


def test_api_run_uses_persistent_checkpoints_and_fails_gate_closed(client):
    with client.session_factory() as session:
        batch_id, rubric_id, paper_ids = _seed_batch(
            session, count=3, name="PGS-6 API run"
        )
        for paper_id in paper_ids:
            session.add(
                models.ScoringRun(
                    paper_id=paper_id,
                    rubric_id=rubric_id,
                    status="scored",
                    ai_total_score=8,
                    final_total_score=8,
                    grade="通过",
                    need_manual_review=False,
                )
            )
        session.commit()

    created = client.post(
        f"/api/batches/{batch_id}/score-jobs",
        json={
            "rescore": False,
            # The shared client fixture is an in-memory SQLite StaticPool;
            # it deliberately uses one DBAPI connection and is not a threaded
            # concurrency fixture.  File-SQLite concurrency is covered by the
            # dedicated 100-item soak below.
            "max_workers": 1,
            "observation_policy": _policy(minimum_sample_size=3),
        },
    )
    assert created.status_code == 201, created.text
    executed = client.post(
        f"/api/batch-scoring-jobs/{created.json()['id']}/run"
    )
    assert executed.status_code == 200, executed.text
    payload = executed.json()
    assert payload["status"] == "completed"
    assert payload["skipped_count"] == 3
    assert payload["pending_count"] == 0
    assert all(len(item["attempt_history"]) == 1 for item in payload["items"])
    assert payload["metrics_snapshot"]["gate"]["ready_for_gate"] is False
    assert (
        payload["metrics_snapshot"]["gate"][
            "production_default_switch_authorized"
        ]
        is False
    )


def test_observation_policy_fails_closed_without_complete_comparison_identity():
    jobs = _jobs_module()
    numeric_policy = _policy()
    numeric_policy["thresholds"]["max_abs_legacy_core_delta"] = 0.5
    numeric_policy["thresholds"]["max_invalid_evidence_rate"] = 0.01
    numeric_policy["fallback_tolerance"]["max_abs_score_delta"] = 0.5
    normalized = jobs.validate_observation_policy(numeric_policy)
    assert normalized["thresholds"]["max_abs_legacy_core_delta"] == "0.5"
    assert normalized["thresholds"]["max_invalid_evidence_rate"] == "0.01"
    assert normalized["fallback_tolerance"]["max_abs_score_delta"] == "0.5"
    assert len(jobs.canonical_sha256(normalized)) == 64
    metrics = {
        "sample_size": 10,
        "completed_items": 10,
        "invalid_evidence_rate": "0",
        "unauthorized_rule_rate": "0",
        "manual_review_rate": "0.10",
        "cache_hit_rate": "0.80",
        "checker_failure_rate": "0",
        "llm_failure_rate": "0",
        "retry_rate": "0",
        "p95_latency_ms": "100",
        "legacy_core_delta": {"available": False, "values": []},
    }
    report = jobs.evaluate_observation_policy(_policy(), metrics)
    assert report["ready_for_gate"] is False
    assert report["rollback_required"] is True
    assert report["signals"]["legacy_core_delta"]["status"] == "unavailable"
    assert report["production_default_switch_authorized"] is False

    metrics["legacy_core_delta"] = {"available": True, "values": ["0.2", "-0.1"]}
    passed = jobs.evaluate_observation_policy(_policy(), metrics)
    assert passed["ready_for_gate"] is True
    assert passed["rollback_required"] is False
    assert passed["production_default_switch_authorized"] is False


def test_hundred_item_soak_is_bounded_checkpointed_and_run_unique(tmp_path):
    jobs = _jobs_module()
    engine = create_engine(
        "sqlite+pysqlite:///%s" % (tmp_path / "m8-100-soak.db"),
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as session:
        batch_id, rubric_id, paper_ids = _seed_batch(
            session, count=100, name="PGS-6 100 soak"
        )
        run_by_paper = {}
        for paper_id in paper_ids:
            run = models.ScoringRun(
                paper_id=paper_id,
                rubric_id=rubric_id,
                status="scored",
                ai_total_score=8,
                final_total_score=8,
                grade="通过",
                need_manual_review=False,
            )
            session.add(run)
            session.flush()
            run_by_paper[paper_id] = run.id
        session.commit()
        job, created = jobs.create_batch_scoring_job(
            session,
            batch_id=batch_id,
            rescore=False,
            max_workers=4,
            observation_policy=_policy(minimum_sample_size=100),
            actor_id=settings.DEFAULT_DEV_USER_ID,
        )
        assert created is True
        job_id = job.id

    active = 0
    maximum_active = 0
    lock = Lock()

    def fake_score(_session, *, paper_id, job_id):
        nonlocal active, maximum_active
        assert job_id
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.003)
        with lock:
            active -= 1
        return {
            "run_id": run_by_paper[paper_id],
            "telemetry": {
                "score_item_count": 1,
                "invalid_evidence_count": 0,
                "rule_decision_count": 1,
                "unauthorized_rule_count": 0,
                "manual_review": False,
                "cache_hits": 1,
                "cache_misses": 0,
                "checker_failures": 0,
                "llm_failures": 0,
                "latency_ms": 20,
                "legacy_core_delta": "0.1",
                "profile_key": "thesis",
                "rubric_version_id": "test-version",
            },
        }

    result = jobs.run_batch_scoring_job(
        factory,
        job_id=job_id,
        score_item=fake_score,
        executor_factory=ThreadPoolExecutor,
    )
    assert 1 < maximum_active <= 4
    assert result.status == "completed"
    assert result.succeeded_count == 100
    assert result.failed_count == 0
    assert result.metrics_snapshot["gate"]["ready_for_gate"] is True
    assert result.metrics_snapshot["gate"]["production_default_switch_authorized"] is False

    with factory() as session:
        persisted = jobs.get_batch_scoring_job(session, job_id)
        run_ids = [item.scoring_run_id for item in persisted.items]
        assert len(run_ids) == len(set(run_ids)) == 100
        assert all(item.status == "succeeded" for item in persisted.items)
    engine.dispose()


def test_partial_timeout_rate_limit_and_checker_failures_retry_only_failed_items(tmp_path):
    jobs = _jobs_module()
    engine = create_engine(
        "sqlite+pysqlite:///%s" % (tmp_path / "m8-fault-retry.db"),
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as session:
        batch_id, rubric_id, paper_ids = _seed_batch(
            session, count=4, name="PGS-6 fault retry"
        )
        run_by_paper = {}
        for paper_id in paper_ids:
            run = models.ScoringRun(
                paper_id=paper_id,
                rubric_id=rubric_id,
                status="scored",
                ai_total_score=8,
                final_total_score=8,
                grade="通过",
                need_manual_review=False,
            )
            session.add(run)
            session.flush()
            run_by_paper[paper_id] = run.id
        job, _ = jobs.create_batch_scoring_job(
            session,
            batch_id=batch_id,
            rescore=False,
            max_workers=2,
            observation_policy=_policy(minimum_sample_size=4),
            actor_id=settings.DEFAULT_DEV_USER_ID,
        )
        job_id = job.id

    calls = {paper_id: 0 for paper_id in paper_ids}

    def fault_score(_session, *, paper_id, job_id):
        calls[paper_id] += 1
        if calls[paper_id] == 1:
            if paper_id == paper_ids[0]:
                raise TimeoutError("provider timeout")
            if paper_id == paper_ids[1]:
                raise RuntimeError("429 rate limit")
            if paper_id == paper_ids[2]:
                raise RuntimeError("checker registry failure")
        return {
            "run_id": run_by_paper[paper_id],
            "telemetry": {
                "score_item_count": 1,
                "invalid_evidence_count": 0,
                "rule_decision_count": 1,
                "unauthorized_rule_count": 0,
                "manual_review": False,
                "cache_hits": 1,
                "cache_misses": 0,
                "checker_failures": 0,
                "llm_failures": 0,
                "latency_ms": 10,
                "legacy_core_delta": "0.1",
                "profile_key": "thesis",
                "rubric_version_id": "test-version",
            },
        }

    first = jobs.run_batch_scoring_job(
        factory, job_id=job_id, score_item=fault_score
    )
    assert first.status == "completed_with_errors"
    assert first.succeeded_count == 1
    assert first.failed_count == 3
    assert first.metrics_snapshot["error_counts"] == {
        "checker_failure": 1,
        "rate_limited": 1,
        "timeout": 1,
    }
    assert first.metrics_snapshot["gate"]["ready_for_gate"] is False

    with factory() as session:
        retried = jobs.retry_batch_scoring_job(session, job_id)
        assert retried.pending_count == 3
        assert retried.succeeded_count == 1

    final = jobs.run_batch_scoring_job(
        factory, job_id=job_id, score_item=fault_score
    )
    assert final.status == "completed"
    assert final.succeeded_count == 4
    assert sorted(item.attempt_count for item in final.items) == [1, 2, 2, 2]
    assert calls[paper_ids[3]] == 1
    assert all(calls[paper_id] == 2 for paper_id in paper_ids[:3])
    assert len({item.scoring_run_id for item in final.items}) == 4
    assert sorted(len(item.attempt_history) for item in final.items) == [1, 2, 2, 2]
    assert final.metrics_snapshot["attempt_error_counts"] == {
        "checker_failure": 1,
        "rate_limited": 1,
        "timeout": 1,
    }
    assert final.metrics_snapshot["llm_failure_rate"] == "0.2857142857142857142857142857"
    assert final.metrics_snapshot["checker_failure_rate"] == "0.1428571428571428571428571429"
    assert final.metrics_snapshot["gate"]["ready_for_gate"] is False
    engine.dispose()


def test_expired_running_lease_resumes_checkpoint_without_parallel_owner(tmp_path):
    jobs = _jobs_module()
    engine = create_engine(
        "sqlite+pysqlite:///%s" % (tmp_path / "m8-stale-resume.db"),
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as session:
        batch_id, rubric_id, paper_ids = _seed_batch(
            session, count=1, name="PGS-6 stale resume"
        )
        run = models.ScoringRun(
            paper_id=paper_ids[0],
            rubric_id=rubric_id,
            status="scored",
            ai_total_score=8,
            final_total_score=8,
            grade="通过",
            need_manual_review=False,
        )
        session.add(run)
        session.flush()
        run_id = run.id
        job, _ = jobs.create_batch_scoring_job(
            session,
            batch_id=batch_id,
            rescore=False,
            max_workers=1,
            observation_policy=_policy(),
            actor_id=settings.DEFAULT_DEV_USER_ID,
        )
        item = job.items[0]
        job.status = "running"
        job.runner_token = "abandoned-runner"
        job.heartbeat_at = datetime(2020, 1, 1)
        item.status = "running"
        item.attempt_count = 1
        item.baseline_scoring_run_id = run_id
        session.commit()
        job_id = job.id

    called = 0

    def resumed_score(_session, *, paper_id, job_id):
        nonlocal called
        called += 1
        return {
            "run_id": run_id,
            "telemetry": {
                "score_item_count": 1,
                "invalid_evidence_count": 0,
                "rule_decision_count": 1,
                "unauthorized_rule_count": 0,
                "manual_review": False,
                "cache_hits": 1,
                "cache_misses": 0,
                "checker_failures": 0,
                "llm_failures": 0,
                "latency_ms": 5,
                "legacy_core_delta": "0.1",
                "profile_key": "thesis",
                "rubric_version_id": "test-version",
            },
        }

    recovered = jobs.run_batch_scoring_job(
        factory, job_id=job_id, score_item=resumed_score
    )
    assert recovered.status == "completed"
    assert recovered.succeeded_count == 1
    assert recovered.items[0].attempt_count == 2
    assert recovered.items[0].scoring_run_id == run_id
    assert called == 1
    assert recovered.runner_token is None
    engine.dispose()


def test_static_web_exposes_persistent_batch_job_progress_and_gate_signals():
    html = (ROOT / "frontend/web/index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend/web/assets/app.js").read_text(encoding="utf-8")
    for element_id in (
        "batch-score-job-panel",
        "batch-score-job-status",
        "batch-score-job-errors",
        "batch-score-job-signals",
    ):
        assert f'id="{element_id}"' in html
    for marker in (
        "/score-jobs",
        "/batch-scoring-jobs/",
        "/cancel",
        "/retry",
        "production_default_switch_authorized",
    ):
        assert marker in script
