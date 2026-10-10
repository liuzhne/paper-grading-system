"""统一执行模型（A1）验收：数据库即队列，Vercel 与 worker 只差“谁来叫醒”。

同一套用例同时驱动两种叫醒：Vercel 用一个先进先出的假队列投递 ``{source_key}``
消息，worker 直接循环调用 ``run_worker_cycle``。领取、名额、轮转、巡检全部走真实代码。

设置 ``PGS_TEST_POSTGRES_URL``（指向本机一个可清空的空库）时，并发领取用例还会在真实
PostgreSQL 上跑一遍，验证 ``FOR UPDATE SKIP LOCKED`` 与来源行锁。
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
import importlib
import os
import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from backend.app.core.config import settings
from backend.app.db import models
from backend.app.db.models import utcnow
from backend.app.services.dev_user import ensure_dev_user


jobs = importlib.import_module("backend.app.services.batch_scoring.jobs")
claim = importlib.import_module("backend.app.services.work_queue.claim")
messages = importlib.import_module("backend.app.services.work_queue.messages")
runner = importlib.import_module("backend.app.services.work_queue.runner")
sweep = importlib.import_module("backend.app.services.work_queue.sweep")
wake = importlib.import_module("backend.app.services.work_queue.wake")
state = importlib.import_module("backend.app.services.work_queue.state")


# ---- 夹具 -----------------------------------------------------------------


def _sqlite_factory(tmp_path):
    engine = create_engine(
        "sqlite+pysqlite:///%s" % (tmp_path / "work-queue.db"),
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    models.Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _postgres_factory():
    url = os.getenv("PGS_TEST_POSTGRES_URL", "").strip()
    if not url:
        pytest.skip("PGS_TEST_POSTGRES_URL is not set")
    if (make_url(url).host or "") not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("PGS_TEST_POSTGRES_URL must point at a local, disposable database")
    engine = create_engine(url)
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autocommit=False, autoflush=False)


@pytest.fixture
def factory(tmp_path):
    engine, session_factory = _sqlite_factory(tmp_path)
    yield session_factory
    engine.dispose()


@pytest.fixture(params=["sqlite", "postgres"])
def concurrent_factory(request, tmp_path):
    if request.param == "sqlite":
        engine, session_factory = _sqlite_factory(tmp_path)
    else:
        engine, session_factory = _postgres_factory()
    yield session_factory
    if request.param == "postgres":
        models.Base.metadata.drop_all(engine)
    engine.dispose()


def _connection(session, *, max_concurrency):
    user = ensure_dev_user(session)
    organization = models.Organization(name="work queue org %s" % models.new_id()[:8])
    session.add(organization)
    session.flush()
    connection = models.AIConnection(
        organization_id=organization.id,
        owner_id=user.id,
        name="limited %s" % models.new_id()[:8],
        provider_type="openai_compatible",
        base_url="https://api.example.test/v1",
        model_name="test-model",
        provider_options={"max_concurrency": max_concurrency},
        api_key_ciphertext="cipher",
        api_key_nonce="nonce",
        api_key_tag="tag",
        key_last4="1234",
    )
    session.add(connection)
    session.flush()
    return connection.id


def _job(session_factory, *, papers, name, connection_id=None, actor=None):
    """一个已解析的批次及其评分任务；每篇预先建好一条评分记录供假评分器引用。"""

    with session_factory() as session:
        user = ensure_dev_user(session)
        rubric = models.Rubric(
            name="%s rubric" % name,
            version=models.new_id()[:8],
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
            ai_connection_id=connection_id,
        )
        session.add_all([rubric, batch])
        session.flush()
        run_by_paper = {}
        for index in range(papers):
            paper = models.Paper(
                batch=batch,
                file_name="%s-%02d.docx" % (name, index),
                file_path="/private/tmp/%s-%02d.docx" % (name, index),
                parsed_text_path="/private/tmp/%s-%02d.json" % (name, index),
                status="parsed",
            )
            session.add(paper)
            session.flush()
        session.commit()
        job, _ = jobs.create_batch_scoring_job(
            session,
            batch_id=batch.id,
            rescore=True,
            max_workers=2,
            observation_policy=None,
            actor_id=actor or settings.DEFAULT_DEV_USER_ID,
        )
        for item in job.items:
            run = models.ScoringRun(
                paper_id=item.paper_id,
                rubric_id=rubric.id,
                status="scored",
                ai_total_score=8,
                final_total_score=8,
                grade="通过",
                need_manual_review=False,
            )
            session.add(run)
            session.flush()
            run_by_paper[item.paper_id] = run.id
        session.commit()
        return job.id, run_by_paper


def _telemetry():
    return {
        "score_item_count": 1,
        "invalid_evidence_count": 0,
        "rule_decision_count": 1,
        "unauthorized_rule_count": 0,
        "manual_review": False,
        "cache_hits": 0,
        "cache_misses": 1,
        "checker_failures": 0,
        "llm_failures": 0,
        "latency_ms": 5,
        "legacy_core_delta": None,
        "profile_key": "thesis",
        "rubric_version_id": "test",
    }


class FakeScorer:
    """记录同时在评的篇数；返回预先建好的评分记录。"""

    def __init__(self, runs, *, hold=0.0):
        self.runs = runs
        self.hold = hold
        self.active = 0
        self.peak = 0
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, _session, *, paper_id, job_id):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append(paper_id)
        try:
            if self.hold:
                time.sleep(self.hold)
            return {"run_id": self.runs[paper_id], "telemetry": _telemetry()}
        finally:
            with self.lock:
                self.active -= 1


class FakeQueue:
    """Vercel 队列的替身：记录叫醒消息，按先进先出投递给真实的消息处理函数。"""

    def __init__(self, monkeypatch):
        self.sent = []
        self.keys = set()
        monkeypatch.setenv("BATCH_SCORING_DISPATCH", "vercel_queue")
        monkeypatch.setattr(wake, "_send", self._send)

    def _send(self, payload, *, idempotency_key, delay=None):
        if idempotency_key in self.keys:
            return None  # 与服务端相同：同一个幂等键只投一次
        self.keys.add(idempotency_key)
        self.sent.append({"payload": payload, "key": idempotency_key, "delay": delay})
        return "message-%s" % len(self.sent)

    def wakes(self):
        return [value for value in self.sent if "source_key" in value["payload"]]

    def drain(self, session_factory, *, score_item, limit=200):
        delivered = 0
        while self.sent and delivered < limit:
            message = self.sent.pop(0)
            if "sweep" in message["payload"]:
                continue  # 巡检链单独测；这里只投叫醒
            delivered += 1
            original = runner.execute_next
            try:
                runner.execute_next = lambda factory, **options: original(
                    factory, score_item=score_item, **options
                )
                messages.handle_wake_payload(session_factory, message["payload"])
            finally:
                runner.execute_next = original
        return delivered


def _wake_after_create(session_factory, job_id):
    """与建任务接口相同：按空闲名额叫醒。"""

    with session_factory() as session:
        source_key = jobs._job_source(session, job_id)
        return wake.wake_for_capacity(session, source_key, reason="create", token=job_id)


def _job_state(session_factory, job_id):
    with session_factory() as session:
        job = jobs.get_batch_scoring_job(session, job_id)
        job.batch  # 读出批次，供会话关闭后断言其阶段
        return job.status, sorted(item.status for item in job.items), job


# ---- 两种叫醒，同一套结果 ------------------------------------------------------


@pytest.mark.parametrize("driver", ["vercel", "worker"])
def test_vercel_and_worker_wakes_drive_jobs_to_the_same_result(factory, monkeypatch, driver):
    with factory() as session:
        connection_id = _connection(session, max_concurrency=1)
        session.commit()
    first, runs = _job(factory, papers=3, name="drive-a", connection_id=connection_id)
    second, more_runs = _job(factory, papers=2, name="drive-b", connection_id=connection_id)
    scorer = FakeScorer({**runs, **more_runs})

    if driver == "vercel":
        queue = FakeQueue(monkeypatch)
        for job_id in (first, second):
            _wake_after_create(factory, job_id)
        # 建任务时按空位叫醒：此刻没人在跑，两个任务各发一条；多出的那条取不到活就退出。
        assert [value["payload"] for value in queue.wakes()] == [
            {"source_key": "connection:%s" % connection_id}
        ] * 2
        delivered = queue.drain(factory, score_item=scorer)
        # 其余每篇靠上一篇结束时的接力叫醒，消息数与篇数同一量级。
        assert 5 <= delivered <= 7
    else:
        while runner.run_worker_cycle(factory, score_item=scorer):
            pass

    for job_id in (first, second):
        status, item_statuses, job = _job_state(factory, job_id)
        assert status == "completed"
        assert set(item_statuses) == {"succeeded"}
        assert all(item.attempt_count == 1 for item in job.items)
        assert (job.pending_count, job.running_count) == (0, 0)
        assert job.succeeded_count == job.total_items
    assert scorer.peak == 1
    # 两个任务交替领取：第二个任务不必等第一个任务评完。
    order = [paper for paper in scorer.calls]
    assert order.index(next(iter(more_runs))) < order.index(list(runs)[-1])


def test_concurrent_claims_never_exceed_the_connection_limit(concurrent_factory):
    session_factory = concurrent_factory
    with session_factory() as session:
        connection_id = _connection(session, max_concurrency=2)
        session.commit()
    job_a, runs_a = _job(session_factory, papers=6, name="limit-a", connection_id=connection_id)
    job_b, runs_b = _job(session_factory, papers=6, name="limit-b", connection_id=connection_id, actor=None)
    scorer = FakeScorer({**runs_a, **runs_b}, hold=0.05)
    deadline = time.monotonic() + 60

    def lane():
        while time.monotonic() < deadline:
            if runner.run_worker_cycle(session_factory, score_item=scorer):
                continue
            with session_factory() as session:
                remaining = session.query(models.BatchScoringItem).filter(
                    models.BatchScoringItem.status.in_(("pending", "running"))
                ).count()
            if not remaining:
                return
            time.sleep(0.01)

    threads = [threading.Thread(target=lane) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert 1 <= scorer.peak <= 2
    assert len(scorer.calls) == len(set(scorer.calls)) == 12
    for job_id in (job_a, job_b):
        status, item_statuses, job = _job_state(session_factory, job_id)
        assert status == "completed"
        assert set(item_statuses) == {"succeeded"}
        assert (job.pending_count, job.running_count, job.succeeded_count) == (0, 0, 6)


# ---- 叫醒、接力与巡检 ----------------------------------------------------------


def test_relay_wakes_the_source_when_an_item_finishes_with_work_left(factory, monkeypatch):
    with factory() as session:
        connection_id = _connection(session, max_concurrency=1)
        session.commit()
    _job_id, runs = _job(factory, papers=3, name="relay", connection_id=connection_id)
    queue = FakeQueue(monkeypatch)
    source_key = "connection:%s" % connection_id
    queue.sent.append({"payload": {"source_key": source_key}, "key": "seed", "delay": None})

    assert queue.drain(factory, score_item=FakeScorer(runs), limit=1) == 1
    # 一篇结束、还有 2 篇待处理、名额空出 1 个：接力叫醒 1 条。
    relays = queue.wakes()
    assert [value["payload"] for value in relays] == [{"source_key": source_key}]
    assert "relay" in relays[0]["key"]


def test_a_lost_wake_is_recovered_by_the_sweep(factory, monkeypatch):
    job_id, runs = _job(factory, papers=2, name="lost wake")
    queue = FakeQueue(monkeypatch)
    _wake_after_create(factory, job_id)
    assert len(queue.wakes()) == 2
    queue.sent.clear()  # 两条叫醒都被平台丢了

    report = sweep.run_global_sweep(factory)
    # 有待处理、有空位、没人在跑：补发叫醒。
    assert report.rung == 2
    queue.drain(factory, score_item=FakeScorer(runs))
    status, item_statuses, _unused = _job_state(factory, job_id)
    assert (status, item_statuses) == ("completed", ["succeeded", "succeeded"])
    with factory() as session:
        assert state.read_state(session, state.LAST_SWEEP) is not None


def test_the_sweep_chain_schedules_one_message_per_slot_and_revives_when_stale(factory, monkeypatch):
    queue = FakeQueue(monkeypatch)
    now = datetime(2030, 1, 1, 0, 0, 30)
    slot = wake.sweep_slot(now)

    with monkeypatch.context() as patched:
        patched.setattr(messages, "utcnow", lambda: now)
        messages.handle_wake_payload(factory, {"sweep": slot})
        # 平台重投同一条巡检消息：下一个槽的幂等键相同，不会多出一条链。
        messages.handle_wake_payload(factory, {"sweep": slot})
    sweeps = [value for value in queue.sent if "sweep" in value["payload"]]
    assert [value["payload"] for value in sweeps] == [{"sweep": slot + 1}]
    assert 0 < sweeps[0]["delay"] <= 120

    with factory() as session:
        assert state.read_state(session, state.LAST_SWEEP) == now
    # 刚巡检过：不补投。
    assert sweep.ensure_sweep_chain(factory, now=now + timedelta(seconds=200)) is False
    # 超过两个周期没巡检：补投一条；同一周期内只补一次。
    later = now + timedelta(minutes=5)
    assert sweep.ensure_sweep_chain(factory, now=later) is True
    assert sweep.ensure_sweep_chain(factory, now=later + timedelta(seconds=10)) is False


def test_worker_mode_sends_no_wake_messages(factory, monkeypatch):
    sent = []
    monkeypatch.setenv("BATCH_SCORING_DISPATCH", "worker")
    monkeypatch.setattr(wake, "_send", lambda *args, **kwargs: sent.append(args))
    job_id, _runs = _job(factory, papers=2, name="worker no wake")
    assert _wake_after_create(factory, job_id) == 0
    assert sweep.ensure_sweep_chain(factory) is False
    assert sent == []


# ---- 租约、围栏与收敛 ----------------------------------------------------------


def test_a_late_result_from_a_dead_execution_is_discarded(factory):
    job_id, runs = _job(factory, papers=1, name="fencing")
    first = claim.claim_next_item(factory)
    assert first.attempt == 1
    # 这次执行被判为已死并重置；随后另一次执行领走了同一篇。
    later = utcnow() + timedelta(minutes=10)
    assert sweep.sweep_stale_items(factory, now=later).recovered == 1
    second = claim.claim_next_item(factory, now=later)
    assert (second.item_id, second.attempt) == (first.item_id, 2)

    paper_id = first.payload["paper_id"]
    assert jobs._finish_item(factory, first, result={"run_id": runs[paper_id]}) is False
    with factory() as session:
        item = session.get(models.BatchScoringItem, first.item_id)
        assert (item.status, item.attempt_count) == ("running", 2)
    assert jobs._finish_item(factory, second, result={"run_id": runs[paper_id]}) is True
    status, item_statuses, _unused = _job_state(factory, job_id)
    assert (status, item_statuses) == ("completed", ["succeeded"])


def test_heartbeat_keeps_the_lease_and_stops_after_the_item_moves_on(factory):
    _job_id, _runs = _job(factory, papers=1, name="heartbeat")
    claimed = claim.claim_next_item(factory)
    assert runner.heartbeat(factory, claimed) is True
    later = utcnow() + timedelta(minutes=10)
    sweep.sweep_stale_items(factory, now=later)
    assert runner.heartbeat(factory, claimed) is False


def test_cancel_stops_pending_items_at_once_and_closes_after_the_running_one(factory):
    job_id, runs = _job(factory, papers=3, name="cancel while running")
    claimed = claim.claim_next_item(factory)
    with factory() as session:
        canceled = jobs.cancel_batch_scoring_job(session, job_id)
        assert canceled.status == "cancel_requested"
        assert (canceled.pending_count, canceled.running_count, canceled.canceled_count) == (0, 1, 2)
        assert canceled.batch.status == "scoring"
    assert claim.claim_next_item(factory) is None

    jobs._execute_item(factory, claimed, score_item=FakeScorer(runs))
    status, item_statuses, job = _job_state(factory, job_id)
    assert status == "canceled"
    assert item_statuses == ["canceled", "canceled", "succeeded"]
    # 取消回落到按已有结果推导出的阶段（这几篇都有评分记录），不再停在“评分中”。
    assert job.batch.status == "scored"


def test_the_sweep_corrects_counter_drift_and_closes_a_settled_job(factory):
    job_id, runs = _job(factory, papers=2, name="drift")
    while runner.run_worker_cycle(factory, score_item=FakeScorer(runs)):
        pass
    with factory() as session:
        job = session.get(models.BatchScoringJob, job_id)
        # 模拟一个停在“评分中”、计数也不对的历史任务。
        job.status = "running"
        job.batch.status = "scoring"
        job.finished_at = None
        job.succeeded_count = 0
        job.pending_count = 2
        session.commit()

    report = sweep.sweep_stale_items(factory)
    assert report.converged == 1
    status, _statuses, job = _job_state(factory, job_id)
    assert status == "completed"
    assert (job.pending_count, job.succeeded_count) == (0, 2)


# ---- 进度读取触发的单任务巡检 ---------------------------------------------------


def _stale_running_item(session_factory, job_id):
    with session_factory() as session:
        job = jobs.get_batch_scoring_job(session, job_id)
        item = min(job.items, key=lambda value: value.ordinal)
        item.status = "running"
        item.attempt_count = 1
        item.started_at = datetime(2020, 1, 1)
        item.heartbeat_at = datetime(2020, 1, 1)
        job.status = "running"
        session.flush()
        jobs._recount(session, job_id)
        session.commit()
        return item.id


def test_a_progress_read_sweeps_its_job_at_most_every_30_seconds(client):
    job_id, _runs = _job(client.session_factory, papers=2, name="read sweep")
    item_id = _stale_running_item(client.session_factory, job_id)

    first = client.get(f"/api/batch-scoring-jobs/{job_id}")
    assert first.status_code == 200, first.text
    by_id = {item["id"]: item for item in first.json()["items"]}
    assert by_id[item_id]["status"] == "pending"
    assert by_id[item_id]["stall_count"] == 1
    assert first.json()["heartbeat_state"] == "waiting"

    _stale_running_item(client.session_factory, job_id)
    second = client.get(f"/api/batches/{first.json()['grading_batch_id']}/score-jobs/latest")
    # 30 秒内不再巡检同一个任务。
    assert {item["id"]: item for item in second.json()["items"]}[item_id]["status"] == "running"
    assert second.json()["heartbeat_state"] == "stale"

    with client.session_factory() as session:
        session.get(models.BatchScoringJob, job_id).last_swept_at = utcnow() - timedelta(seconds=31)
        session.commit()
    third = client.get(f"/api/batch-scoring-jobs/{job_id}")
    assert {item["id"]: item for item in third.json()["items"]}[item_id]["status"] == "pending"


def test_the_read_sweep_marker_does_not_refresh_updated_at(factory):
    job_id, _runs = _job(factory, papers=1, name="marker")
    with factory() as session:
        before = session.get(models.BatchScoringJob, job_id).updated_at
    sweep.sweep_parent_on_read(factory, jobs.KIND, job_id, now=utcnow() + timedelta(hours=1))
    with factory() as session:
        job = session.get(models.BatchScoringJob, job_id)
        # 运维页按 updated_at 判断任务是否停滞：限频标记不能顺带刷新它。
        assert job.last_swept_at is not None
        assert job.updated_at == before


def test_readiness_flags_an_overdue_sweep_and_counts_item_heartbeats(factory):
    from backend.app.services.deployment.readiness import _batch_signal

    job_id, _runs = _job(factory, papers=2, name="readiness")
    with factory() as session:
        signal = _batch_signal(session)
        # 有活动任务却从没巡检过：卡住的条目没人找回。
        assert signal["sweep"]["status"] == "fail"
        assert signal["status"] == "fail"
    sweep.run_global_sweep(factory)

    claimed = claim.claim_next_item(factory)
    with factory() as session:
        job = session.get(models.BatchScoringJob, job_id)
        # 任务行很久没更新，但条目心跳是新的：这不是停滞。
        job.heartbeat_at = datetime(2020, 1, 1)
        session.commit()
    assert runner.heartbeat(factory, claimed) is True
    with factory() as session:
        signal = _batch_signal(session)
        assert signal["sweep"]["status"] == "pass"
        assert signal["stale_count"] == 0
        assert signal["status"] == "pass"
