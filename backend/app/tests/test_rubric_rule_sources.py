"""Manual source classification feeds drafts, without authorizing scores."""
from backend.app.api.routes import rubrics as routes
from backend.app.tests.test_rubric_parse_coverage_api import _import, _rules_with_blocking_row, _resolve, BLOCKING_UNIT


def test_assigned_source_enters_draft_and_restore_removes_it(client, monkeypatch):
    rubric = _import(client, _rules_with_blocking_row())['rubric']
    rid = rubric['id']
    before = client.get(f'/api/rubrics/{rid}/review-workspace').json()['rules']
    assert _resolve(client, rid, BLOCKING_UNIT, {'action':'assign', 'criterion_code':'C02', 'reason':'合成归类'}).status_code == 200
    captured = []
    monkeypatch.setattr(routes, '_rubric_ai_scorer', lambda *a: object())
    def draft(**kwargs):
        captured.append(kwargs)
        return {'requires_confirmation': True, 'rule_groups': []}
    monkeypatch.setattr(routes, 'draft_deduction_rules', draft)
    response = client.post(f'/api/rubrics/{rid}/draft-deduction-rules', json={'criteria':[rubric['criteria'][1]]})
    assert response.status_code == 200, response.text
    analysis = captured[0]['input_analysis']
    assert any(u['source_refs'] == [BLOCKING_UNIT] and u['text'] == '错别字每处扣1分' for u in analysis['unresolved_segments'])
    assert client.get(f'/api/rubrics/{rid}').json()['criteria'] == rubric['criteria']
    assert client.get(f'/api/rubrics/{rid}/review-workspace').json()['rules'] == before
    restored = _resolve(client, rid, BLOCKING_UNIT, {'action':'restore', 'reason':'移出重新归类'})
    assert restored.status_code == 200, restored.text
    assert restored.json()['unit']['status'] == 'unclaimed'
    assert restored.json()['coverage']['blocking_count'] == 1
    captured.clear()
    response = client.post(f'/api/rubrics/{rid}/draft-deduction-rules', json={'criteria':[rubric['criteria'][1]]})
    assert response.status_code == 200
    assert not captured  # original explicit deduction requires no AI drafting


def test_word_source_locator_reaches_ai_draft(client, monkeypatch):
    from io import BytesIO
    from docx import Document
    from backend.app.tests.test_rubric_parse_coverage_api import XLSX, _clean_rules

    document = Document()
    document.add_paragraph('错别字每处扣1分。')
    buffer = BytesIO()
    document.save(buffer)
    imported = client.post('/api/rubrics/import-files', data={'name':'合成 Word 规则来源', 'version':'v1'}, files={
        'rules_file': ('rules.xlsx', _clean_rules(), XLSX),
        'template_file': ('source.docx', buffer.getvalue(), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    })
    assert imported.status_code == 200, imported.text
    rubric = imported.json()['rubric']
    rid = rubric['id']
    unit = next(u for u in client.get(f'/api/rubrics/{rid}/parse-coverage').json()['coverage']['unclaimed'] if u['unit_id'].startswith('docx:'))
    assert _resolve(client, rid, unit['unit_id'], {'action':'assign', 'criterion_code':'C02', 'reason':'合成归类'}).status_code == 200
    captures = []
    monkeypatch.setattr(routes, '_rubric_ai_scorer', lambda *a: object())
    monkeypatch.setattr(routes, 'draft_deduction_rules', lambda **kwargs: captures.append(kwargs) or {'requires_confirmation':True, 'rule_groups':[]})
    response = client.post(f'/api/rubrics/{rid}/draft-deduction-rules', json={'criteria':[rubric['criteria'][1]]})
    assert response.status_code == 200, response.text
    assert any(u['source_refs'] == [unit['unit_id']] and u['text'] == unit['text'] for u in captures[0]['input_analysis']['unresolved_segments'])
