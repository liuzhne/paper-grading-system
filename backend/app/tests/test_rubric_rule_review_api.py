"""阶段 6.5 接口：规则审查运行、查看、豁免与“未经审查即发布”留痕。

C 阶段起审查是 AI 任务（kind=rule_review）：每个评分项一个条目，两个以上评分项时再加
一次跨项审查；全部条目结束后合并、编号并写进草稿。估算走单独的接口，不调用模型。
"""

import pytest
from sqlalchemy import select

from backend.app.db import models
from backend.app.services.ai_tasks import service as ai_task_service
from backend.app.tests.conftest import publish_rubric_via_api
from backend.app.tests.test_rubric_parse_coverage_api import _clean_rules
from backend.app.tests.test_rubric_parse_coverage_api import _import
from backend.app.tests.test_ai_tasks import _provider_error
from backend.app.tests.test_rubric_unit_classifier import FakeScorer


def _finding_for(payload):
    source = payload["sources"][0]
    return {"issues": [{"type": "undecidable", "rule_ids": ["R1"], "source_ids": [source["id"]],
                        "quote": source["text"][:4], "problem": "条件难以客观判断", "example": "", "severity": "low"}]}


@pytest.fixture
def use_scorer(monkeypatch):
    def install(*responses):
        scorer = FakeScorer(*responses)
        monkeypatch.setattr(ai_task_service, "get_llm_scorer", lambda *a, **k: scorer)
        return scorer
    return install


def _drain(client):
    from backend.app.services.work_queue.runner import execute_next

    while execute_next(client.session_factory) is not None:
        pass


def _review(client, rubric_id, scope="all", regenerate=False):
    response = client.post(f"/api/rubrics/{rubric_id}/ai-tasks", json={
        "kind": "rule_review", "params": {"scope": scope}, "regenerate": regenerate,
    })
    if response.status_code >= 400:
        return response, None
    _drain(client)
    return response, client.get(f"/api/ai-tasks/{response.json()['id']}").json()


