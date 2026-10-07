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


def test_partial_retry_keeps_other_valid_suggestions(client, monkeypatch):
    scorer = FakeScorer(_label_all('rule', 'C02'), _label_all('requirement', 'C01'))
    monkeypatch.setattr(rubric_routes, 'get_llm_scorer', lambda *a, **k: scorer)
    rid = _import(client, _rules_with_blocking_row())['rubric']['id']
    assert _run(client, rid).status_code == 200
    response = _run(client, rid, unit_ids=['xlsx:评分规则!R4C4'])
    assert response.status_code == 200
    results = {item['unit_id']: item for item in response.json()['results']}
    assert results['xlsx:评分规则!R4C2']['suggested_criterion'] == 'C02'
    assert results['xlsx:评分规则!R4C4']['suggested_criterion'] == 'C01'
    assert client.get(f'/api/rubrics/{rid}/parse-coverage').json()['unit_classifications']['stale'] is False


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


def test_classification_rejects_source_changes_during_provider_call(client, monkeypatch):
    from backend.app.services.rubric_import.parse_state import current_compilation
    rid = _import(client, _rules_with_blocking_row())["rubric"]["id"]

    class ChangingScorer:
        provider = "fake"
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

    monkeypatch.setattr(rubric_routes, "get_llm_scorer", lambda *a, **k: ChangingScorer())
    response = _run(client, rid, unit_ids=["xlsx:评分规则!R4C4"])
    assert response.status_code == 409, response.text
    assert "CLASSIFICATION_INPUT_CHANGED" in response.text
