"""阶段 5 接口：用户确认后运行兜底分类器；结果持久化并带指纹，评分项变化后标记过期。

B 阶段起归类是 AI 任务（kind=unit_classification）：每 3 个单元一个条目，由统一执行
模型领取；条目成功即合进当前草稿的归类建议。
"""

import pytest

from backend.app.db import models
from backend.app.services.ai_tasks import service as ai_task_service
from backend.app.tests.test_rubric_parse_coverage_api import _import
from backend.app.tests.test_rubric_parse_coverage_api import _rules_with_blocking_row
from backend.app.tests.test_rubric_unit_classifier import FakeScorer
from backend.app.tests.test_rubric_unit_classifier import _label_all


UNITS = ["xlsx:评分规则!R4C4", "xlsx:评分规则!R4C2"]


@pytest.fixture
def use_scorer(monkeypatch):
    def install(scorer):
        monkeypatch.setattr(ai_task_service, "get_llm_scorer", lambda *a, **k: scorer)
        return scorer
    return install


def _submit(client, rubric_id, **params):
    regenerate = bool(params.get("rejudge"))
    return client.post(f"/api/rubrics/{rubric_id}/ai-tasks", json={
        "kind": "unit_classification", "params": params, "regenerate": regenerate,
    })


def _drain(client):
    from backend.app.services.work_queue.runner import execute_next

    while execute_next(client.session_factory) is not None:
        pass


def _run(client, rubric_id, **params):
    """提交并执行完；返回 (提交响应, 结束后的任务)。"""
    response = _submit(client, rubric_id, **params)
    if response.status_code >= 400:
        return response, None
    _drain(client)
    return response, client.get(f"/api/ai-tasks/{response.json()['id']}").json()


def _classifications(client, rubric_id):
    return client.get(f"/api/rubrics/{rubric_id}/parse-coverage").json()["unit_classifications"]


def test_classification_is_persisted_and_visible_after_reload(client, use_scorer):
    use_scorer(FakeScorer(_label_all("rule", "C02", "high")))
    rubric_id = _import(client, _rules_with_blocking_row())["rubric"]["id"]
    response, task = _run(client, rubric_id)
    assert response.status_code == 202, response.text
    assert task["status"] == "succeeded"
    assert task["result"]["classified_count"] == 2
    stored = _classifications(client, rubric_id)
    assert [r["unit_id"] for r in stored["results"]] == UNITS
    assert stored["stale"] is False
    coverage = client.get(f"/api/rubrics/{rubric_id}/parse-coverage").json()
    # 分类只是建议：单元仍未认领、门禁仍在
    assert coverage["unclaimed_summary"]["blocking"] == 1


def test_units_are_sent_three_per_item(client, use_scorer):
    scorer = use_scorer(FakeScorer(_label_all()))
    rubric_id = _import(client, _rules_with_blocking_row())["rubric"]["id"]
    response = _submit(client, rubric_id)
    task = response.json()
    assert task["total_items"] == 1  # 2 个单元：一批
    assert task["scope"]["unit_ids"] == sorted(UNITS)
    assert scorer.calls == []  # 提交不调用模型


def test_classification_scope_can_be_narrowed(client, use_scorer):
    scorer = use_scorer(FakeScorer(_label_all()))
    rubric_id = _import(client, _rules_with_blocking_row())["rubric"]["id"]
    response, _task = _run(client, rubric_id, unit_ids=["xlsx:评分规则!R4C4"])
    assert response.status_code == 202, response.text
    assert [u["unit_id"] for u in scorer.calls[0][1]["units"]] == ["xlsx:评分规则!R4C4"]


def test_classification_becomes_stale_when_criteria_change(client, use_scorer):
    use_scorer(FakeScorer(_label_all()))
    rubric = _import(client, _rules_with_blocking_row())["rubric"]
    assert _run(client, rubric["id"])[1]["status"] == "succeeded"
    workspace = client.get(f"/api/rubrics/{rubric['id']}/review-workspace").json()
    criteria = rubric["criteria"]
    criteria[0]["name"] = "研究设计"
    recompiled = client.post(f"/api/rubrics/{rubric['id']}/recompile", json={
        "supersedes_compilation_id": workspace["compilation_id"], "version": "v2", "criteria": criteria,
        "atomic_rules": [{"id": r["id"], "content_token": r["content_token"], "changes": {"rule_text": r["rule_text"]}}
                         for r in workspace["rules"]]})
    assert recompiled.status_code == 200, recompiled.text
    assert _classifications(client, rubric["id"])["stale"] is True


