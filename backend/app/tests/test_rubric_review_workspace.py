from backend.app.tests.test_m8_web_rubric_lifecycle import _import_rubric
from backend.app.tests.test_rubric_visibility import _login
from backend.app.db import models
from sqlalchemy import select
import pytest


def workspace(client, rubric_id):
    response = client.get(f"/api/rubrics/{rubric_id}/review-workspace")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    return response.json()


def confirmation(view, rule):
    return {"compilation_id": view["compilation_id"], "rule_id": rule["id"],
            "content_token": rule["content_token"], "reason": "核对合成模板条款"}


def test_atomic_edit_preserves_source_and_rejects_stale_or_incomplete_input(client):
    imported = _import_rubric(client, name="合成原子编辑")
    rubric = imported["rubric"]
    view = workspace(client, rubric["id"])
    first = view["rules"][0]
    payload = {"supersedes_compilation_id": view["compilation_id"], "version": "v2",
               "criteria": rubric["criteria"], "atomic_rules": [{"id": first["id"],
               "content_token": first["content_token"], "changes": {"rule_text": "修改后的合成条款"}}]}
    path = f"/api/rubrics/{rubric['id']}/recompile"
    assert client.post(path, json={**payload, "atomic_rules": []}).status_code == 422
    stale = {**payload, "atomic_rules": [{**payload["atomic_rules"][0], "content_token": "0" * 64}]}
    assert client.post(path, json=stale).status_code == 422
    assert workspace(client, rubric["id"])["compilation_id"] == view["compilation_id"]
    response = client.post(path, json=payload)
    assert response.status_code == 200, response.text
    updated = workspace(client, rubric["id"])
    assert updated["rules"][0]["rule_text"] == "修改后的合成条款"
    assert updated["rules"][0]["sources"] == first["sources"]
    assert updated["rules"][0]["levels"] == first["levels"]
    assert updated["rules"][0]["rule_code"] == first["rule_code"]
    assert updated["rules"][0]["status"] == "draft"
    assert len(updated["template_links"]) == len(view["template_links"])


def test_ai_append_keeps_imported_atomic_rule_and_source(client):
    rubric = _import_rubric(client, name="合成追加保留")["rubric"]
    before = workspace(client, rubric["id"])
    criteria = rubric["criteria"]
    criteria[0]["scoring_mode"] = "deductive"
    criteria[0]["deduction_rules_structured"] = [{"match": "缺少论证", "trigger": "缺少论证", "points": 2,
        "reason": "合成缺失论证", "source": "ai_inferred", "generation_fingerprint": "f" * 64,
        "draft_row_key": "METHOD::G1::0", "confirmed": True}]
    response = client.post(f"/api/rubrics/{rubric['id']}/recompile", json={
        "supersedes_compilation_id": before["compilation_id"], "version": "v2", "criteria": criteria})
    assert response.status_code == 200, response.text
    after = workspace(client, rubric["id"])
    assert len(after["rules"]) == 2
    original = next(rule for rule in after["rules"] if rule["rule_code"] == before["rules"][0]["rule_code"])
    assert original["rule_text"] == before["rules"][0]["rule_text"]
    assert original["sources"] == before["rules"][0]["sources"]
    # Keeping both directions requires explicit resolution; never bypass the scoring policy.
    assert "band_criterion_invalid" in {item["code"] for item in after["structural_blockers"]}


def test_manual_total_edit_keeps_policy_consistent(client):
    created = client.post('/api/rubrics', json={"name":"合成总分编辑", "version":"v1", "total_score":10,
        "criteria":[{"code":"T01", "name":"论证", "max_score":10, "scoring_mode":"deductive",
                     "deduction_rules_structured":[{"match":"缺少论证", "points":2, "reason":"合成条款"}]}]})
    assert created.status_code == 200, created.text
    rubric = created.json()
    before = workspace(client, rubric['id'])
    rubric['criteria'][0]['max_score'] = 20
    saved = client.post(f"/api/rubrics/{rubric['id']}/recompile", json={
        "supersedes_compilation_id": before['compilation_id'], "version":"v2", "total_score":20,
        "criteria":rubric['criteria']})
    assert saved.status_code == 200, saved.text
    assert 'global_policy_unsupported' not in {item['code'] for item in workspace(client, rubric['id'])['structural_blockers']}


def test_import_view_confirm_and_reload_is_audited_without_publication(client):
    rubric_id = _import_rubric(client, name="条款确认合成模板")["rubric"]["id"]
    view = workspace(client, rubric_id)
    rule = view["rules"][0]
    assert rule["rule_text"]
    assert rule["sources"]
    assert rule["levels"]
    url = f"/api/rubrics/{rubric_id}/rules/{rule['rule_code']}/confirm"
    response = client.post(url, json=confirmation(view, rule))
    assert response.status_code == 200, response.text
    confirmed = workspace(client, rubric_id)["rules"][0]
    assert confirmed["status"] == "approved"
    assert confirmed["reviewed_by"] and confirmed["reviewed_at"]
    assert confirmed["rule_text"] == rule["rule_text"]
    assert client.get(f"/api/rubrics/{rubric_id}").json()["status"] == "draft"
    # Retrying a request with the original content must not create new audit state.
    assert client.post(url, json=confirmation(view, rule)).status_code == 200
    assert workspace(client, rubric_id)["rules"][0]["reviewed_at"] == confirmed["reviewed_at"]
    assert client.get(f"/api/rubrics/{rubric_id}/rule-coverage").json()["complete_count"] == 1


