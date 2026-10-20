"""评分输出预算方案 P1：观测模型实际的思考与截断，身份与行为不符时停下。

docs/评分输出预算与截断恢复改造方案.md 第 5 节。全部用本地假客户端 / 假打分器，不调用真实模型。
"""

from __future__ import annotations

from datetime import timedelta
import logging

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.core.config import settings
from backend.app.db import models
from backend.app.services.batch_scoring import jobs
from backend.app.services.deployment.readiness import build_ops_readiness
from backend.app.services.deployment.readiness import build_organization_readiness
from backend.app.services.llm import rate_limit
from backend.app.services.llm.anthropic_messages_adapter import observed_usage_from_messages
from backend.app.services.llm.mock import MockLLMScorer
from backend.app.services.llm.openai_compatible_adapter import OpenAICompatibleChatScorer
from backend.app.services.llm.usage import ThinkingConsistencyGuard
from backend.app.services.llm.usage import ThinkingNotDisabledError
from backend.app.services.llm.usage import UsageMeter
from backend.app.services.llm.usage import observed_usage
from backend.app.services.scoring import engine as scoring_engine
from backend.app.tests.conftest import create_legacy_unversioned_rubric_fixture
from backend.app.tests.conftest import make_sample_docx
from backend.app.tests.test_m0_characterization import M0_RUBRIC
from backend.app.tests.test_m8_batch_scoring_jobs import _policy
from backend.app.tests.test_m8_batch_scoring_jobs import _seed_batch


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
OFF = {"enabled": False, "type": "none"}


# ---------------------------------------------------------------- 观测


def test_reported_reasoning_wins_and_missing_reasoning_is_inferred_from_the_total():
    reported = observed_usage(prompt=100, completion=30, total=130, reported_reasoning=12)
    assert (reported["reasoning_tokens"], reported["reasoning_source"]) == (12, "reported")

    # 不单独报告推理 token、但把思考计入合计的厂商（Vertex 实际会报告，这里是推算的兜底）。
    inferred = observed_usage(prompt=1200, completion=70, total=2040)
    assert (inferred["reasoning_tokens"], inferred["reasoning_source"]) == (770, "inferred")

    # 厂商明确报告 0：不再推算。
    zero = observed_usage(prompt=100, completion=30, total=200, reported_reasoning=0)
    assert (zero["reasoning_tokens"], zero["reasoning_source"]) == (0, None)

    # 只有思考块、没有计数（Claude）。
    seen = observed_usage(prompt=10, completion=5, total=15, thinking_seen=True)
    assert (seen["reasoning_tokens"], seen["reasoning_source"]) == (0, "reported")

    clean = observed_usage(prompt=10, completion=5, total=15, finish_reason="length", truncated=True)
    assert clean["reasoning_source"] is None
    assert (clean["finish_reason"], clean["truncated"]) == ("length", True)


def test_meter_counts_reasoning_answers_inferred_answers_and_truncations():
    meter = UsageMeter()
    meter.record_success(observed_usage(prompt=10, completion=5, total=15))
    meter.record_success(observed_usage(prompt=10, completion=5, total=40, truncated=True))
    meter.record_success(observed_usage(prompt=10, completion=5, total=15, reported_reasoning=9))

    snapshot = meter.snapshot()
    assert snapshot["reasoning_tokens"] == 25 + 9
    assert snapshot["reasoning_requests"] == 2
    assert snapshot["inferred_reasoning_requests"] == 1
    assert snapshot["truncated_count"] == 1
    assert snapshot["request_count"] == 3


class _ScriptedClient:
    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.sent = 0

    def post(self, url, headers, json, **kwargs):
        self.sent += 1
        return httpx.Response(200, json=self.bodies.pop(0), request=httpx.Request("POST", url))

    def close(self):
        pass


def _chat(*, finish_reason="stop", usage, message=None):
    return {
        "id": "chat",
        "choices": [
            {"finish_reason": finish_reason, "message": message or {"content": '{"ok": true}'}}
        ],
        "usage": usage,
    }


