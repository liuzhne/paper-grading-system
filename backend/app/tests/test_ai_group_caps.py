"""AI group limits must survive application without becoming repeat caps."""
from copy import deepcopy
import pytest
from backend.app.services.rubric_import.deduction_caps import normalize_ai_group_caps
from backend.app.services.scoring.core.rule_executor import _deduction_effects
from backend.app.tests.test_rubric_review_workspace import workspace, confirmation


def rows():
    return [{"points": p, "reason": "合成条款", "match": f"合成条件{i}",
             "repeat_policy": "once", "cap_points": 6, "mutex_group": "T01-G1",
             "source": "ai_inferred", "confirmed": True, "severity": severity,
             "generation_fingerprint": "f" * 64,
             "generation_metadata": {"fingerprint": "f" * 64},
             "draft_row_key": f"T01::G1::{i}"}
            for i, (p, severity) in enumerate([(2, "minor"), (6, "severe")])]


@pytest.mark.parametrize("new_format", [False, True])
def test_ai_apply_confirm_and_recompile_respects_publish_contract(client, new_format):
    deductions = rows()
    if new_format:
        for row in deductions:
            row["group_cap_points"], row["cap_points"] = row["cap_points"], None
    response = client.post('/api/rubrics', json={"name": "合成组上限", "version": "v1", "total_score": 10,
        "criteria": [{"code": "T01", "name": "合成论证", "max_score": 10, "scoring_mode": "deductive",
                      "deduction_rules_structured": deductions}]})
    assert response.status_code == 200, response.text
    rubric = response.json()
    view = workspace(client, rubric['id'])
    assert not view['structural_blockers'], view['structural_blockers']
    assert len(view['rules']) == 2
    for rule in view['rules']:
        assert rule['cap_points'] is None
        assert rule['origin']['group_cap_points'] == 6
        assert rule['repeat_policy'] == 'once'
        assert rule['mutex_group'] == 'T01-G1'
        assert not rule['levels']
        result = client.post(f"/api/rubrics/{rubric['id']}/rules/{rule['rule_code']}/confirm", json=confirmation(view, rule))
        assert result.status_code == 200, result.text
        decision = {"occurrences": [{"occurrence_id": str(n)} for n in range(3)]}
        _, error = _deduction_effects(rule, decision, 10)
        assert error is None
        assert float(decision['calculated_effect']) == -float(rule['max_points'])
    assert all(r['status'] == 'approved' for r in workspace(client, rubric['id'])['rules'])
    result = client.post(f"/api/rubrics/{rubric['id']}/recompile", json={
        "criteria": rubric['criteria'], "supersedes_compilation_id": view['compilation_id'], "version": "v2"})
    assert result.status_code == 200, result.text
    assert not workspace(client, rubric['id'])['structural_blockers']
    assert client.get(f"/api/rubrics/{rubric['id']}").json()['status'] == 'draft'


@pytest.mark.parametrize('changes', [
    {'source': 'user_text'}, {'generation_metadata': {}}, {'repeat_policy': 'capped'},
    {'mutex_group': None}, {'points': 7}, {'levels': [{'code': 'bad'}]},
    {'draft_row_key': 'T02::G1::0'}, {'cap_points': -1},
])
def test_unproven_or_non_equivalent_legacy_caps_are_not_cleared(changes):
    values = rows()
    values[0].update(changes)
    before = deepcopy(values)
    result = normalize_ai_group_caps(values, criterion_code='T01', maximum=10)
    assert result[0]['cap_points'] == before[0]['cap_points']
    assert values == before


def test_group_cap_mismatch_and_explicit_invalid_groups_fail_closed():
    values = rows()
    values[1]['cap_points'] = 8
    assert normalize_ai_group_caps(values, criterion_code='T01', maximum=10) == values
    for row in values:
        row['group_cap_points'] = row.pop('cap_points')
    with pytest.raises(ValueError):
        normalize_ai_group_caps(values, criterion_code='T01', maximum=10)


def test_manual_once_cap_remains_a_located_chinese_blocker(client):
    response = client.post('/api/rubrics', json={"name": "合成非法人工上限", "version": "v1", "total_score": 10,
        "criteria": [{"code": "T01", "name": "合成论证", "max_score": 10, "scoring_mode": "deductive",
                      "deduction_rules_structured": [{"match": "合成条件", "points": 2, "repeat_policy": "once", "cap_points": 6}]}]})
    assert response.status_code == 200, response.text
    issues = workspace(client, response.json()['id'])['structural_blockers']
    issue = next(i for i in issues if i['code'] == 'deduct_rule_invalid')
    assert 'manual.t01.deduct.1.v1' in issue['message']
    assert '单条累计上限' in issue['message']


def test_existing_approved_conflicts_repaired_in_successor_without_rewriting_history(client):
    from backend.app.db import models
    response = client.post('/api/rubrics', json={"name": "合成存量上限修复", "version": "v1", "total_score": 10,
        "criteria": [{"code": "T01", "name": "合成论证", "max_score": 10, "scoring_mode": "deductive",
                      "deduction_rules_structured": rows()}]})
    rubric = response.json()
    old = workspace(client, rubric['id'])
    # Recreate the exact pre-fix persisted representation in isolated SQLite.
    with client.session_factory() as session:
        for item in old['rules']:
            rule = session.get(models.AtomicRule, item['id'])
            rule.cap_points = 6
            rule.criterion.deduction_rules_structured = rows()
        session.commit()
    old = workspace(client, rubric['id'])
    assert len(old['structural_blockers']) == 2
    for rule in old['rules']:
        result = client.post(f"/api/rubrics/{rubric['id']}/rules/{rule['rule_code']}/confirm", json=confirmation(old, rule))
        assert result.status_code == 200, result.text
    before = workspace(client, rubric['id'])
    full = client.get(f"/api/rubrics/{rubric['id']}").json()
    result = client.post(f"/api/rubrics/{rubric['id']}/recompile", json={
        "criteria": full['criteria'], "supersedes_compilation_id": before['compilation_id'], "version": "v2",
        "atomic_rules": [{"id": r['id'], "content_token": r['content_token'], "changes": {"cap_points": None}} for r in before['rules']]})
    assert result.status_code == 200, result.text
    after = workspace(client, rubric['id'])
    assert after['compilation_id'] != before['compilation_id']
    assert not after['structural_blockers']
    for previous, rule in zip(before['rules'], after['rules']):
        assert rule['cap_points'] is None
        assert rule['origin']['group_cap_points'] == 6
        assert rule['status'] == 'draft'
        for key in ('max_points', 'repeat_policy', 'mutex_group', 'rule_code', 'rule_text', 'levels', 'sources'):
            assert rule[key] == previous[key]
        with client.session_factory() as session:
            historical = session.get(models.AtomicRule, previous['id'])
            assert historical.cap_points == 6
            assert historical.status == 'approved'
        confirmed = client.post(f"/api/rubrics/{rubric['id']}/rules/{rule['rule_code']}/confirm", json=confirmation(after, rule))
        assert confirmed.status_code == 200, confirmed.text
    # New structured representation is idempotent; later compilation cannot reintroduce the cap.
    projected = client.get(f"/api/rubrics/{rubric['id']}").json()['criteria'][0]['deduction_rules_structured']
    assert normalize_ai_group_caps(projected, criterion_code='T01', maximum=10) == projected
