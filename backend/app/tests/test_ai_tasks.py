"""A2：AI 任务与起草迁移到任务（方案第 4–8、11 节）。

任务由统一执行模型领取：这里用 ``run_worker_cycle`` 驱动（与 worker 相同的代码路径），
模型客户端替换成假的起草器。
"""

from __future__ import annotations

from datetime import timedelta
import importlib
from pathlib import Path
import re

import pytest

from backend.app.core.contract import API_CONTRACT_HEADER
from backend.app.core.contract import API_CONTRACT_VERSION
from backend.app.db import models
from backend.app.db.models import utcnow
from backend.app.services.llm.errors import ProviderCallError
from backend.app.services.llm.errors import ProviderError
from backend.app.tests.test_template_ai_rule_refactor import _criterion


ROOT = Path(__file__).resolve().parents[3]
service = importlib.import_module("backend.app.services.ai_tasks.service")
execution = importlib.import_module("backend.app.services.ai_tasks.execution")
runner = importlib.import_module("backend.app.services.work_queue.runner")
claim = importlib.import_module("backend.app.services.work_queue.claim")
sweep = importlib.import_module("backend.app.services.work_queue.sweep")


def _group(code="T02-R1"):
    return {
        "group_code": code,
        "issue": "核心需求缺失",
        "mutex_group": code + "-SEVERITY",
        "cap_points": 6,
        "rules": [
            {
                "severity": "minor",
                "trigger": "个别需求描述不完整",
                "points": 2,
                "reason": "局部缺失",
                "repeat_policy": "once",
                "source": "ai_inferred",
                "source_refs": ["/criteria/T02/description"],
            },
            {
                "severity": "severe",
                "trigger": "核心需求完全缺失",
                "points": 6,
                "reason": "核心缺失",
                "repeat_policy": "once",
                "source": "ai_inferred",
                "source_refs": ["/criteria/T02/description"],
            },
        ],
    }


def _provider_error(code, *, status=429, retry_after=None):
    return ProviderCallError(
        "fake",
        ProviderError(
            schema_version="provider-error@1",
            code=code,
            scope="provider",
            retryable=code == "rate_limited",
            reducible=False,
            http_status=status,
            provider_error_type=None,
            provider_error_code=None,
            message=code,
            provider_request_id=None,
            retry_after=retry_after,
            rate_limit_headers={},
        ),
    )


class FakeDrafter:
    """按调用顺序返回 ``script`` 里的结果；元素是异常时抛出它。"""

    provider = "openai_compatible"
    model_name = "draft-task-model"
    model_version = "test-v1"

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []

    def complete_json(self, instructions, payload):
        self.calls.append((instructions, payload))
        result = self.script.pop(0) if self.script else {"rule_groups": [_group()]}
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def drafter(monkeypatch):
    fake = FakeDrafter()
    monkeypatch.setattr(service, "get_llm_scorer", lambda *args, **kwargs: fake)
    return fake


def _rubric(client):
    created = client.post(
        "/api/rubrics",
        json={
            "name": "AI task rubric %s" % models.new_id()[:6],
            "version": "v1",
            "total_score": 20,
            "criteria": [_criterion(scoring_mode="review_only")],
        },
    )
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _create(client, rubric_id, **overrides):
    body = {"kind": "rule_draft", "params": {"criterion": _criterion()}, **overrides}
    return client.post(f"/api/rubrics/{rubric_id}/ai-tasks", json=body)


def _drain(client, *, now=None):
    count = 0
    while runner.execute_next(client.session_factory) is not None:
        count += 1
        assert count < 50
    return count