def test_estimate_returns_prechecks_and_scope_without_model_call(client, use_scorer):
    scorer = use_scorer()
    rubric_id = _import(client, _clean_rules(), name="审查估算")["rubric"]["id"]
    response = client.post(f"/api/rubrics/{rubric_id}/rule-review/estimate", json={"scope": "all"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["criteria_codes"] == ["C01", "C02"]
    assert body["estimate"]["calls"] == 3  # 两个评分项 + 一次跨项审查
    assert body["prechecks"] == []
    assert scorer.calls == []


def test_review_task_has_one_item_per_criterion_plus_cross(client, use_scorer):
    scorer = use_scorer()
    rubric_id = _import(client, _clean_rules(), name="审查条目")["rubric"]["id"]
    response = client.post(f"/api/rubrics/{rubric_id}/ai-tasks", json={"kind": "rule_review", "params": {"scope": "all"}})
    assert response.status_code == 202, response.text
    task = response.json()
    assert task["total_items"] == 3
    assert [item["label"] for item in task["items"]] == ["C01", "C02", "__cross__"]
    assert scorer.calls == []  # 提交不调用模型
    again = client.post(f"/api/rubrics/{rubric_id}/ai-tasks", json={"kind": "rule_review", "params": {"scope": "all"}})
    assert again.status_code == 200 and again.json()["id"] == task["id"]  # 双击只建一个任务


def test_review_is_persisted_and_findings_can_be_dismissed(client, use_scorer):
    use_scorer(_finding_for, _finding_for, {"issues": []})
    rubric_id = _import(client, _clean_rules(), name="审查持久化")["rubric"]["id"]
    response, task = _review(client, rubric_id)
    assert response.status_code == 202, response.text
    assert task["status"] == "succeeded", task
    assert [f["criterion_code"] for f in task["result"]["findings"]] == ["C01", "C02"]
    view = client.get(f"/api/rubrics/{rubric_id}/rule-review").json()
    assert view["stale"] is False and len(view["findings"]) == 2
    assert [f["id"] for f in view["findings"]] == ["F1", "F2"]
    assert view["model"] == {"provider": "fake", "model_name": "fake-model"}
    dismissed = client.post(f"/api/rubrics/{rubric_id}/rule-review/findings/F1/dismiss", json={"reason": "已知且可接受"})
    assert dismissed.status_code == 200, dismissed.text
    statuses = {f["id"]: f["status"] for f in client.get(f"/api/rubrics/{rubric_id}/rule-review").json()["findings"]}
    assert statuses == {"F1": "dismissed", "F2": "open"}
    assert client.post(f"/api/rubrics/{rubric_id}/rule-review/findings/F9/dismiss",
                       json={"reason": "x"}).status_code == 404


def test_invalid_output_is_repaired_once_then_recorded_as_failed_criterion(client, use_scorer):
    # C01 两次都缺 issues：记为审查失败，其余评分项照常（与同步审查相同）。
    def respond(payload):
        if "criteria" in payload:
            return {"issues": []}
        return {"oops": 1} if payload["criterion"]["code"] == "C01" else _finding_for(payload)

    scorer = use_scorer(*[respond] * 6)
    rubric_id = _import(client, _clean_rules(), name="审查修正")["rubric"]["id"]
    _response, task = _review(client, rubric_id)
    assert task["status"] == "succeeded", task
    assert task["result"]["failed"] == ["C01"]
    assert [f["criterion_code"] for f in task["result"]["findings"]] == ["C02"]
    assert len(scorer.calls) == 4
    # 修正作为同一条目的下一次执行，带修正提示。
    assert "缺少 issues 数组" not in scorer.calls[0][0]
    assert "缺少 issues 数组" in scorer.calls[1][0]


def test_provider_failure_fails_the_task_and_retry_reruns_only_failed_items(client, use_scorer):
    def quota(_payload):
        raise _provider_error("quota_exhausted", status=402)

    scorer = use_scorer({"issues": []}, quota)
    rubric_id = _import(client, _clean_rules(), name="审查失败")["rubric"]["id"]
    _response, task = _review(client, rubric_id)
    assert task["status"] == "failed"
    assert task["error_code"] == "AI_PROVIDER_ERROR"
    assert "额度已用完" in task["error_message"]
    assert [item["status"] for item in task["items"]] == ["succeeded", "failed", "canceled"]
    assert client.get(f"/api/rubrics/{rubric_id}/rule-review").json() == {"reviewed": False}

    scorer.responses = [{"issues": []}, {"issues": []}]
    retried = client.post(f"/api/ai-tasks/{task['id']}/retry")
    assert retried.status_code == 200, retried.text
    _drain(client)
    done = client.get(f"/api/ai-tasks/{task['id']}").json()
    assert done["status"] == "succeeded"
    assert len(scorer.calls) == 4  # 第 1 项没有重跑
    assert client.get(f"/api/rubrics/{rubric_id}/rule-review").json()["reviewed"] is True


def test_review_without_real_model_and_old_endpoint_retired(client):
    rubric_id = _import(client, _clean_rules(), name="未审查")["rubric"]["id"]
    assert client.get(f"/api/rubrics/{rubric_id}/rule-review").json() == {"reviewed": False}
    response = client.post(f"/api/rubrics/{rubric_id}/ai-tasks", json={"kind": "rule_review", "params": {"scope": "all"}})
    assert response.status_code == 503
    assert "AI_CONNECTION_MISSING" in response.text
    retired = client.post(f"/api/rubrics/{rubric_id}/rule-review", json={"scope": "all"})
    assert retired.status_code == 410
    assert retired.json()["detail"]["code"] == "ENDPOINT_RETIRED"
    assert retired.json()["detail"]["context"]["kind"] == "rule_review"


def test_rules_changed_while_reviewing_marks_the_review_stale(client, use_scorer):
    use_scorer({"issues": []}, {"issues": []}, {"issues": []})
    rubric_id = _import(client, _clean_rules(), name="审查期间改规则")["rubric"]["id"]
    response = client.post(f"/api/rubrics/{rubric_id}/ai-tasks", json={"kind": "rule_review", "params": {"scope": "all"}})
    assert response.status_code == 202
    with client.session_factory() as session:
        criterion = session.scalars(select(models.RubricCriterion).where(
            models.RubricCriterion.rubric_id == rubric_id, models.RubricCriterion.code == "C01")).one()
        criterion.deduction_rules = [*criterion.deduction_rules, "新增一条：格式错误每处扣1分"]
        session.commit()
    _drain(client)
    view = client.get(f"/api/rubrics/{rubric_id}/rule-review").json()
    assert view["reviewed"] is True and view["stale"] is True


def _publish_events(client, rubric_id):
    with client.session_factory() as session:
        compilation = session.scalars(select(models.RubricCompilation).where(
            models.RubricCompilation.rubric_id == rubric_id, models.RubricCompilation.published_at.is_not(None))).one()
        return [e for e in compilation.human_changes if e.get("action") == "rule_review_at_publish"]


def test_publishing_without_review_is_recorded(client):
    rubric_id = _import(client, _clean_rules(), name="未审查发布")["rubric"]["id"]
    publish_rubric_via_api(client, rubric_id)
    events = _publish_events(client, rubric_id)
    assert [e["review_state"] for e in events] == ["not_reviewed"]


def test_publishing_after_fresh_review_is_recorded_as_reviewed(client, use_scorer):
    use_scorer({"issues": []}, {"issues": []}, {"issues": []})
    rubric_id = _import(client, _clean_rules(), name="已审查发布")["rubric"]["id"]
    _response, task = _review(client, rubric_id)
    assert task["status"] == "succeeded"
    publish_rubric_via_api(client, rubric_id)
    events = _publish_events(client, rubric_id)
    assert [(e["review_state"], e["open_findings"]) for e in events] == [("reviewed", 0)]
    # 发布时清理该评分标准的 AI 任务。
    assert client.get(f"/api/ai-tasks/{task['id']}").status_code == 404
