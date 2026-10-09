"""连接级并发上限、429 限流重试与模型调用诊断日志。

厂商按 Key 限并发（例如免费档只允许 1 个），起草、归类和批量评分都不能超过连接
声明的上限；429 要按 Retry-After 再等几次，而超时仍然不重试。
"""

import asyncio
import importlib
import logging
import threading
import time
from datetime import datetime

import httpx
import pytest

from backend.app.core.config import settings
from backend.app.db import models
from backend.app.db.models import utcnow
from backend.app.services.ai_connections import ConnectionRuntime
from backend.app.services.ai_connections import validate_provider_options
from backend.app.services.dev_user import ensure_dev_user
from backend.app.services.llm import openai_compatible_adapter as compat
from backend.app.services.llm import transport
from backend.app.services.llm.errors import ProviderCallError
from backend.app.services.llm.factory import get_llm_scorer
from backend.app.services.llm.factory import scorer_concurrency
from backend.app.services.llm.rate_limit import reset_provider_runtime_for_tests
from backend.app.services.rubric_import import ai_rule_drafter
from backend.app.services.rubric_import.classification import llm_classifier
from backend.app.tests.test_m8_batch_scoring_jobs import _seed_batch


@pytest.fixture(autouse=True)
def _fresh_circuits():
    reset_provider_runtime_for_tests()
    yield
    reset_provider_runtime_for_tests()


def _jobs():
    return importlib.import_module("backend.app.services.batch_scoring.jobs")


def _runtime(options):
    return ConnectionRuntime(
        connection_id="conn-1", key_version=1, organization_id="org-1",
        provider_type="openai_compatible", base_url="https://api.z.ai/api/paas/v4",
        model_name="glm-4.7-flash", provider_options=options, api_key="test-key",
    )


# ---- 配置项 ---------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 3, 8])
def test_max_concurrency_accepts_small_integers(value):
    assert validate_provider_options({"max_concurrency": value}) == {"max_concurrency": value}


@pytest.mark.parametrize("value", [0, 9, True, "2", 1.5, None])
def test_max_concurrency_rejects_anything_else(value):
    with pytest.raises(ValueError, match="max_concurrency"):
        validate_provider_options({"max_concurrency": value})


def test_concurrency_is_not_part_of_the_reproducibility_snapshot():
    # 调低并发躲 429 不能让已锁定该连接的批次报「连接配置已变更」。
    assert _runtime({"top_p": 0.9, "max_concurrency": 1}).snapshot() == _runtime({"top_p": 0.9}).snapshot()
    assert _runtime({"top_p": 0.9}).snapshot() != _runtime({"top_p": 0.8}).snapshot()


def test_bound_scorer_carries_the_declared_limit():
    limited = get_llm_scorer(_runtime({"max_concurrency": 1}))
    assert limited.max_concurrency == 1
    assert scorer_concurrency(limited, 3) == 1
    assert scorer_concurrency(get_llm_scorer(_runtime({})), 3) == 3
    assert scorer_concurrency(object(), 3) == 3
    # 声明了就以它为准：Bedrock 等按配额调高。
    assert scorer_concurrency(get_llm_scorer(_runtime({"max_concurrency": 8})), 3) == 8


# ---- 起草 -----------------------------------------------------------------


def test_draft_batches_never_exceed_the_connection_limit():
    active = peak = 0
    lock = threading.Lock()

    def run(batch):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return batch

    assert ai_rule_drafter._run_draft_batches([1, 2, 3, 4], run, max_concurrency=1) == [1, 2, 3, 4]
    assert peak == 1


def test_drafting_passes_the_scorers_declared_limit(monkeypatch):
    from backend.app.tests.test_template_ai_rule_refactor import _criterion

    class Stop(Exception):
        pass

    seen = {}

    def fake_run(batches, run, *, max_concurrency, has_time):
        seen["max_concurrency"] = max_concurrency
        seen["has_time"] = has_time()
        raise Stop

    monkeypatch.setattr(ai_rule_drafter, "_run_draft_batches", fake_run)
    scorer = type("LimitedScorer", (), {"max_concurrency": 1})()
    with pytest.raises(Stop):
        ai_rule_drafter.draft_deduction_rules(
            criterion=_criterion(), input_analysis={}, scorer=scorer, business_profile_key="thesis",
        )
    assert seen["max_concurrency"] == 1
    assert seen["has_time"] is True  # 刚开始，预算充足