def _task(client, task_id):
    response = client.get(f"/api/ai-tasks/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


# ---- 提交、去重与执行 ----------------------------------------------------------


def test_submit_returns_a_task_at_once_and_the_worker_drafts_it(client, drafter):
    rubric_id = _rubric(client)
    created = _create(client, rubric_id)
    assert created.status_code == 202, created.text
    task = created.json()
    assert task["status"] == "queued"
    assert task["model_name"] == "draft-task-model"
    assert task["total_items"] == len(task["items"]) == 2  # 说明一句 + 证据提示一条：两批
    assert task["result"] is None
    assert drafter.calls == []  # 请求里不调模型

    assert _drain(client) == 2
    done = _task(client, task["id"])
    assert done["status"] == "succeeded"
    assert (done["succeeded_count"], done["pending_count"]) == (2, 0)
    result = done["result"]
    assert result["status"] == "pending_confirmation"
    draft = result["draft"]
    # 合并沿用同步起草：规则组编号按“评分项-批次-组”稳定生成。
    assert [group["group_code"] for group in draft["rule_groups"]] == ["T02-B01-G01", "T02-B02-G01"]
    assert draft["generation_metadata"]["model_name"] == "draft-task-model"
    assert draft["generation_metadata"]["fingerprint"]
    assert all(rule["confirmed"] is False for group in draft["rule_groups"] for rule in group["rules"])


def test_a_repeated_submission_returns_the_same_task(client, drafter):
    rubric_id = _rubric(client)
    first = _create(client, rubric_id).json()
    # 双击、另一个标签页、刷新后重点：同指纹直接返回已有任务。
    again = _create(client, rubric_id)
    assert again.status_code == 200
    assert again.json()["id"] == first["id"]
    _drain(client)
    reused = _create(client, rubric_id)
    # 成功结果保留到发布，同指纹直接复用，不重复计费。
    assert reused.status_code == 200
    assert reused.json()["id"] == first["id"]
    assert reused.json()["status"] == "succeeded"
    assert len(drafter.calls) == 2


def test_regenerate_supersedes_the_old_task(client, drafter):
    rubric_id = _rubric(client)
    first = _create(client, rubric_id).json()
    second = _create(client, rubric_id, regenerate=True)
    assert second.status_code == 202
    assert second.json()["id"] != first["id"]
    old = _task(client, first["id"])
    assert old["status"] == "superseded"
    assert old["canceled_count"] == 2
    active = client.get(f"/api/rubrics/{rubric_id}/ai-tasks", params={"kind": "rule_draft", "active": 1})
    assert [value["id"] for value in active.json()] == [second.json()["id"]]


def test_a_criterion_that_needs_no_ai_succeeds_without_items(client, drafter):
    rubric_id = _rubric(client)
    response = _create(
        client,
        rubric_id,
        params={"criterion": _criterion(deduction_rules=["缺少需求说明，扣 3 分"])},
    )
    assert response.status_code == 202
    task = response.json()
    assert (task["status"], task["total_items"]) == ("succeeded", 0)
    assert task["result"]["status"] == "already_structured"
    assert drafter.calls == []


def test_a_mock_model_is_refused_with_an_actionable_problem(client):
    rubric_id = _rubric(client)
    response = _create(client, rubric_id)
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "AI_DRAFT_CONNECTION_MISSING"
    assert detail["retryable"] is False


# ---- 429、修正、失败与重试 ------------------------------------------------------


def test_rate_limit_defers_the_item_instead_of_waiting(client, drafter):
    drafter.script = [_provider_error("rate_limited", retry_after="7")]
    rubric_id = _rubric(client)
    task = _create(client, rubric_id).json()

    assert runner.execute_next(client.session_factory) is not None
    with client.session_factory() as session:
        deferred = session.get(models.AITaskItem, task["items"][0]["id"])
        assert deferred.status == "pending"
        assert deferred.deferral_count == 1
        wait = (deferred.not_before - utcnow()).total_seconds()
        assert 0 < wait <= 7
    # 名额释放后先执行另一批；被延后的那批要等到最早可执行时间。
    assert runner.execute_next(client.session_factory) is not None
    assert runner.execute_next(client.session_factory) is None
    later = utcnow() + timedelta(seconds=8)
    assert claim.claim_next_item(client.session_factory, now=later) is not None


def test_invalid_output_is_repaired_once_as_the_next_execution(client, drafter):
    drafter.script = [{"rule_groups": [{"issue": "缺字段"}]}]
    rubric_id = _rubric(client)
    task = _create(client, rubric_id).json()
    _drain(client)
    done = _task(client, task["id"])
    assert done["status"] == "succeeded"
    repair_prompt = drafter.calls[1][0]
    assert "上次输出未通过校验" in repair_prompt
    with client.session_factory() as session:
        item = session.get(models.AITaskItem, task["items"][0]["id"])
        assert item.repair_count == 1
        assert item.attempt_count == 2


def test_a_permanent_failure_keeps_successful_batches_and_retries_only_the_failed(client, drafter):
    drafter.script = [{"rule_groups": [_group()]}, _provider_error("quota_exhausted")]
    rubric_id = _rubric(client)
    task = _create(client, rubric_id).json()
    _drain(client)
    failed = _task(client, task["id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "AI_DRAFT_PROVIDER_ERROR"
    assert "额度" in failed["error_message"]
    assert (failed["succeeded_count"], failed["failed_count"]) == (1, 1)
    assert failed["result"] is None

    retried = client.post(f"/api/ai-tasks/{task['id']}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "queued"
    assert retried.json()["pending_count"] == 1
    calls_before = len(drafter.calls)
    _drain(client)
    assert len(drafter.calls) == calls_before + 1  # 只重跑失败的那批
    assert _task(client, task["id"])["status"] == "succeeded"


def test_timeouts_retry_once_then_fail(client, drafter):
    drafter.script = [_provider_error("request_timeout", status=None)] * 2 + [{"rule_groups": [_group()]}]
    rubric_id = _rubric(client)
    task = _create(client, rubric_id).json()
    _drain(client)
    done = _task(client, task["id"])
    assert done["status"] == "failed"
    first = next(item for item in done["items"] if item["ordinal"] == 0)
    assert (first["status"], first["attempt_count"]) == ("failed", 2)


def test_cancel_stops_pending_batches(client, drafter):
    rubric_id = _rubric(client)
    task = _create(client, rubric_id).json()
    claimed = claim.claim_next_item(client.session_factory)
    canceled = client.post(f"/api/ai-tasks/{task['id']}/cancel")
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"
    assert canceled.json()["canceled_count"] == 1
    # 进行中的那批跑完后不再继续，结果不合并。
    execution._execute(client.session_factory, claimed)
    after = _task(client, task["id"])
    assert after["status"] == "canceled"
    assert after["result"] is None
    # 已取消的任务不占指纹：重新提交得到新任务。
    assert _create(client, rubric_id).status_code == 202


def test_a_changed_connection_key_fails_the_task_with_its_cause(client, drafter):
    rubric_id = _rubric(client)
    task = _create(client, rubric_id).json()
    with client.session_factory() as session:
        row = session.get(models.AITask, task["id"])
        row.ai_connection_snapshot = {"ai_connection_id": "gone"}
        row.ai_connection_id = None
        session.commit()
    _drain(client)
    failed = _task(client, task["id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "AI_CONNECTION_MISSING"
    assert drafter.calls == []


def test_a_dead_execution_is_requeued_then_failed_after_the_stall_limit(client, drafter):
    rubric_id = _rubric(client)
    task = _create(client, rubric_id).json()
    clock = utcnow()
    for attempt in range(1, 4):
        claimed = claim.claim_next_item(client.session_factory, now=clock)
        assert claimed.item_id == task["items"][0]["id"] or attempt > 1
        clock = clock + timedelta(minutes=5)
        sweep.sweep_stale_items(client.session_factory, now=clock)
    with client.session_factory() as session:
        item = session.get(models.AITaskItem, claimed.item_id)
        assert item.status == "failed"
        assert item.error_code == "WORK_ITEM_STALLED"


# ---- 与批量评分共用名额与优先级 ----------------------------------------------------


def test_ai_items_are_claimed_before_batch_scoring_items(client, drafter):
    from backend.app.tests.test_m8_batch_scoring_jobs import _seed_batch
    from backend.app.services.batch_scoring.jobs import create_batch_scoring_job
    from backend.app.core.config import settings

    with client.session_factory() as session:
        batch_id, _rubric_id, _papers = _seed_batch(session, count=2, name="queue behind AI")
        create_batch_scoring_job(
            session, batch_id=batch_id, rescore=False, max_workers=1,
            observation_policy=None, actor_id=settings.DEFAULT_DEV_USER_ID,
        )
    rubric_id = _rubric(client)
    task = _create(client, rubric_id).json()
    first = claim.claim_next_item(client.session_factory)
    # 用户在页面上等 AI 结果：AI 条目优先。
    assert (first.kind, first.parent_id) == ("ai_task", task["id"])


def test_ai_items_count_against_the_same_source_limit(client, drafter):
    from backend.app.services import platform_llm

    with client.session_factory() as session:
        platform_llm.set_config(
            session, provider_type="openai_compatible", base_url="https://api.example.test/v1",
            model_name="platform-model", api_key="platform-key", configured_by="admin-1",
            provider_options={"max_concurrency": 1},
        )
        session.commit()
    rubric_id = _rubric(client)
    _create(client, rubric_id)
    assert claim.claim_next_item(client.session_factory) is not None
    # 平台模型声明同时 1 个：第二批要等第一批结束。
    assert claim.claim_next_item(client.session_factory) is None


# ---- 旧接口、版本守卫、发布清理 ----------------------------------------------------


def test_the_synchronous_draft_endpoint_is_retired(client):
    rubric_id = _rubric(client)
    response = client.post(f"/api/rubrics/{rubric_id}/draft-deduction-rules", json={"criteria": []})
    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "ENDPOINT_RETIRED"
    assert response.headers[API_CONTRACT_HEADER] == API_CONTRACT_VERSION


def test_frontend_and_backend_share_the_contract_version():
    source = (ROOT / "frontend/workbench/src/api/contract.js").read_text(encoding="utf-8")
    match = re.search(r'API_CONTRACT_VERSION\s*=\s*"([^"]+)"', source)
    assert match and match.group(1) == API_CONTRACT_VERSION


def test_publishing_a_rubric_deletes_its_ai_tasks(client, drafter):
    rubric_id = _rubric(client)
    _create(client, rubric_id)
    with client.session_factory() as session:
        assert service.delete_rubric_ai_tasks(session, rubric_id) == 1
        session.commit()
        assert session.query(models.AITask).count() == 0
        assert session.query(models.AITaskItem).count() == 0


def test_tasks_are_invisible_outside_their_organization(client, drafter):
    rubric_id = _rubric(client)
    task = _create(client, rubric_id).json()
    with client.session_factory() as session:
        other = models.Organization(name="另一个组织")
        session.add(other)
        session.flush()
        session.get(models.AITask, task["id"]).organization_id = other.id
        session.commit()
    from fastapi import HTTPException

    from backend.app.api.deps import CurrentPrincipal
    from backend.app.api.routes import ai_tasks as routes

    principal = CurrentPrincipal(
        user_id="someone", organization_id="org-x", organization_role="teacher", platform_role="user",
    )
    with client.session_factory() as session:
        with pytest.raises(HTTPException) as excinfo:
            routes._task_or_404(session, task["id"], principal)
        assert excinfo.value.status_code == 404


# ---- 规则写明来自 AI 与生成模型名 -------------------------------------------------


def test_adopted_ai_rules_record_origin_and_model(client):
    rubric_id = _rubric(client)
    predecessor = client.get(f"/api/rubrics/{rubric_id}/execution-draft").json()["active_compilation"]["id"]
    ai_rule = {
        "match": "核心需求完全缺失", "trigger": "核心需求完全缺失", "points": 6, "reason": "核心缺失",
        "severity": "severe", "repeat_policy": "once", "mutex_group": "T02-B01-G01-SEVERITY",
        "source": "ai_inferred", "source_refs": ["/criteria/T02/description"], "confirmed": True,
        "ai_origin": True, "ai_model": "draft-task-model",
    }
    user_rule = {
        "match": "缺少需求说明", "points": 3, "reason": "缺少需求说明", "repeat_policy": "once",
        "source": "user_text", "confirmed": True,
    }
    recompiled = client.post(
        f"/api/rubrics/{rubric_id}/recompile",
        json={
            "supersedes_compilation_id": predecessor,
            "version": "v1",
            "criteria": [_criterion(deduction_rules_structured=[ai_rule, user_rule])],
            "reason": "确认并应用 AI 起草的扣分细则",
        },
    )
    assert recompiled.status_code == 200, recompiled.text
    rules = {rule["rule_text"]: rule for rule in client.get(f"/api/rubrics/{rubric_id}/review-workspace").json()["rules"]}
    assert (rules["核心缺失"]["ai_origin"], rules["核心缺失"]["ai_model"]) == (True, "draft-task-model")
    assert (rules["缺少需求说明"]["ai_origin"], rules["缺少需求说明"]["ai_model"]) == (False, None)


def test_every_ai_task_write_route_is_role_gated():
    import inspect as source_inspect

    from backend.app.api.routes import ai_tasks as routes

    source = source_inspect.getsource(routes)
    blocks = [block for block in re.split(r"\n(?=@router\.)", source) if block.startswith("@router.post")]
    assert len(blocks) == 3
    for block in blocks:
        assert "require_organization_role" in block, block.split("\n", 2)[1]