def test_mock_connection_is_refused(client):
    rubric_id = _import(client, _rules_with_blocking_row())["rubric"]["id"]
    response = _submit(client, rubric_id)
    assert response.status_code == 503
    assert "AI_CONNECTION_MISSING" in response.text


def test_rubric_without_ledger_or_nothing_to_classify(client, use_scorer):
    use_scorer(FakeScorer(_label_all()))
    created = client.post("/api/rubrics", json={
        "name": "手工", "version": "v1", "total_score": 10,
        "criteria": [{"code": "T01", "name": "论证", "max_score": 10, "scoring_mode": "deductive",
                      "deduction_rules_structured": [{"match": "缺少论证", "points": 2, "reason": "合成"}]}]})
    assert _submit(client, created.json()["id"]).status_code == 404
    rubric_id = _import(client, _rules_with_blocking_row(), name="空范围")["rubric"]["id"]
    assert _submit(client, rubric_id, unit_ids=["nothing"]).status_code == 422


def test_partial_rejudge_keeps_other_valid_suggestions(client, use_scorer):
    use_scorer(FakeScorer(_label_all('rule', 'C02'), _label_all('requirement', 'C01')))
    rid = _import(client, _rules_with_blocking_row())['rubric']['id']
    assert _run(client, rid)[1]['status'] == 'succeeded'
    # 显式勾选重新判断一条：其余单元的有效建议保留。
    response, task = _run(client, rid, unit_ids=['xlsx:评分规则!R4C4'], rejudge=True)
    assert response.status_code == 202, response.text
    assert task['status'] == 'succeeded'
    results = {item['unit_id']: item for item in _classifications(client, rid)['results']}
    assert results['xlsx:评分规则!R4C2']['suggested_criterion'] == 'C02'
    assert results['xlsx:评分规则!R4C4']['suggested_criterion'] == 'C01'
    assert _classifications(client, rid)['stale'] is False


def test_continuing_skips_units_that_already_have_suggestions(client, use_scorer):
    scorer = use_scorer(FakeScorer(_label_all('rule', 'C02'), _label_all('rule', 'C02')))
    rid = _import(client, _rules_with_blocking_row())['rubric']['id']
    assert _run(client, rid, unit_ids=['xlsx:评分规则!R4C4'])[1]['status'] == 'succeeded'
    response, task = _run(client, rid)
    assert task['status'] == 'succeeded'
    # 续跑：已拿到建议的单元不再付费重跑。
    assert response.status_code == 202
    assert task['scope']['unit_ids'] == ['xlsx:评分规则!R4C2']
    assert [u['unit_id'] for u in scorer.calls[-1][1]['units']] == ['xlsx:评分规则!R4C2']
    everything = _submit(client, rid).json()
    assert (everything['status'], everything['total_items']) == ('succeeded', 0)
    assert sorted(everything['result']['skipped_unit_ids']) == sorted(UNITS)


def test_units_already_in_a_running_task_are_not_sent_twice(client, use_scorer):
    use_scorer(FakeScorer(_label_all()))
    rid = _import(client, _rules_with_blocking_row())['rubric']['id']
    first = _submit(client, rid).json()
    # 另一个标签页对同一批单元再点一次：全部被占用，返回进行中的那个任务。
    again = _submit(client, rid, unit_ids=['xlsx:评分规则!R4C2'], rejudge=False)
    assert again.status_code == 200
    assert again.json()['id'] == first['id']


def test_the_synchronous_classification_endpoint_is_retired(client):
    rid = _import(client, _rules_with_blocking_row())['rubric']['id']
    response = client.post(f"/api/rubrics/{rid}/unit-classifications", json={})
    assert response.status_code == 410
    assert response.json()['detail']['code'] == 'ENDPOINT_RETIRED'