# ---- 适配器：429 重试与诊断日志 ---------------------------------------------


def _chat(content='{"items": []}', **extra):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], **extra}


def _scorer(responses, calls, base_url):
    def handler(request):
        calls.append(request)
        result = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(result, Exception):
            raise result
        return result

    return compat.OpenAICompatibleChatScorer(
        api_key="test-key", base_url=base_url, model_name="glm-4.7-flash",
        provider_name="zai", client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _too_many():
    return httpx.Response(
        429, json={"error": {"code": "1302", "message": "并发数过高"}}, headers={"retry-after": "1"},
    )


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(transport.time, "sleep", recorded.append)
    return recorded


def test_rate_limit_is_retried_even_when_timeouts_are_not(sleeps, caplog):
    calls = []
    scorer = _scorer([_too_many(), httpx.Response(200, json=_chat(model="glm-4.7-flash", provider="Z"))],
                     calls, "https://zai-a.example/v4")
    with caplog.at_level(logging.INFO, logger="paper_grading.llm.calls"):
        result = scorer.complete_json("系统", {"units": []}, attempts_limit=1, rate_limit_retries=2)

    assert result == {"items": []}
    assert len(calls) == 2
    assert sleeps == [pytest.approx(1.0)]
    text = caplog.text
    # 厂商错误码区分并发超额（1302）、频率超额与服务繁忙；没有它只能猜。
    assert "llm_call_failed provider=zai model=glm-4.7-flash code=rate_limited status=429 provider_code=1302" in text
    assert "retry=yes" in text
    assert "llm_call provider=zai model=glm-4.7-flash routed_model=glm-4.7-flash upstream=Z" in text
    assert "并发数过高" not in text  # 不记厂商正文


def test_timeouts_still_fail_fast_with_a_single_attempt(sleeps):
    calls = []
    scorer = _scorer([httpx.ReadTimeout("slow")], calls, "https://zai-b.example/v4")
    with pytest.raises(ProviderCallError) as excinfo:
        scorer.complete_json("系统", {"units": []}, attempts_limit=1, rate_limit_retries=2)
    assert excinfo.value.error.code == "request_timeout"
    assert len(calls) == 1
    assert sleeps == []


def test_rate_limit_retries_are_bounded(sleeps):
    calls = []
    scorer = _scorer([_too_many()], calls, "https://zai-c.example/v4")
    with pytest.raises(ProviderCallError) as excinfo:
        scorer.complete_json("系统", {"units": []}, attempts_limit=1, rate_limit_retries=2)
    assert excinfo.value.error.code == "rate_limited"
    assert len(calls) == 3


def test_error_envelope_inside_a_success_status_is_logged_with_its_code(sleeps, caplog):
    calls = []
    envelope = {"error": {"code": 502, "message": "Provider returned error", "metadata": {"provider_name": "Chutes"}}}
    scorer = _scorer([httpx.Response(200, json=envelope)], calls, "https://router.example/v1")
    with caplog.at_level(logging.INFO, logger="paper_grading.llm.calls"):
        with pytest.raises(compat.ChatJSONOutputError):
            scorer.complete_json("系统", {"units": []}, attempts_limit=1)
    assert "llm_call_error_envelope provider=zai model=glm-4.7-flash routed_model=- upstream=Chutes provider_code=502" in caplog.text
    assert "Provider returned error" not in caplog.text


# ---- 归类 -----------------------------------------------------------------


def test_classifier_asks_for_rate_limit_retries_without_timeout_retries():
    captured = {}

    class Scorer:
        provider = "zai"
        model_name = "glm-4.7-flash"

        def complete_json(self, instructions, payload, *, default_max_tokens=None, attempts_limit=None,
                          default_timeout_seconds=None, rate_limit_retries=None):
            captured.update(attempts_limit=attempts_limit, rate_limit_retries=rate_limit_retries)
            return {"items": [{"unit_id": "u1", "label": "context", "suggested_criterion": None,
                               "reason": "说明文字", "confidence": "high"}]}

    result = llm_classifier.classify_units(
        [{"unit_id": "u1", "text": "说明", "context": {}}], [{"code": "C1", "name": "规范"}], Scorer(),
    )
    assert [item["unit_id"] for item in result["results"]] == ["u1"]
    assert captured == {"attempts_limit": 1, "rate_limit_retries": 2}


# ---- 批量评分 -------------------------------------------------------------


def _bind_connection(session, batch_id, *, max_concurrency):
    user = ensure_dev_user(session)
    organization = models.Organization(name=f"并发测试组织-{batch_id[:8]}")
    session.add(organization)
    session.flush()
    connection = models.AIConnection(
        organization_id=organization.id, owner_id=user.id, name=f"Z.ai 免费-{batch_id[:8]}",
        provider_type="openai_compatible", base_url="https://api.z.ai/api/paas/v4",
        model_name="glm-4.7-flash", provider_options={"max_concurrency": max_concurrency},
        api_key_ciphertext="cipher", api_key_nonce="nonce", api_key_tag="tag", key_last4="1234",
    )
    session.add(connection)
    session.flush()
    session.get(models.GradingBatch, batch_id).ai_connection_id = connection.id
    session.commit()


def _limited_job(client, *, count, name, first_started_at, max_concurrency=1):
    jobs = _jobs()
    with client.session_factory() as session:
        batch_id, rubric_id, paper_ids = _seed_batch(session, count=count, name=name)
        _bind_connection(session, batch_id, max_concurrency=max_concurrency)
        job, _ = jobs.create_batch_scoring_job(
            session, batch_id=batch_id, rescore=False, max_workers=2,
            observation_policy=None, actor_id=settings.DEFAULT_DEV_USER_ID,
        )
        items = sorted(job.items, key=lambda value: (value.created_at, value.id))
        items[0].status = "running"
        items[0].attempt_count = 1
        items[0].started_at = first_started_at
        job.status = "running"
        session.commit()
        return job.id, job.max_workers, [item.id for item in items], rubric_id, paper_ids


def test_local_worker_parallelism_follows_the_connection_declaration(client):
    # 工作台固定传 max_workers=2；连接声明了同时请求数就以它为准，可以调低也可以调高。
    _job_id, lowered, *_ = _limited_job(client, count=2, name="lowered", first_started_at=utcnow())
    _job_id, raised, *_ = _limited_job(
        client, count=2, name="raised", first_started_at=utcnow(), max_concurrency=6,
    )
    assert (lowered, raised) == (1, 6)


def test_queue_defers_items_while_the_connection_is_busy(client):
    jobs = _jobs()
    job_id, _, item_ids, _, _ = _limited_job(client, count=3, name="busy", first_started_at=utcnow())

    with pytest.raises(jobs.ConnectionAtCapacityError) as next_in_line:
        jobs.run_batch_scoring_item(client.session_factory, job_id=job_id, item_id=item_ids[1])
    with pytest.raises(jobs.ConnectionAtCapacityError) as further_back:
        jobs.run_batch_scoring_item(client.session_factory, job_id=job_id, item_id=item_ids[2])

    # 排得越靠后等得越久，减少空转的函数调用。
    assert next_in_line.value.retry_after_seconds == jobs.CONNECTION_WAIT_BASE_SECONDS
    assert further_back.value.retry_after_seconds == 2 * jobs.CONNECTION_WAIT_BASE_SECONDS
    with client.session_factory() as session:
        waiting = session.get(models.BatchScoringItem, item_ids[1])
        assert (waiting.status, waiting.attempt_count) == ("pending", 0)


def test_an_expired_lease_does_not_hold_the_connection(client):
    jobs = _jobs()
    job_id, _, item_ids, rubric_id, paper_ids = _limited_job(
        client, count=2, name="stale holder", first_started_at=datetime(2020, 1, 1),
    )
    with client.session_factory() as session:
        # 已有评分记录且不重评：领取后直接跳过，不调用模型。
        session.add(models.ScoringRun(
            paper_id=paper_ids[1], rubric_id=rubric_id, status="scored", ai_total_score=8,
            final_total_score=8, grade="通过", need_manual_review=False,
        ))
        session.commit()

    jobs.run_batch_scoring_item(client.session_factory, job_id=job_id, item_id=item_ids[1])

    with client.session_factory() as session:
        assert session.get(models.BatchScoringItem, item_ids[1]).status == "skipped"


def test_queue_consumer_defers_with_a_fresh_delayed_message(monkeypatch):
    queue = importlib.import_module("backend.app.services.batch_scoring.vercel_queue")
    jobs = _jobs()
    sent = []

    async def fake_send(topic, payload, **options):
        sent.append((topic, payload, options))

    def at_capacity(*_args, **_kwargs):
        raise jobs.ConnectionAtCapacityError(60)

    monkeypatch.setattr(queue, "send", fake_send)
    monkeypatch.setattr(jobs, "run_batch_scoring_item", at_capacity)
    for _ in range(2):
        asyncio.run(queue.score_batch_item({"job_id": "job-1", "item_id": "item-1"}))

    assert [value[0] for value in sent] == [queue.SCORING_TOPIC] * 2
    assert all(value[1] == {"job_id": "job-1", "item_id": "item-1"} for value in sent)
    assert all(value[2]["delay"] == 60 for value in sent)
    keys = [value[2]["idempotency_key"] for value in sent]
    # 同键会被队列去重：延迟消息一旦被吞，这篇论文就再没人领。
    assert all(key.startswith("score-item-1-wait-") for key in keys) and keys[0] != keys[1]


# ---- 额度耗尽：同是 429，但等待不会恢复 ---------------------------------------


def _project(status, body):
    from backend.app.services.llm.errors import project_provider_error

    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    response = httpx.Response(status, json=body, request=request)
    return project_provider_error(httpx.HTTPStatusError("error", request=request, response=response))


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"type": "insufficient_quota", "code": "insufficient_quota", "message": "quota"}},
        {"error": {"code": "1113", "message": "余额不足或无可用资源包,请充值。"}},
    ],
)
def test_quota_exhausted_429_is_not_retryable(body):
    projected = _project(429, body)
    assert (projected.code, projected.retryable) == ("quota_exhausted", False)