def test_changed_rule_content_and_wrong_version_cannot_be_confirmed(client):
    rubric_id = _import_rubric(client, name="冲突确认合成模板")["rubric"]["id"]
    view = workspace(client, rubric_id)
    rule = view["rules"][0]
    url = f"/api/rubrics/{rubric_id}/rules/{rule['rule_code']}"
    edited = client.patch(url, json={"changes": {"rule_text": "修改后的合成条款"}, "reason": "修订"})
    assert edited.status_code == 200, edited.text
    assert client.post(url + "/confirm", json=confirmation(view, rule)).status_code == 409
    current = workspace(client, rubric_id)
    payload = confirmation(current, current["rules"][0])
    payload["compilation_id"] = "other-version"
    assert client.post(url + "/confirm", json=payload).status_code == 409
    assert workspace(client, rubric_id)["rules"][0]["status"] == "draft"


def test_recovery_projection_remains_body_free(client):
    rubric_id = _import_rubric(client, name="安全投影合成模板")["rubric"]["id"]
    recovery = client.get(f"/api/rubrics/{rubric_id}/execution-draft").json()
    assert "rule_text" not in recovery["active_compilation"]["rules"][0]
    assert "sources" not in recovery["active_compilation"]["rules"][0]


def test_review_details_and_confirmation_enforce_visibility_and_role(client, monkeypatch):
    owner, org_id = _login(client, monkeypatch, "review-owner")
    rubric_id = _import_rubric(client, name="私有审核合成模板")["rubric"]["id"]
    view = workspace(client, rubric_id)
    rule = view["rules"][0]
    url = f"/api/rubrics/{rubric_id}/rules/{rule['rule_code']}/confirm"
    client.post("/api/auth/logout")
    _login(client, monkeypatch, "review-other")
    assert client.get(f"/api/rubrics/{rubric_id}/review-workspace").status_code == 404
    assert client.post(url, json=confirmation(view, rule)).status_code == 404
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"username": "review-owner", "password": "review-owner correct password"}).status_code == 204
    assert client.post("/api/auth/organization-context", json={"organization_id": org_id}).status_code == 200
    with client.session_factory() as session:
        member = session.scalar(select(models.OrganizationMember).where(
            models.OrganizationMember.user_id == owner, models.OrganizationMember.organization_id == org_id))
        member.role = "member"
        session.commit()
    assert client.get(f"/api/rubrics/{rubric_id}/review-workspace").status_code == 200
    assert client.post(url, json=confirmation(view, rule)).status_code == 403


@pytest.mark.parametrize("change", ["append", "content", "context"])
def test_only_unchanged_confirmed_generated_rows_survive_recompilation(client, change):
    first = {"trigger": "没有说明方法", "match": "没有说明方法", "reason": "方法缺失", "points": 2,
             "source": "ai_inferred", "confirmed": True, "generation_fingerprint": "f" * 64,
             "draft_row_key": "C1::G1::0"}
    created = client.post("/api/rubrics", json={"name": "逐次确认合成模板", "version": "v1",
        "total_score": 10, "criteria": [{"code": "C1", "name": "方法", "max_score": 10,
        "description": "核对方法", "scoring_mode": "deductive", "deduction_rules_structured": [first]}]})
    assert created.status_code == 200, created.text
    rubric_id = created.json()["id"]
    view = workspace(client, rubric_id)
    rule = view["rules"][0]
    response = client.post(f"/api/rubrics/{rubric_id}/rules/{rule['rule_code']}/confirm", json=confirmation(view, rule))
    assert response.status_code == 200, response.text
    full = client.get(f"/api/rubrics/{rubric_id}").json()
    criteria = full["criteria"]
    criteria[0]["deduction_rules_structured"].append({**first, "reason": "缺少论证", "trigger": "缺少论证",
        "match": "缺少论证", "points": 3, "draft_row_key": "C1::G1::1"})
    if change == "content":
        criteria[0]["deduction_rules_structured"][0]["reason"] = "改过的规则"
    if change == "context":
        criteria[0]["description"] = "改过的评分上下文"
    response = client.post(f"/api/rubrics/{rubric_id}/recompile", json={
        "supersedes_compilation_id": view["compilation_id"], "version": "v2", "criteria": criteria,
        "reason": "用户确认第二条合成建议",
    })
    assert response.status_code == 200, response.text
    current = workspace(client, rubric_id)
    assert current["rules"][0]["status"] == ("approved" if change == "append" else "draft")
    assert current["rules"][1]["status"] == "draft"
    coverage = client.get(f"/api/rubrics/{rubric_id}/rule-coverage").json()
    assert coverage["criteria"][0]["from_ai_count"] == 2
    assert coverage["criteria"][0]["from_source_count"] == 0
    with client.session_factory() as session:
        compilation = session.get(models.RubricCompilation, current["compilation_id"])
        approvals = [event for event in compilation.human_changes if event["action"] == "approve"]
        assert len(approvals) == (1 if change == "append" else 0)
        if approvals:
            assert rule["id"] in approvals[0]["reason"]