def test_a_rate_limited_batch_waits_and_then_merges(client, use_scorer):
    from datetime import timedelta
    from backend.app.db.models import utcnow
    from backend.app.services.llm.errors import ProviderCallError
    from backend.app.tests.test_ai_tasks import _provider_error
    from backend.app.services.work_queue.runner import execute_next

    class Limited(FakeScorer):
        def complete_json(self, instructions, payload):
            if not self.calls:
                self.calls.append((instructions, payload))
                raise _provider_error("rate_limited", retry_after="9")
            return super().complete_json(instructions, payload)

    assert ProviderCallError
    use_scorer(Limited(_label_all('rule', 'C02')))
    rid = _import(client, _rules_with_blocking_row())['rubric']['id']
    task = _submit(client, rid).json()
    assert execute_next(client.session_factory) is not None
    with client.session_factory() as session:
        item = session.get(models.AITaskItem, task['items'][0]['id'])
        assert (item.status, item.deferral_count) == ('pending', 1)
        assert item.not_before > utcnow() + timedelta(seconds=5)
    from backend.app.services.work_queue.claim import claim_next_item
    from backend.app.services.ai_tasks.execution import _execute
    claimed = claim_next_item(client.session_factory, now=utcnow() + timedelta(seconds=10))
    _execute(client.session_factory, claimed)
    assert client.get(f"/api/ai-tasks/{task['id']}").json()['status'] == 'succeeded'
    assert len(_classifications(client, rid)['results']) == 2


def test_parallel_batches_merge_after_provider_calls_without_lost_results(client, tmp_path):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.app.services.rubric_import.parse_state import run_unit_classification

    rid = _import(client, _rules_with_blocking_row())["rubric"]["id"]
    path = tmp_path / "parallel-classification.db"
    # Independent connections, unlike the default in-memory StaticPool fixture.
    source = client.session_factory.kw["bind"].raw_connection()
    with sqlite3.connect(path) as target:
        source.driver_connection.backup(target)
    source.close()
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    sessions = sessionmaker(bind=engine, autoflush=False)
    barrier = Barrier(2)

    class ConcurrentScorer:
        provider = "fake"
        model_name = "test"
        def complete_json(self, instructions, payload):
            # Both requests must reach the provider before either saves.
            barrier.wait(timeout=5)
            return _label_all("rule", "C02")(payload)

    def run(uid):
        with sessions() as session:
            result = run_unit_classification(session, rid, ConcurrentScorer(), unit_ids=[uid], actor_id="test")
            session.commit()
            return result

    ids = ["xlsx:评分规则!R4C4", "xlsx:评分规则!R4C2"]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(run, ids))
        assert sorted(len(r["results"]) for r in responses) == [1, 2]
        from backend.app.services.rubric_import.parse_state import current_compilation
        with sessions() as session:
            stored = current_compilation(session, rid).raw_model_output["unit_classifications"]
            assert {r["unit_id"] for r in stored["results"]} == set(ids)
    finally:
        engine.dispose()


def test_classification_fails_the_batch_when_sources_change_during_the_model_call(client, monkeypatch):
    from backend.app.services.rubric_import.parse_state import current_compilation
    rid = _import(client, _rules_with_blocking_row())["rubric"]["id"]

    class ChangingScorer:
        provider = "fake"
        model_name = "fake"

        def complete_json(self, instructions, payload):
            with client.session_factory() as session:
                compilation = current_compilation(session, rid)
                from copy import deepcopy
                raw = deepcopy(compilation.raw_parse_output)
                for unit in raw["source_ledger"]["units"]:
                    if unit["unit_id"] == "xlsx:评分规则!R4C4":
                        unit["text"] = "更新后的原文扣2分"
                compilation.raw_parse_output = raw
                session.commit()
            return _label_all("rule", "C02")(payload)

    monkeypatch.setattr(ai_task_service, "get_llm_scorer", lambda *a, **k: ChangingScorer())
    response, task = _run(client, rid, unit_ids=["xlsx:评分规则!R4C4"])
    assert response.status_code == 202, response.text
    assert task["status"] == "failed"
    assert task["error_code"] == "CLASSIFICATION_INPUT_CHANGED"
    assert not (_classifications(client, rid) or {}).get("results")


def test_classification_tasks_from_several_tabs_share_the_connection_limit(client, use_scorer):
    from backend.app.services import platform_llm
    from backend.app.services.work_queue.claim import claim_next_item

    use_scorer(FakeScorer(_label_all(), _label_all()))
    with client.session_factory() as session:
        platform_llm.set_config(
            session, provider_type="openai_compatible", base_url="https://api.example.test/v1",
            model_name="platform-model", api_key="platform-key", configured_by="admin-1",
            provider_options={"max_concurrency": 1},
        )
        session.commit()
    rid = _import(client, _rules_with_blocking_row())['rubric']['id']
    # 两个标签页各归类一条：两个任务、各一批，但同一时刻只能有一批在调模型。
    first = _submit(client, rid, unit_ids=[UNITS[0]]).json()
    second = _submit(client, rid, unit_ids=[UNITS[1]]).json()
    assert first['id'] != second['id']
    assert claim_next_item(client.session_factory) is not None
    assert claim_next_item(client.session_factory) is None
