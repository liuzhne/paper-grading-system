"""评分细则排除落库：排除的细则不挡发布、不进版本哈希与评分快照、评分时不执行。

docs/评分细则排除落库改造方案.md。全部用内存 SQLite + Mock LLM，不调用真实模型。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy import update

from backend.app.core.config import settings
from backend.app.db import models
from backend.app.services.rubrics import lifecycle
from backend.app.services.scoring import engine as scoring_engine
from backend.app.services.scoring.adapters.rubric_snapshot import CompiledRubricSnapshotLoader
from backend.app.tests.conftest import make_sample_docx


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _create(client, name="合成排除细则"):
    response = client.post("/api/rubrics", json={
        "name": name, "version": "v1", "total_score": 20,
        "criteria": [
            {"code": "T01", "name": "论证", "max_score": 10, "scoring_mode": "deductive",
             "deduction_rules_structured": [
                 {"match": "缺少论证依据", "points": 2, "reason": "合成条款一"},
                 {"match": "结论与数据不符", "points": 3, "reason": "合成条款二"},
             ]},
            {"code": "T02", "name": "规范", "max_score": 10, "scoring_mode": "deductive",
             "deduction_rules_structured": [
                 {"match": "引用格式不统一", "points": 1, "reason": "合成条款三"},
             ]},
        ],
    })
    assert response.status_code == 200, response.text
    return response.json()


def _workspace(client, rubric_id):
    response = client.get(f"/api/rubrics/{rubric_id}/review-workspace")
    assert response.status_code == 200, response.text
    return response.json()


def _body(view, rule, reason="核对合成条款"):
    return {"compilation_id": view["compilation_id"], "rule_id": rule["id"],
            "content_token": rule["content_token"], "reason": reason}


def _rules(view, criterion_code):
    # 工作区只给 criterion_id；手动录入的规则编号以评分项编号开头（manual.t01.deduct.1.v1）。
    prefix = f"manual.{criterion_code.lower()}."
    return [rule for rule in view["rules"] if rule["rule_code"].startswith(prefix)]


def _act(client, rubric_id, view, rule, action):
    return client.post(f"/api/rubrics/{rubric_id}/rules/{rule['rule_code']}/{action}",
                       json=_body(view, rule))


def _confirm_rest(client, rubric_id):
    view = _workspace(client, rubric_id)
    for rule in view["rules"]:
        if rule["status"] in {"draft", "review"}:
            assert _act(client, rubric_id, view, rule, "confirm").status_code == 200
    return _workspace(client, rubric_id)


def _publish(client, rubric_id, view):
    assert client.post(f"/api/rubrics/{rubric_id}/submit-review").status_code == 200
    return client.post(f"/api/rubrics/{rubric_id}/publish", json={
        "compilation_id": view["compilation_id"], "reason": "合成发布"})


def _excluded_published(client, name="合成排除细则"):
    rubric = _create(client, name)
    view = _workspace(client, rubric["id"])
    excluded = _rules(view, "T01")[1]
    response = _act(client, rubric["id"], view, excluded, "exclude")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    view = _confirm_rest(client, rubric["id"])
    published = _publish(client, rubric["id"], view)
    assert published.status_code == 200, published.text
    return rubric, view, excluded


def test_exclusion_persists_and_does_not_count_as_pending(client):
    rubric = _create(client)
    view = _workspace(client, rubric["id"])
    target = _rules(view, "T01")[0]
    assert _act(client, rubric["id"], view, target, "exclude").status_code == 200
    # 重试（响应丢失后再点一次）不重复写审计。
    assert _act(client, rubric["id"], view, target, "exclude").status_code == 200

    reloaded = _workspace(client, rubric["id"])
    status = {rule["rule_code"]: rule["status"] for rule in reloaded["rules"]}
    assert status[target["rule_code"]] == "rejected"
    with client.session_factory() as session:
        events = session.get(models.RubricCompilation, view["compilation_id"]).human_changes
    actions = [event["action"] for event in events if event["rule_code"] == target["rule_code"]]
    assert actions == ["submit_review", "reject"]

    coverage = client.get(f"/api/rubrics/{rubric['id']}/rule-coverage").json()
    t01 = next(item for item in coverage["criteria"] if item["code"] == "T01")
    assert (t01["rule_count"], t01["rejected_count"], t01["user_pending_count"]) == (1, 1, 1)
    _confirm_rest(client, rubric["id"])
    coverage = client.get(f"/api/rubrics/{rubric['id']}/rule-coverage").json()
    assert coverage["pending_review_count"] == 0 and coverage["blocking_count"] == 0

    # 恢复回到草稿，需要重新确认；恢复已是草稿的细则是空操作。
    restored = _act(client, rubric["id"], reloaded, target, "restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "draft"
    assert _act(client, rubric["id"], reloaded, target, "restore").json()["status"] == "draft"


def test_exclude_rejects_stale_content_confirmed_rules_and_the_last_rule(client):
    rubric = _create(client)
    view = _workspace(client, rubric["id"])
    first, second = _rules(view, "T01")
    stale = {**_body(view, first), "content_token": "0" * 64}
    assert client.post(f"/api/rubrics/{rubric['id']}/rules/{first['rule_code']}/exclude",
                       json=stale).status_code == 409

    only = _rules(view, "T02")[0]
    last = _act(client, rubric["id"], view, only, "exclude")
    assert last.status_code == 409 and "至少保留 1 条" in last.json()["detail"]

    assert _act(client, rubric["id"], view, first, "exclude").status_code == 200
    blocked = _act(client, rubric["id"], view, second, "exclude")
    assert blocked.status_code == 409 and "至少保留 1 条" in blocked.json()["detail"]
    assert _workspace(client, rubric["id"])["rules"][1]["status"] == "draft"  # 整个事务回滚

    assert _act(client, rubric["id"], view, second, "confirm").status_code == 200
    confirmed = _act(client, rubric["id"], view, second, "exclude")
    assert confirmed.status_code == 409 and "已确认" in confirmed.json()["detail"]


def test_excluded_rule_publishes_and_never_reaches_snapshot_or_scoring(client, monkeypatch):
    rubric, view, excluded = _excluded_published(client)
    with client.session_factory() as session:
        version = session.scalar(select(models.RubricVersion).where(
            models.RubricVersion.rubric_id == rubric["id"]))
        snapshot = CompiledRubricSnapshotLoader().load_from_session(
            session=session, rubric_version_id=version.id,
            expected_profile_key=version.business_profile_key,
        ).to_mapping()
    codes = {rule["rule_code"] for rule in snapshot["atomic_rules"]}
    assert excluded["rule_code"] not in codes
    assert len(codes) == len(view["rules"]) - 1

    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")
    batch = client.post("/api/batches", json={"name": "排除评分", "rubric_id": rubric["id"]})
    assert batch.status_code == 200, batch.text
    upload = client.post("/api/papers/upload", data={"batch_id": batch.json()["id"]},
                         files={"file": ("p.docx", make_sample_docx().getvalue(), DOCX_MIME)})
    assert upload.status_code == 200, upload.text
    with client.session_factory() as session:
        run = scoring_engine.score_paper(session, upload.json()["id"])
        tasks = session.scalars(select(models.RuleScoringTask.rule_code).where(
            models.RuleScoringTask.scoring_run_id == run.id)).all()
    assert set(tasks) == codes


def test_flipping_an_excluded_rule_after_publication_fails_closed(client):
    rubric, _view, excluded = _excluded_published(client, "合成排除篡改")
    with client.session_factory() as session:
        version = session.scalar(select(models.RubricVersion).where(
            models.RubricVersion.rubric_id == rubric["id"]))
        session.execute(update(models.AtomicRule).where(
            models.AtomicRule.id == excluded["id"]).values(status="approved"))
        with pytest.raises(ValueError, match="hash does not match"):
            CompiledRubricSnapshotLoader().load_from_session(
                session=session, rubric_version_id=version.id,
                expected_profile_key=version.business_profile_key,
            )
        session.rollback()


def test_version_hash_ignores_nothing_without_exclusions(client):
    """没有排除的细则时，版本内容哈希与过滤前逐字节一致（已发布版本不受影响）。"""

    rubric = _create(client, "合成无排除")
    view = _confirm_rest(client, rubric["id"])
    assert _publish(client, rubric["id"], view).status_code == 200
    with client.session_factory() as session:
        version = session.scalar(select(models.RubricVersion).where(
            models.RubricVersion.rubric_id == rubric["id"]))
        compilation = session.get(models.RubricCompilation, version.compilation_id)
        source = session.get(models.Rubric, rubric["id"])
        assert lifecycle._canonical_version_hash(session, source, compilation, version) == version.version_hash
        original = lifecycle.participates
        try:
            lifecycle.participates = lambda _rule: True
            unfiltered = lifecycle._canonical_version_hash(session, source, compilation, version)
        finally:
            lifecycle.participates = original
        assert unfiltered == version.version_hash


def _patch(client, rubric_id, rule_code, changes):
    response = client.patch(f"/api/rubrics/{rubric_id}/rules/{rule_code}",
                            json={"changes": changes, "reason": "合成编辑"})
    assert response.status_code == 200, response.text


def test_gate_blocks_dependency_on_excluded_rule_and_allows_lone_mutex_member(client):
    rubric = _create(client, "合成排除依赖")
    view = _workspace(client, rubric["id"])
    first, second = _rules(view, "T01")
    third = _rules(view, "T02")[0]
    _patch(client, rubric["id"], third["rule_code"], {"depends_on_rule_codes": [first["rule_code"]]})
    _patch(client, rubric["id"], first["rule_code"], {"mutex_group": "T01-tier"})
    _patch(client, rubric["id"], second["rule_code"], {"mutex_group": "T01-tier"})
    view = _workspace(client, rubric["id"])
    first = next(rule for rule in view["rules"] if rule["rule_code"] == first["rule_code"])
    assert _act(client, rubric["id"], view, first, "exclude").status_code == 200

    blockers = {item["code"]: item for item in _workspace(client, rubric["id"])["structural_blockers"]}
    assert "dependency_rejected" in blockers
    assert blockers["dependency_rejected"]["identity"]["rule_code"] == third["rule_code"]
    assert "mutex_definition_invalid" not in blockers  # 同组只剩 1 条是排除造成的

    _patch(client, rubric["id"], third["rule_code"], {"depends_on_rule_codes": []})
    view = _confirm_rest(client, rubric["id"])
    assert not view["structural_blockers"]
    assert _publish(client, rubric["id"], view).status_code == 200


def test_gate_requires_an_intact_rejection_audit(client):
    rubric = _create(client, "合成排除审计")
    view = _workspace(client, rubric["id"])
    target = _rules(view, "T01")[0]
    assert _act(client, rubric["id"], view, target, "exclude").status_code == 200
    view = _confirm_rest(client, rubric["id"])
    with client.session_factory() as session:
        compilation = session.get(models.RubricCompilation, view["compilation_id"])
        session.execute(update(models.RubricCompilation).where(
            models.RubricCompilation.id == compilation.id
        ).values(human_changes=[event for event in compilation.human_changes
                                if not (event["rule_code"] == target["rule_code"] and event["action"] == "reject")]))
        session.commit()
    blocked = _publish(client, rubric["id"], view)
    assert blocked.status_code == 400 and "rule_rejection_audit_missing" in blocked.json()["detail"]


def test_recompile_and_clone_keep_exclusions(client):
    rubric, view, excluded = _excluded_published(client, "合成排除克隆")
    clone = client.post(f"/api/rubrics/{rubric['id']}/clone", json={"new_version": "v2"})
    assert clone.status_code == 200, clone.text
    cloned = _workspace(client, clone.json()["id"])
    status = {rule["rule_code"]: rule["status"] for rule in cloned["rules"]}
    assert status.pop(excluded["rule_code"]) == "rejected"
    assert set(status.values()) == {"draft"}

    # 保存重新校验：内容没变的排除沿用到新编译。
    detail = client.get(f"/api/rubrics/{clone.json()['id']}").json()
    saved = client.post(f"/api/rubrics/{clone.json()['id']}/recompile", json={
        "supersedes_compilation_id": cloned["compilation_id"], "version": "v2",
        "criteria": detail["criteria"], "reason": "合成重新校验",
    })
    assert saved.status_code == 200, saved.text
    after = _workspace(client, clone.json()["id"])
    assert after["compilation_id"] != cloned["compilation_id"]
    assert {rule["rule_code"]: rule["status"] for rule in after["rules"]}[excluded["rule_code"]] == "rejected"