def test_compatible_adapter_logs_finish_reason_and_reasoning_without_content(caplog):
    rate_limit.reset_provider_runtime_for_tests()
    client = _ScriptedClient(
        _chat(
            finish_reason="length",
            usage={"prompt_tokens": 1200, "completion_tokens": 70, "total_tokens": 2040},
            message={"content": "SECRET-PAPER-TEXT"},
        ),
        _chat(
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "total_tokens": 140,
                "completion_tokens_details": {"reasoning_tokens": 25},
            }
        ),
    )
    scorer = OpenAICompatibleChatScorer(
        api_key="test-key",
        base_url="https://observe-compatible.invalid/v1",
        model_name="observe-model",
        client=client,
    )

    with caplog.at_level(logging.INFO, logger="paper_grading.llm.calls"):
        scorer._post_with_retry({"model": "observe-model"}, attempts_limit=1)
        scorer._post_with_retry({"model": "observe-model"}, attempts_limit=1)

    lines = [record.getMessage() for record in caplog.records if record.name == "paper_grading.llm.calls"]
    assert "finish_reason=length" in lines[0]
    assert "reasoning_tokens=770 reasoning=inferred" in lines[0]
    assert "reasoning_tokens=25 reasoning=reported" in lines[1]
    assert not any("SECRET-PAPER-TEXT" in line for line in lines)
    snapshot = scorer.usage_meter.snapshot()
    assert (snapshot["reasoning_tokens"], snapshot["truncated_count"]) == (795, 1)
    rate_limit.reset_provider_runtime_for_tests()


def test_compatible_adapter_counts_reasoning_content_as_thinking():
    rate_limit.reset_provider_runtime_for_tests()
    client = _ScriptedClient(
        _chat(
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            message={"content": "{}", "reasoning_content": "先想一想"},
        )
    )
    scorer = OpenAICompatibleChatScorer(
        api_key="test-key",
        base_url="https://observe-zhipu.invalid/v1",
        model_name="observe-model",
        client=client,
    )
    scorer._post_with_retry({"model": "observe-model"}, attempts_limit=1)
    assert scorer.usage_meter.snapshot()["reasoning_requests"] == 1
    rate_limit.reset_provider_runtime_for_tests()


def test_claude_thinking_blocks_and_max_tokens_are_observed():
    usage = observed_usage_from_messages(
        {
            "stop_reason": "max_tokens",
            "content": [{"type": "thinking", "thinking": "…"}, {"type": "text", "text": "{"}],
            "usage": {"input_tokens": 50, "output_tokens": 512},
        }
    )
    assert usage["reasoning_source"] == "reported"
    assert (usage["finish_reason"], usage["truncated"]) == ("max_tokens", True)


# ---------------------------------------------------------------- 守卫


def _delta(*, answered=1, reasoning=0, inferred=0):
    return {
        "request_count": answered,
        "failure_count": 0,
        "reasoning_requests": reasoning,
        "inferred_reasoning_requests": inferred,
    }


def test_reported_reasoning_trips_the_guard_at_once():
    guard = ThinkingConsistencyGuard()
    assert guard.after_call(OFF, _delta(reasoning=1)) is True
    with pytest.raises(ThinkingNotDisabledError) as caught:
        guard.before_call(OFF)
    assert caught.value.sent is False


def test_inferred_reasoning_needs_two_answers_in_a_row():
    guard = ThinkingConsistencyGuard()
    assert guard.after_call(OFF, _delta(reasoning=1, inferred=1)) is False
    # 中间一次干净的应答，重新计数。
    assert guard.after_call(OFF, _delta()) is False
    assert guard.after_call(OFF, _delta(reasoning=1, inferred=1)) is False
    assert guard.after_call(OFF, _delta(reasoning=1, inferred=1)) is True


def test_guard_ignores_runs_that_allow_thinking_and_calls_without_metering():
    guard = ThinkingConsistencyGuard()
    assert guard.after_call({"enabled": True, "type": "enabled"}, _delta(reasoning=1)) is False
    assert guard.after_call(OFF, None) is False
    assert guard.after_call(None, _delta(reasoning=1)) is False
    guard.before_call(OFF)


# ---------------------------------------------------------------- 一篇论文


