"""阶段 5 接口：用户确认后运行兜底分类器；结果持久化并带指纹，评分项变化后标记过期。"""

from backend.app.api.routes import rubrics as rubric_routes
from backend.app.tests.test_rubric_parse_coverage_api import _import
from backend.app.tests.test_rubric_parse_coverage_api import _rules_with_blocking_row
from backend.app.tests.test_rubric_unit_classifier import FakeScorer
from backend.app.tests.test_rubric_unit_classifier import _label_all


def _run(client, rubric_id, **body):
    return client.post(f"/api/rubrics/{rubric_id}/unit-classifications", json=body)


def test_classification_is_persisted_and_visible_after_reload(client, monkeypatch):
    scorer = FakeScorer(_label_all("rule", "C02", "high"))
    monkeypatch.setattr(rubric_routes, "get_llm_scorer", lambda *a, **k: scorer)
    rubric_id = _import(client, _rules_with_blocking_row())["rubric"]["id"]
    response = _run(client, rubric_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [r["unit_id"] for r in body["results"]] == ["xlsx:评分规则!R4C4", "xlsx:评分规则!R4C2"]
    assert body["stale"] is False
    coverage = client.get(f"/api/rubrics/{rubric_id}/parse-coverage").json()
    assert coverage["unit_classifications"]["results"] == body["results"]
    assert coverage["unit_classifications"]["stale"] is False
    # 分类只是建议：单元仍未认领、门禁仍在
    assert coverage["unclaimed_summary"]["blocking"] == 1


def test_classification_scope_can_be_narrowed(client, monkeypatch):
    scorer = FakeScorer(_label_all())
    monkeypatch.setattr(rubric_routes, "get_llm_scorer", lambda *a, **k: scorer)
    rubric_id = _import(client, _rules_with_blocking_row())["rubric"]["id"]
    response = _run(client, rubric_id, unit_ids=["xlsx:评分规则!R4C4"])
    assert response.status_code == 200, response.text
    assert [u["unit_id"] for u in scorer.calls[0][1]["units"]] == ["xlsx:评分规则!R4C4"]


def test_classification_becomes_stale_when_criteria_change(client, monkeypatch):
    monkeypatch.setattr(rubric_routes, "get_llm_scorer", lambda *a, **k: FakeScorer(_label_all()))
    rubric = _import(client, _rules_with_blocking_row())["rubric"]
    assert _run(client, rubric["id"]).status_code == 200
    workspace = client.get(f"/api/rubrics/{rubric['id']}/review-workspace").json()
    criteria = rubric["criteria"]
    criteria[0]["name"] = "研究设计"
    recompiled = client.post(f"/api/rubrics/{rubric['id']}/recompile", json={
        "supersedes_compilation_id": workspace["compilation_id"], "version": "v2", "criteria": criteria,
        "atomic_rules": [{"id": r["id"], "content_token": r["content_token"], "changes": {"rule_text": r["rule_text"]}}
                         for r in workspace["rules"]]})
    assert recompiled.status_code == 200, recompiled.text
    coverage = client.get(f"/api/rubrics/{rubric['id']}/parse-coverage").json()
    assert coverage["unit_classifications"]["stale"] is True


def test_mock_connection_is_refused(client):
    rubric_id = _import(client, _rules_with_blocking_row())["rubric"]["id"]
    response = _run(client, rubric_id)
    assert response.status_code == 503
    assert "AI_CONNECTION_MISSING" in response.text


def test_rubric_without_ledger_or_nothing_to_classify(client, monkeypatch):
    monkeypatch.setattr(rubric_routes, "get_llm_scorer", lambda *a, **k: FakeScorer(_label_all()))
    created = client.post("/api/rubrics", json={
        "name": "手工", "version": "v1", "total_score": 10,
        "criteria": [{"code": "T01", "name": "论证", "max_score": 10, "scoring_mode": "deductive",
                      "deduction_rules_structured": [{"match": "缺少论证", "points": 2, "reason": "合成"}]}]})
    assert _run(client, created.json()["id"]).status_code == 404
    rubric_id = _import(client, _rules_with_blocking_row(), name="空范围")["rubric"]["id"]
    assert _run(client, rubric_id, unit_ids=["nothing"]).status_code == 422
