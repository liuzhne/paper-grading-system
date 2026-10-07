"""阶段 0：token 计量、评分前预估与上限、规则级决策账本。

全部使用本地假打分器 / Mock，不调用任何真实模型。
"""

from copy import deepcopy
from datetime import datetime
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

from backend.app.core.config import settings
from backend.app.db import models
from backend.app.services.llm import rate_limit
from backend.app.services.llm.errors import ProviderCallError
from backend.app.services.llm.mock import MockLLMScorer
from backend.app.services.llm.openai_adapter import OpenAIResponsesScorer
from backend.app.services.llm.openai_compatible_adapter import OpenAICompatibleChatScorer
from backend.app.services.llm.usage import TokenBudgetExceededError
from backend.app.services.llm.usage import UsageMeter
from backend.app.services.llm.usage import usage_delta
from backend.app.services.scoring import engine as scoring_engine
from backend.app.services.scoring import usage_estimate
from backend.app.services.scoring.core.contracts import ScoringRequest
from backend.app.services.scoring.core.decision_identity import rule_decision_identity
from backend.app.services.scoring.core.failures import project_rule_execution_failure
from backend.app.services.scoring.core.rule_executor import execute_rule_plan
from backend.app.services.scoring.core.rule_executor import prompt_envelope_for_rule
from backend.app.services.scoring.decision_ledger import DatabaseDecisionLedger
from backend.app.services.scoring.decision_ledger import bypass_ledger_reads
from backend.app.services.scoring.profiles.thesis import ThesisLLMRuntime
from backend.app.tests import test_m4_rule_executor as m4
from backend.app.tests.conftest import create_legacy_unversioned_rubric_fixture
from backend.app.tests.conftest import make_sample_docx
from backend.app.tests.test_m0_characterization import M0_RUBRIC


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# ---------------------------------------------------------------- 0a 计量