def test_concurrency_429_stays_a_retryable_rate_limit():
    projected = _project(429, {"error": {"code": "1302", "message": "并发数过高"}})
    assert (projected.code, projected.retryable) == ("rate_limited", True)


def test_quota_exhausted_is_not_retried_even_with_rate_limit_budget(sleeps, caplog):
    calls = []
    exhausted = httpx.Response(429, json={"error": {"code": "1113", "message": "余额不足"}})
    scorer = _scorer([exhausted], calls, "https://zai-d.example/v4")
    with caplog.at_level(logging.INFO, logger="paper_grading.llm.calls"):
        with pytest.raises(ProviderCallError) as excinfo:
            scorer.complete_json("系统", {"units": []}, attempts_limit=1, rate_limit_retries=2)
    assert excinfo.value.error.code == "quota_exhausted"
    assert len(calls) == 1 and sleeps == []
    assert "code=quota_exhausted status=429 provider_code=1113" in caplog.text
    assert "retry=no" in caplog.text


# ---- 起草的总时间预算 ---------------------------------------------------------


def test_budget_is_checked_before_every_batch():
    from backend.app.services.rubric_import.ai_rule_drafter import AIRuleDraftValidationError

    started = []
    budget = iter([True, True, False])  # 第 1、2 批开始前还有时间，第 3 批开始前没有了

    with pytest.raises(AIRuleDraftValidationError) as excinfo:
        ai_rule_drafter._run_draft_batches(
            ["b1", "b2", "b3", "b4"], lambda batch: started.append(batch) or batch,
            max_concurrency=1, has_time=lambda: next(budget),
        )

    assert started == ["b1", "b2"]
    error = excinfo.value
    assert error.code == "AI_DRAFT_TIME_BUDGET_EXCEEDED"
    assert "分 4 批" in error.message and "已完成 2 批" in error.message and "同时只允许 1 个" in error.message
    assert "并发上限" in error.user_action