class _ThinkingScorer(MockLLMScorer):
    """非 Mock 的替身：身份不思考，每次应答却都报告了推理 token。"""

    provider = "fake-provider"
    model_name = "fake-thinking-model"
    model_version = "fake-v1"
    temperature = 0
    max_output_tokens = 512
    calls = 0
    reasoning = {"reported_reasoning": 300}

    def __init__(self):
        self.usage_meter = UsageMeter()

    def score_core_envelope(self, *, envelope):
        type(self).calls += 1
        self.usage_meter.record_success(
            observed_usage(**{"prompt": 100, "completion": 10, "total": 110, **type(self).reasoning})
        )
        rule = envelope.to_mapping()["atomic_rule_snapshot"]
        return {
            "schema_version": "semantic-rule-response@2",
            "rule_code": rule["rule_code"],
            "status": "not_triggered",
            "level_code": None,
            "occurrences": [],
        }

    def close(self):
        pass


@pytest.fixture
def thinking_paper(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")
    monkeypatch.setattr(settings, "SCORING_ENGINE_MODE", "core")
    monkeypatch.setattr(_ThinkingScorer, "calls", 0)
    monkeypatch.setattr(
        scoring_engine, "get_llm_scorer", lambda *args, **kwargs: _ThinkingScorer()
    )
    rubric_id = create_legacy_unversioned_rubric_fixture(client, M0_RUBRIC)
    batch = client.post("/api/batches", json={"name": "thinking", "rubric_id": rubric_id})
    assert batch.status_code == 200, batch.text
    upload = client.post(
        "/api/papers/upload",
        data={"batch_id": batch.json()["id"]},
        files={"file": ("thinking.docx", make_sample_docx().getvalue(), DOCX_MIME)},
    )
    assert upload.status_code == 200, upload.text
    return upload.json()["id"]


def _semantic_tasks(db, run_id):
    return db.scalars(
        select(models.RuleScoringTask)
        .where(
            models.RuleScoringTask.scoring_run_id == run_id,
            models.RuleScoringTask.judge_type == "semantic",
        )
        .order_by(models.RuleScoringTask.created_at, models.RuleScoringTask.id)
    ).all()


def test_the_run_records_reasoning_and_the_rule_that_was_actually_sent(client, thinking_paper):
    with client.session_factory() as db:
        run = scoring_engine.score_paper(db, thinking_paper)
        tasks = _semantic_tasks(db, run.id)
        assert _ThinkingScorer.calls == 1
        assert [task.provider_error["code"] for task in tasks] == ["PROVIDER_THINKING_NOT_DISABLED"]
        assert "decision discarded" in tasks[0].provider_error["message"]
        assert (tasks[0].status, tasks[0].attempt_count, tasks[0].max_attempts) == (
            "failed_exhausted",
            1,
            1,
        )
        assert run.final_total_score is None
        assert (run.reasoning_tokens, run.truncated_count) == (300, 0)
        error = jobs._incomplete_scoring_error(tasks)
        assert error.code == "PROVIDER_THINKING_NOT_DISABLED"
        assert jobs._classify_failure(error) == ("PROVIDER_THINKING_NOT_DISABLED", "llm")


def test_a_clean_model_records_one_real_attempt_per_rule(client, thinking_paper, monkeypatch):
    monkeypatch.setattr(_ThinkingScorer, "reasoning", {})
    with client.session_factory() as db:
        run = scoring_engine.score_paper(db, thinking_paper)
        tasks = _semantic_tasks(db, run.id)
        sent = [task for task in tasks if task.status == "succeeded"]
        assert sent and len(sent) == _ThinkingScorer.calls
        assert {task.attempt_count for task in sent} == {1}
        assert run.reasoning_tokens == 0


def _three_rule_request():
    from backend.app.tests import test_m4_rule_executor as m4

    criterion = m4._criterion()
    return m4._request_for(
        *((criterion, m4._deduct_rule(code=code)) for code in ("R1", "R2", "R3"))
    )


def _execute(request, scorer):
    from backend.app.services.scoring.core.rule_executor import execute_rule_plan
    from backend.app.services.scoring.decision_ledger import RuleCallJournal
    from backend.app.services.scoring.profiles.thesis import ThesisLLMRuntime
    from backend.app.tests import test_m4_rule_executor as m4

    journal = RuleCallJournal()
    result = m4._result_mapping(
        execute_rule_plan(
            request=request,
            checker_registry=m4._CheckerRegistry(),
            llm_runtime=ThesisLLMRuntime(scorer),
            profile=m4._TechnicalProposalProfile(),
            execution_journal=journal,
        )
    )
    issues = {
        issue["rule_code"]: issue["code"]
        for issue in result["review_issues"]
        if issue.get("rule_code")
    }
    return result, issues, journal


def test_reported_reasoning_stops_the_remaining_rules_without_sending_them():
    scorer = _ThinkingScorer()
    type(scorer).calls = 0

    _result, issues, journal = _execute(_three_rule_request(), scorer)

    assert type(scorer).calls == 1
    assert issues == {code: "PROVIDER_THINKING_NOT_DISABLED" for code in ("R1", "R2", "R3")}
    assert journal.provider_attempts == {"R1": 1, "R2": 0, "R3": 0}


def test_inferred_reasoning_keeps_the_first_answer_and_stops_at_the_second(monkeypatch):
    monkeypatch.setattr(_ThinkingScorer, "reasoning", {"total": 400})
    scorer = _ThinkingScorer()
    type(scorer).calls = 0

    _result, issues, journal = _execute(_three_rule_request(), scorer)

    assert type(scorer).calls == 2
    assert issues == {"R2": "PROVIDER_THINKING_NOT_DISABLED", "R3": "PROVIDER_THINKING_NOT_DISABLED"}
    assert journal.provider_attempts == {"R1": 1, "R2": 1, "R3": 0}


def test_a_run_that_allows_thinking_is_not_stopped(monkeypatch):
    request = _three_rule_request()
    request["runtime_identity"]["provider"]["thinking"] = {"enabled": True, "type": "enabled"}
    from backend.app.tests import test_m4_rule_executor as m4

    m4._refresh_request_identity(request)
    scorer = _ThinkingScorer()
    type(scorer).calls = 0

    _result, issues, journal = _execute(request, scorer)

    assert type(scorer).calls == 3
    assert issues == {}
    assert journal.provider_attempts == {"R1": 1, "R2": 1, "R3": 1}


# ---------------------------------------------------------------- 批量任务


def _thinking_failure():
    error = jobs.IncompleteScoringResultError(
        "PROVIDER_THINKING_NOT_DISABLED",
        jobs.THINKING_NOT_DISABLED_MESSAGE,
        failure_kind="llm",
    )
    error.provider_usage = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "reasoning_tokens": 300,
        "truncated_count": 1,
    }
    return error