def test_usage_meter_counts_tokens_requests_and_failures():
    meter = UsageMeter()
    meter.record_success({"prompt_tokens": 100, "completion_tokens": 20})
    meter.record_success({"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 9})
    before = meter.snapshot()
    meter.record_failure()

    assert before == {
        "prompt_tokens": 105,
        "completion_tokens": 21,
        "total_tokens": 129,
        "request_count": 2,
        "failure_count": 0,
    }
    assert usage_delta(meter.snapshot(), before) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "request_count": 1,
        "failure_count": 1,
    }


class _ScriptedClient:
    def __init__(self, *responses):
        self.responses = list(responses)

    def post(self, url, headers, json, **kwargs):
        status, body = self.responses.pop(0)
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    def close(self):
        pass


def test_compatible_adapter_meters_answered_and_rejected_requests():
    rate_limit.reset_provider_runtime_for_tests()
    client = _ScriptedClient(
        (200, {"id": "a", "choices": [], "usage": {"prompt_tokens": 70, "completion_tokens": 7, "total_tokens": 77}}),
        (400, {"error": {"message": "Parameter 'top_p'=1.0 is not supported"}}),
    )
    scorer = OpenAICompatibleChatScorer(
        api_key="test-key",
        base_url="https://meter-compatible.invalid/v1",
        model_name="meter-model",
        client=client,
    )

    scorer._post_with_retry({"model": "meter-model"}, attempts_limit=1)
    with pytest.raises(ProviderCallError):
        scorer._post_with_retry({"model": "meter-model"}, attempts_limit=1)

    assert scorer.usage_meter.snapshot() == {
        "prompt_tokens": 70,
        "completion_tokens": 7,
        "total_tokens": 77,
        "request_count": 2,
        "failure_count": 1,
    }
    rate_limit.reset_provider_runtime_for_tests()


def test_responses_adapter_meters_input_and_output_tokens():
    rate_limit.reset_provider_runtime_for_tests()
    client = _ScriptedClient(
        (200, {"id": "r", "usage": {"input_tokens": 300, "output_tokens": 40, "total_tokens": 340}}),
    )
    scorer = OpenAIResponsesScorer(
        api_key="test-key",
        base_url="https://meter-responses.invalid/v1",
        model_name="meter-model",
        client=client,
    )

    scorer._post_with_retry({"model": "meter-model"}, attempts_limit=1)

    snapshot = scorer.usage_meter.snapshot()
    assert (snapshot["prompt_tokens"], snapshot["completion_tokens"]) == (300, 40)
    assert snapshot["request_count"] == 1
    rate_limit.reset_provider_runtime_for_tests()


class _Envelope:
    def __init__(self, rule_code="R1"):
        self.value = {"atomic_rule_snapshot": {"rule_code": rule_code, "criterion_code": "C1"}}

    def to_mapping(self):
        return deepcopy(self.value)


class _MeteredScorer:
    provider = "fake-provider"
    model_name = "fake-model"

    def __init__(self, prompt_tokens=600):
        self.usage_meter = UsageMeter()
        self.prompt_tokens = prompt_tokens
        self.calls = 0

    def score_core_envelope(self, *, envelope):
        self.calls += 1
        self.usage_meter.record_success(
            {"prompt_tokens": self.prompt_tokens, "completion_tokens": 20}
        )
        return {"ok": True}


def test_paper_runtime_records_call_usage_and_stops_at_the_input_cap():
    scorer = _MeteredScorer()
    # A shared scorer already spent tokens on an earlier paper; only this
    # paper's usage counts against its cap.
    scorer.usage_meter.record_success({"prompt_tokens": 5000})
    runtime = ThesisLLMRuntime(scorer, input_token_cap=1000)

    runtime.score(envelope=_Envelope())
    assert runtime.last_call_usage["prompt_tokens"] == 600
    runtime.score(envelope=_Envelope())
    with pytest.raises(TokenBudgetExceededError) as caught:
        runtime.score(envelope=_Envelope())

    assert scorer.calls == 2
    assert (caught.value.used, caught.value.cap) == (1200, 1000)
    assert project_rule_execution_failure(caught.value)[0] == "TOKEN_BUDGET_EXCEEDED"


def test_incomplete_run_reports_the_paper_token_cap():
    from types import SimpleNamespace

    from backend.app.services.batch_scoring import jobs

    error = jobs._incomplete_scoring_error(
        [
            SimpleNamespace(
                status="failed_exhausted",
                provider_error={"code": "TOKEN_BUDGET_EXCEEDED"},
            )
        ]
    )
    assert error.code == "TOKEN_BUDGET_EXCEEDED"
    assert "单篇上限" in str(error)
    assert "复用" in str(error)


# ---------------------------------------------------------------- 0c 账本：Core


def test_decision_identity_changes_with_any_envelope_input():
    base = {"runtime_identity": {"provider": {"model": "kimi-k3"}}, "x": 1}
    same = deepcopy(base)
    other_model = deepcopy(base)
    other_model["runtime_identity"]["provider"]["model"] = "qwen3-max"

    assert rule_decision_identity(base) == rule_decision_identity(same)
    assert rule_decision_identity(base) != rule_decision_identity(other_model)


class _MemoryLedger:
    def __init__(self):
        self.rows = {}
        self.puts = []

    def get(self, *, decision_identity):
        value = self.rows.get(decision_identity)
        return None if value is None else deepcopy(value)

    def put(self, *, decision_identity, rule_code, response, usage=None):
        self.rows[decision_identity] = deepcopy(response)
        self.puts.append(rule_code)


def _triggered_request_and_response():
    request = m4._request_for((m4._criterion(), m4._deduct_rule()))
    response = m4._semantic_response(
        m4.RULE_CODE, occurrences=[m4._quote_occurrence(request)]
    )
    return request, response


def _run(request, runtime, ledger):
    return m4._result_mapping(
        execute_rule_plan(
            request=deepcopy(request),
            checker_registry=m4._CheckerRegistry(),
            llm_runtime=runtime,
            profile=m4._TechnicalProposalProfile(),
            decision_ledger=ledger,
        )
    )


def test_executor_replays_a_validated_decision_without_calling_the_provider():
    request, response = _triggered_request_and_response()
    ledger = _MemoryLedger()

    first_runtime = m4._SemanticRuntime({m4.RULE_CODE: response})
    first = _run(request, first_runtime, ledger)
    # No scripted responses: any provider call would raise KeyError.
    second_runtime = m4._SemanticRuntime({})
    second = _run(request, second_runtime, ledger)

    assert len(first_runtime.envelopes) == 1
    assert second_runtime.envelopes == []
    assert ledger.puts == [m4.RULE_CODE]
    assert second == first
    assert m4._decision(second)["status"] == "triggered"


def test_executor_never_records_a_decision_it_rejected():
    request, response = _triggered_request_and_response()
    response["occurrences"][0]["finding_code"] = "NOT_AUTHORIZED"
    ledger = _MemoryLedger()

    result = _run(request, m4._SemanticRuntime({m4.RULE_CODE: response}), ledger)

    assert m4._decision(result)["status"] == "invalid"
    assert ledger.puts == []


def test_a_stored_decision_that_no_longer_validates_is_judged_again():
    request, response = _triggered_request_and_response()
    value = ScoringRequest.from_mapping(deepcopy(request)).to_mapping()
    node = value["plan"]["nodes"][0]
    identity = rule_decision_identity(
        prompt_envelope_for_rule(
            request=value, node=node, profile=m4._TechnicalProposalProfile()
        )
    )
    stale = deepcopy(response)
    stale["occurrences"][0]["finding_code"] = "NOT_AUTHORIZED"
    ledger = _MemoryLedger()
    ledger.rows[identity] = stale

    runtime = m4._SemanticRuntime({m4.RULE_CODE: response})
    result = _run(request, runtime, ledger)

    assert len(runtime.envelopes) == 1
    assert m4._decision(result)["status"] == "triggered"
    assert ledger.rows[identity] == response


# ---------------------------------------------------------------- 0c 账本：数据库


@pytest.fixture
def ledger_engine():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    models.Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_database_ledger_scopes_expires_and_tracks_replays(ledger_engine):
    clock = [datetime(2026, 10, 5, 12, 0, 0)]
    now = lambda: clock[0]  # noqa: E731
    first = "a" * 64
    second = "b" * 64
    ledger = DatabaseDecisionLedger(
        bind=ledger_engine,
        organization_id=None,
        scope="connection:A",
        ttl_days=30,
        now=now,
    )

    assert ledger.get(decision_identity=first) is None
    ledger.put(
        decision_identity=first,
        rule_code="R1",
        response={"status": "not_triggered"},
        usage={"prompt_tokens": 11, "completion_tokens": 2},
    )
    assert ledger.get(decision_identity=first) == {"status": "not_triggered"}
    assert (ledger.hits, ledger.misses, ledger.writes) == (1, 1, 1)
    assert ledger.reused_rule_codes == {"R1"}
    assert ledger.known_identities([first, second]) == {first}

    other_connection = DatabaseDecisionLedger(
        bind=ledger_engine, organization_id=None, scope="connection:B", ttl_days=30, now=now
    )
    assert other_connection.get(decision_identity=first) is None

    rescoring = DatabaseDecisionLedger(
        bind=ledger_engine,
        organization_id=None,
        scope="connection:A",
        ttl_days=30,
        read_enabled=False,
        now=now,
    )
    assert rescoring.get(decision_identity=first) is None
    rescoring.put(decision_identity=first, rule_code="R1", response={"status": "triggered"})
    assert ledger.get(decision_identity=first) == {"status": "triggered"}

    clock[0] += timedelta(days=31)
    assert ledger.get(decision_identity=first) is None
    ledger.put(decision_identity=second, rule_code="R2", response={"status": "x"})
    with ledger_engine.connect() as connection:
        identities = connection.execute(
            select(models.RuleDecisionLedger.decision_identity_hash)
        ).scalars().all()
    # The expired row of this scope was purged by the write.
    assert identities == [second]


def test_database_ledger_fails_soft(ledger_engine):
    ledger = DatabaseDecisionLedger(
        bind=ledger_engine, organization_id=None, scope="connection:A", ttl_days=30
    )
    models.RuleDecisionLedger.__table__.drop(ledger_engine)

    assert ledger.get(decision_identity="c" * 64) is None
    ledger.put(decision_identity="c" * 64, rule_code="R", response={"status": "x"})
    assert ledger.writes == 0


def test_put_after_a_rejected_replay_is_not_counted_as_reuse(ledger_engine):
    ledger = DatabaseDecisionLedger(
        bind=ledger_engine, organization_id=None, scope="connection:A", ttl_days=30
    )
    ledger.put(decision_identity="d" * 64, rule_code="R", response={"status": "x"})
    ledger.get(decision_identity="d" * 64)
    ledger.put(decision_identity="d" * 64, rule_code="R", response={"status": "y"})

    assert ledger.reused_rule_codes == set()


# ---------------------------------------------------------------- Core 论文路径端到端


class _CountingScorer(MockLLMScorer):
    """Non-mock provider stand-in: every atomic rule judged ``not_triggered``.

    Legacy compatibility nodes of the M0 rubric still use the Mock
    ``score_criterion``; only Core atomic-rule calls are counted and metered.
    """

    provider = "fake-provider"
    model_name = "fake-model"
    model_version = "fake-v1"
    temperature = 0
    max_output_tokens = 512
    calls = 0

    def __init__(self):
        self.usage_meter = UsageMeter()

    def score_core_envelope(self, *, envelope):
        type(self).calls += 1
        self.usage_meter.record_success({"prompt_tokens": 100, "completion_tokens": 10})
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
def core_paper(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")
    monkeypatch.setattr(settings, "SCORING_ENGINE_MODE", "core")
    monkeypatch.setattr(_CountingScorer, "calls", 0)
    monkeypatch.setattr(
        scoring_engine, "get_llm_scorer", lambda *args, **kwargs: _CountingScorer()
    )
    rubric_id = create_legacy_unversioned_rubric_fixture(client, M0_RUBRIC)
    batch = client.post(
        "/api/batches", json={"name": "decision ledger", "rubric_id": rubric_id}
    )
    assert batch.status_code == 200, batch.text
    upload = client.post(
        "/api/papers/upload",
        data={"batch_id": batch.json()["id"]},
        files={"file": ("ledger.docx", make_sample_docx().getvalue(), DOCX_MIME)},
    )
    assert upload.status_code == 200, upload.text
    return batch.json()["id"], upload.json()["id"]


def _semantic_tasks(db, run_id):
    return db.scalars(
        select(models.RuleScoringTask).where(
            models.RuleScoringTask.scoring_run_id == run_id,
            models.RuleScoringTask.judge_type == "semantic",
        )
    ).all()


def test_retry_replays_decisions_and_meters_only_what_was_paid(client, core_paper):
    _batch_id, paper_id = core_paper

    first = client.post("/api/papers/%s/score" % paper_id)
    assert first.status_code == 200, first.text
    judged = _CountingScorer.calls
    assert judged > 0

    retry = client.post("/api/scoring-runs/%s/retry" % first.json()["id"])
    assert retry.status_code == 200, retry.text
    assert _CountingScorer.calls == judged

    with client.session_factory() as db:
        first_run = db.get(models.ScoringRun, first.json()["id"])
        retry_run = db.get(models.ScoringRun, retry.json()["id"])
        assert (first_run.prompt_tokens, first_run.completion_tokens) == (
            judged * 100,
            judged * 10,
        )
        assert retry_run.prompt_tokens == 0
        assert not any(task.decision_reused for task in _semantic_tasks(db, first_run.id))
        replayed = _semantic_tasks(db, retry_run.id)
        assert len(replayed) == judged
        assert all(task.decision_reused for task in replayed)
        assert db.scalar(select(func.count()).select_from(models.RuleDecisionLedger)) == judged

        # Explicit rescoring judges every rule again.
        with bypass_ledger_reads():
            rescored = scoring_engine.retry_score_paper(db, retry_run.id)
        assert _CountingScorer.calls == 2 * judged
        assert not any(task.decision_reused for task in _semantic_tasks(db, rescored.id))


def test_estimate_counts_calls_locally_and_reports_reuse(client, core_paper):
    batch_id, paper_id = core_paper

    estimate = client.get("/api/batches/%s/score-estimate" % batch_id)
    assert estimate.status_code == 200, estimate.text
    body = estimate.json()
    assert _CountingScorer.calls == 0  # 估算不调用模型
    assert body["paper_count"] == 1
    assert body["calls"] > 0 and body["reused_rules"] == 0
    assert body["estimated_input_tokens"] > 0
    paper_estimate = body["papers"][0]
    assert paper_estimate["supported"] is True

    scored = client.post("/api/papers/%s/score" % paper_id)
    assert scored.status_code == 200, scored.text
    with client.session_factory() as db:
        paper = db.get(models.Paper, paper_id)
        after = usage_estimate.estimate_paper(db, paper, reuse=True)
        assert after["reused_rules"] == paper_estimate["calls"]
        assert after["calls"] == 0
        without_reuse = usage_estimate.estimate_paper(db, paper, reuse=False)
        assert without_reuse["calls"] == paper_estimate["calls"]

    rescore = client.get("/api/batches/%s/score-estimate?rescore=true" % batch_id)
    assert rescore.json()["calls"] == paper_estimate["calls"]


def test_estimate_marks_legacy_path_papers_unsupported(client, core_paper, monkeypatch):
    batch_id, _paper_id = core_paper
    monkeypatch.setattr(settings, "SCORING_ENGINE_MODE", "legacy")

    body = client.get("/api/batches/%s/score-estimate" % batch_id).json()

    assert body["papers"][0]["supported"] is False
    assert body["papers"][0]["reason"] == "legacy_path"
    assert body["estimated_input_tokens"] == 0


def test_token_caps_refuse_to_queue_a_job(client, core_paper, monkeypatch):
    batch_id, _paper_id = core_paper
    monkeypatch.setattr(settings, "SCORING_MAX_INPUT_TOKENS_PER_PAPER", 1)

    refused = client.post("/api/batches/%s/score-jobs" % batch_id, json={})

    assert refused.status_code == 409, refused.text
    assert "超过上限" in refused.json()["detail"]
    assert _CountingScorer.calls == 0
    with client.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(models.BatchScoringJob)) == 0


def test_no_cap_means_no_estimate_on_job_creation(client, core_paper, monkeypatch):
    batch_id, _paper_id = core_paper

    def _fail(*args, **kwargs):
        raise AssertionError("estimate must not run without a configured cap")

    monkeypatch.setattr(usage_estimate, "estimate_batch", _fail)

    created = client.post("/api/batches/%s/score-jobs" % batch_id, json={})

    assert created.status_code == 201, created.text