def test_a_later_criterion_cannot_start_its_only_batch_after_the_budget_is_spent():
    # 截止时间按整次请求算：前面的评分项用完了预算，后面只有一批的评分项也不能再开始。
    from backend.app.services.rubric_import.ai_rule_drafter import AIRuleDraftValidationError

    started = []
    with pytest.raises(AIRuleDraftValidationError) as excinfo:
        ai_rule_drafter._run_draft_batches(["only"], started.append, max_concurrency=1, has_time=lambda: False)
    assert started == []
    assert excinfo.value.code == "AI_DRAFT_TIME_BUDGET_EXCEEDED"


def test_admission_needs_less_than_a_full_long_timeout():
    # 连接显式设了 300 秒超时也能开始第一批：门槛取 60 秒，调用超时由适配器压到预算以内。
    long_timeout = type("S", (), {"timeout_seconds": 300, "timeout_seconds_explicit": True})()
    now = time.monotonic()
    assert ai_rule_drafter._has_time_for_call(now + 259, long_timeout) is True
    assert ai_rule_drafter._has_time_for_call(now + 59, long_timeout) is False
    assert ai_rule_drafter._has_time_for_call(None, long_timeout) is True


def test_repair_is_skipped_when_it_would_overrun_the_budget(monkeypatch):
    from backend.app.services.rubric_import.ai_rule_drafter import AIRuleDraftValidationError

    attempts = []

    def invalid(**kwargs):
        attempts.append(kwargs)
        raise AIRuleDraftValidationError("AI_DRAFT_OUTPUT_INVALID", "模型未返回有效的规则 JSON。", "请重试。")

    monkeypatch.setattr(ai_rule_drafter, "_draft_deduction_rules_once", invalid)
    scorer = type("S", (), {"timeout_seconds": 0, "timeout_seconds_explicit": False})()
    with pytest.raises(AIRuleDraftValidationError, match="有效的规则 JSON"):
        ai_rule_drafter._draft_batch_with_repair(
            criterion={}, input_analysis={}, scorer=scorer, business_profile_key="thesis",
            deadline=time.monotonic() + 30,
        )
    assert len(attempts) == 1  # 剩余时间不够再开始一次调用，就不做修正
    assert attempts[0]["deadline"] is not None  # 截止时间传给了调用，由适配器约束超时