def test_a_thinking_mismatch_stops_the_rest_of_the_job(tmp_path):
    engine = create_engine(
        "sqlite+pysqlite:///%s" % (tmp_path / "thinking-stop.db"),
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as session:
        batch_id, _rubric_id, paper_ids = _seed_batch(session, count=3, name="thinking stop")
        job, _ = jobs.create_batch_scoring_job(
            session,
            batch_id=batch_id,
            rescore=False,
            max_workers=1,
            observation_policy=_policy(minimum_sample_size=3),
            actor_id=settings.DEFAULT_DEV_USER_ID,
        )
        job_id = job.id

    scored = []

    def score_item(_session, *, paper_id, job_id):
        scored.append(paper_id)
        raise _thinking_failure()

    final = jobs.run_batch_scoring_job(factory, job_id=job_id, score_item=score_item)

    assert len(scored) == 1
    assert final.status == "canceled"
    assert (final.failed_count, final.canceled_count) == (1, 2)
    failed = next(item for item in final.items if item.status == "failed")
    assert failed.error_code == "PROVIDER_THINKING_NOT_DISABLED"
    assert "关闭思考" in failed.error_message
    # 失败的这篇也看得到花了多少、思考了多少。
    assert (failed.telemetry["reasoning_tokens"], failed.telemetry["truncated_count"]) == (300, 1)
    assert failed.telemetry["llm_failures"] == 1
    for item in final.items:
        if item.status == "canceled":
            assert item.error_code == "PROVIDER_THINKING_NOT_DISABLED"
            assert "未产生费用" in item.error_message

    # 换了模型之后，任务照常可以重试。
    with factory() as session:
        retried = jobs.retry_batch_scoring_job(session, job_id)
        assert retried.pending_count == 3
    engine.dispose()


def test_other_failures_do_not_stop_the_job(tmp_path):
    engine = create_engine(
        "sqlite+pysqlite:///%s" % (tmp_path / "truncation-continue.db"),
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as session:
        batch_id, _rubric_id, _paper_ids = _seed_batch(session, count=2, name="truncation")
        job, _ = jobs.create_batch_scoring_job(
            session,
            batch_id=batch_id,
            rescore=False,
            max_workers=1,
            observation_policy=_policy(minimum_sample_size=2),
            actor_id=settings.DEFAULT_DEV_USER_ID,
        )
        job_id = job.id

    def score_item(_session, *, paper_id, job_id):
        raise jobs.IncompleteScoringResultError(
            "PROVIDER_OUTPUT_TRUNCATED", "截断", failure_kind="llm"
        )

    final = jobs.run_batch_scoring_job(factory, job_id=job_id, score_item=score_item)
    assert final.status == "completed_with_errors"
    assert final.failed_count == 2
    engine.dispose()


def test_truncation_message_warns_that_raising_the_cap_rejudges_everything():
    from types import SimpleNamespace

    error = jobs._incomplete_scoring_error(
        [SimpleNamespace(status="failed_exhausted", provider_error={"code": "PROVIDER_OUTPUT_TRUNCATED"})]
    )
    assert error.code == "PROVIDER_OUTPUT_TRUNCATED"
    assert "重新判定" in str(error)


# ---------------------------------------------------------------- 运维观测


def _scored_run(db, *, organization_id, model, reasoning, truncated, created_at):
    from uuid import uuid4

    from backend.app.services.dev_user import ensure_dev_user

    user = ensure_dev_user(db)
    rubric = models.Rubric(
        name="obs-%s" % uuid4().hex,
        version="v1",
        total_score=10,
        status="published",
        created_by=user.id,
        owner_id=user.id,
        organization_id=organization_id,
    )
    batch = models.GradingBatch(
        name="obs", rubric=rubric, status="draft", created_by=user.id, owner_id=user.id,
        organization_id=organization_id,
    )
    paper = models.Paper(
        batch=batch, file_name="p.docx", file_path="/tmp/p.docx", status="parsed",
        organization_id=organization_id,
    )
    db.add_all([rubric, batch, paper])
    db.flush()
    db.add(
        models.ScoringRun(
            paper_id=paper.id,
            rubric_id=rubric.id,
            organization_id=organization_id,
            status="scored",
            model_provider="openai_compatible",
            model_name=model,
            completion_tokens=100,
            reasoning_tokens=reasoning,
            truncated_count=truncated,
            created_at=created_at,
        )
    )
    db.commit()


def test_ops_readiness_shows_reasoning_and_truncation_per_model_without_gating(client):
    from backend.app.db.models import utcnow

    now = utcnow()
    with client.session_factory() as db:
        org = models.Organization(id="org-obs", name="obs", created_by=settings.DEFAULT_DEV_USER_ID)
        db.add(org)
        db.commit()
        _scored_run(db, organization_id="org-obs", model="gemini-2.5-flash", reasoning=900, truncated=2, created_at=now)
        _scored_run(db, organization_id=None, model="other-model", reasoning=0, truncated=0, created_at=now)
        # 窗口之外的不算。
        _scored_run(db, organization_id="org-obs", model="gemini-2.5-flash", reasoning=5000, truncated=9, created_at=now - timedelta(days=2))

        platform = build_ops_readiness(db)
        signal = platform["signals"]["batch_jobs"]["model_output"]
        assert signal["status"] == "fail"
        assert (signal["runs"], signal["reasoning_tokens"], signal["truncated_count"]) == (2, 900, 2)
        assert {model["model"] for model in signal["models"]} == {"gemini-2.5-flash", "other-model"}
        # 只观察：不改变批任务信号本身的状态。
        assert platform["signals"]["batch_jobs"]["status"] == "pass"

        scoped = build_organization_readiness(db, "org-obs")["signals"]["batch_jobs"]["model_output"]
        assert [model["model"] for model in scoped["models"]] == ["gemini-2.5-flash"]
        assert scoped["runs"] == 1


def test_ops_readiness_model_output_is_unavailable_without_runs(client):
    with client.session_factory() as db:
        signal = build_ops_readiness(db)["signals"]["batch_jobs"]["model_output"]
    assert signal["status"] == "unavailable"
    assert signal["models"] == []
