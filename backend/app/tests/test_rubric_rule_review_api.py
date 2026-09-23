"""阶段 6.5 接口：规则审查运行、查看、豁免与“未经审查即发布”留痕。"""

from sqlalchemy import select

from backend.app.api.routes import rubrics as rubric_routes
from backend.app.db import models
from backend.app.tests.conftest import publish_rubric_via_api
from backend.app.tests.test_rubric_parse_coverage_api import _clean_rules
from backend.app.tests.test_rubric_parse_coverage_api import _import
from backend.app.tests.test_rubric_unit_classifier import FakeScorer


def _finding_for(payload):
    source = payload["sources"][0]
    return {"issues": [{"type": "undecidable", "rule_ids": ["R1"], "source_ids": [source["id"]],
                        "quote": source["text"][:4], "problem": "条件难以客观判断", "example": "", "severity": "low"}]}


def _use(monkeypatch, *responses):
    scorer = FakeScorer(*responses)
    monkeypatch.setattr(rubric_routes, "get_llm_scorer", lambda *a, **k: scorer)
    return scorer


def test_dry_run_returns_prechecks_and_scope_without_model_call(client, monkeypatch):
    scorer = _use(monkeypatch)
    rubric_id = _import(client, _clean_rules(), name="审查估算")["rubric"]["id"]
    response = client.post(f"/api/rubrics/{rubric_id}/rule-review", json={"dry_run": True, "scope": "all"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["criteria_codes"] == ["C01", "C02"]
    assert body["estimate"]["calls"] == 3  # 两个评分项 + 一次跨项审查
    assert body["prechecks"] == []
    assert scorer.calls == []


def test_review_is_persisted_and_findings_can_be_dismissed(client, monkeypatch):
    _use(monkeypatch, _finding_for, _finding_for, {"issues": []})
    rubric_id = _import(client, _clean_rules(), name="审查持久化")["rubric"]["id"]
    ran = client.post(f"/api/rubrics/{rubric_id}/rule-review", json={"scope": "all"})
    assert ran.status_code == 200, ran.text
    assert [f["criterion_code"] for f in ran.json()["findings"]] == ["C01", "C02"]
    view = client.get(f"/api/rubrics/{rubric_id}/rule-review").json()
    assert view["stale"] is False and len(view["findings"]) == 2
    dismissed = client.post(f"/api/rubrics/{rubric_id}/rule-review/findings/F1/dismiss", json={"reason": "已知且可接受"})
    assert dismissed.status_code == 200, dismissed.text
    statuses = {f["id"]: f["status"] for f in client.get(f"/api/rubrics/{rubric_id}/rule-review").json()["findings"]}
    assert statuses == {"F1": "dismissed", "F2": "open"}
    assert client.post(f"/api/rubrics/{rubric_id}/rule-review/findings/F9/dismiss",
                       json={"reason": "x"}).status_code == 404


def test_review_view_without_run_and_mock_refusal(client):
    rubric_id = _import(client, _clean_rules(), name="未审查")["rubric"]["id"]
    assert client.get(f"/api/rubrics/{rubric_id}/rule-review").json() == {"reviewed": False}
    response = client.post(f"/api/rubrics/{rubric_id}/rule-review", json={"scope": "all"})
    assert response.status_code == 503


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


def test_publishing_after_fresh_review_is_recorded_as_reviewed(client, monkeypatch):
    _use(monkeypatch, {"issues": []}, {"issues": []}, {"issues": []})
    rubric_id = _import(client, _clean_rules(), name="已审查发布")["rubric"]["id"]
    assert client.post(f"/api/rubrics/{rubric_id}/rule-review", json={"scope": "all"}).status_code == 200
    publish_rubric_via_api(client, rubric_id)
    events = _publish_events(client, rubric_id)
    assert [(e["review_state"], e["open_findings"]) for e in events] == [("reviewed", 0)]