def test_draft_endpoint_shares_one_deadline_and_reports_budget_exhaustion(client, monkeypatch):
    from backend.app.api.routes import rubrics as rubric_routes
    from backend.app.services.rubric_import.ai_rule_drafter import AIRuleDraftValidationError
    from backend.app.tests.test_template_ai_rule_refactor import _criterion

    created = client.post("/api/rubrics", json={
        "name": "AI draft time budget", "version": "v1", "total_score": 20,
        "criteria": [_criterion(scoring_mode="review_only")],
    })
    assert created.status_code == 200, created.text
    deadlines = []

    def out_of_time(**kwargs):
        deadlines.append(kwargs["deadline"])
        raise AIRuleDraftValidationError(
            "AI_DRAFT_TIME_BUDGET_EXCEEDED", "该评分项需要分 4 批生成", "请调高该连接的并发上限",
        )

    monkeypatch.setattr(rubric_routes, "get_llm_scorer", lambda *a, **k: object())
    monkeypatch.setattr(rubric_routes, "draft_deduction_rules", out_of_time)
    response = client.post(
        f"/api/rubrics/{created.json()['id']}/draft-deduction-rules", json={"criteria": [_criterion()]},
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "AI_DRAFT_TIME_BUDGET_EXCEEDED"
    # 原样重试还会撞上同一个上限，不能提示「稍后重试」。
    assert detail["retryable"] is False
    budget = settings.RUBRIC_AI_DRAFT_TIME_BUDGET_SECONDS
    assert deadlines and 0 < deadlines[0] - time.monotonic() <= budget


# ---- 适配器：截止时间约束每次调用与重试 -------------------------------------------


def test_expired_deadline_sends_no_request(sleeps):
    calls = []
    scorer = _scorer([httpx.Response(200, json=_chat())], calls, "https://deadline-a.example/v4")
    with pytest.raises(ProviderCallError) as excinfo:
        scorer.complete_json("系统", {"units": []}, attempts_limit=1, deadline=time.monotonic() - 1)
    assert excinfo.value.error.code == "request_timeout"
    assert calls == []


def test_call_timeout_is_capped_by_the_remaining_budget(sleeps):
    calls = []
    scorer = _scorer([httpx.Response(200, json=_chat())], calls, "https://deadline-b.example/v4")
    scorer.complete_json("系统", {"units": []}, attempts_limit=1, deadline=time.monotonic() + 40)
    read_timeout = calls[0].extensions["timeout"]["read"]
    assert 0 < read_timeout <= 40  # 配置的超时更长，也不能越过截止时间


def test_rate_limit_retry_is_skipped_when_the_wait_would_eat_the_budget(sleeps):
    calls = []
    scorer = _scorer([_too_many()], calls, "https://deadline-c.example/v4")
    with pytest.raises(ProviderCallError) as excinfo:
        # Retry-After 1 秒，但等完只剩不到 30 秒，重试那次调用来不及做完。
        scorer.complete_json("系统", {"units": []}, attempts_limit=1, rate_limit_retries=2,
                             deadline=time.monotonic() + 25)
    assert excinfo.value.error.code == "rate_limited"
    assert len(calls) == 1 and sleeps == []


def test_a_timeout_cut_short_by_the_budget_is_reported_as_budget_exhaustion(monkeypatch):
    from backend.app.services.rubric_import.ai_rule_drafter import AIRuleDraftValidationError
    from backend.app.tests.test_template_ai_rule_refactor import _criterion

    deadline = time.monotonic() + 61
    calls = []
    scorer = _scorer([httpx.ReadTimeout("cut by budget")], calls, "https://deadline-d.example/v4")

    class TimeIsUp:
        """起草模块看到的时钟已经走到截止时间；适配器仍用真实时钟。"""

        @staticmethod
        def monotonic():
            return deadline

    monkeypatch.setattr(ai_rule_drafter, "time", TimeIsUp)
    with pytest.raises(AIRuleDraftValidationError) as excinfo:
        ai_rule_drafter._draft_deduction_rules_once(
            criterion=_criterion(), input_analysis={}, scorer=scorer,
            business_profile_key="thesis", deadline=deadline,
        )
    assert excinfo.value.code == "AI_DRAFT_TIME_BUDGET_EXCEEDED"
    assert len(calls) == 1


# ---- 平台默认模型与全局队列并发 ------------------------------------------------


def test_platform_model_declaration_limits_unbound_batches(client):
    from backend.app.services import platform_llm

    jobs = _jobs()
    with client.session_factory() as session:
        platform_llm.set_config(
            session, provider_type="openai_compatible", base_url="https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1",
            model_name="openai.gpt-oss-120b-1:0", api_key="platform-test-key", configured_by="admin-1",
            provider_options={"max_concurrency": 1},
        )
        batch_id, _rubric_id, _paper_ids = _seed_batch(session, count=2, name="platform busy")
        job, _ = jobs.create_batch_scoring_job(
            session, batch_id=batch_id, rescore=False, max_workers=2,
            observation_policy=None, actor_id=settings.DEFAULT_DEV_USER_ID,
        )
        assert job.max_workers == 1
        items = sorted(job.items, key=lambda value: (value.created_at, value.id))
        items[0].status, items[0].attempt_count, items[0].started_at = "running", 1, utcnow()
        job.status = "running"
        session.commit()
        job_id, waiting_id = job.id, items[1].id

    with pytest.raises(jobs.ConnectionAtCapacityError):
        jobs.run_batch_scoring_item(client.session_factory, job_id=job_id, item_id=waiting_id)


@pytest.mark.parametrize(
    "raw, expected", [(None, 8), ("", 8), ("16", 16), ("0", 1), ("99", 32), ("abc", 8)],
)
def test_queue_concurrency_is_configurable_and_bounded(monkeypatch, raw, expected):
    queue = importlib.import_module("backend.app.services.batch_scoring.vercel_queue")
    if raw is None:
        monkeypatch.delenv("BATCH_SCORING_QUEUE_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("BATCH_SCORING_QUEUE_CONCURRENCY", raw)
    assert queue.queue_concurrency() == expected
