"""Manual source classification feeds drafts, without authorizing scores."""
from backend.app.services.ai_tasks import service as ai_task_service
from backend.app.tests.test_rubric_parse_coverage_api import _import, _rules_with_blocking_row, _resolve, BLOCKING_UNIT


class _Scorer:
    provider = 'fake'
    model_name = 'fake-draft-model'


def _draft_task(client, monkeypatch, rid, criterion):
    """起草改为 AI 任务：建任务时冻结的输入就是模型将看到的规则材料。"""
    monkeypatch.setattr(ai_task_service, 'get_llm_scorer', lambda *a, **k: _Scorer())
    response = client.post(f'/api/rubrics/{rid}/ai-tasks', json={
        'kind': 'rule_draft', 'params': {'criterion': criterion}, 'regenerate': True,
    })
    assert response.status_code == 202, response.text
    from backend.app.db import models
    with client.session_factory() as session:
        task = session.get(models.AITask, response.json()['id'])
        return task.input_snapshot['input_analysis'], response.json()


def test_assigned_source_enters_draft_and_restore_removes_it(client, monkeypatch):
    rubric = _import(client, _rules_with_blocking_row())['rubric']
    rid = rubric['id']
    before = client.get(f'/api/rubrics/{rid}/review-workspace').json()['rules']
    assert _resolve(client, rid, BLOCKING_UNIT, {'action':'assign', 'criterion_code':'C02', 'reason':'合成归类'}).status_code == 200
    analysis, task = _draft_task(client, monkeypatch, rid, rubric['criteria'][1])
    assert task['total_items'] >= 1
    assert any(u['source_refs'] == [BLOCKING_UNIT] and u['text'] == '错别字每处扣1分' for u in analysis['unresolved_segments'])
    assert client.get(f'/api/rubrics/{rid}').json()['criteria'] == rubric['criteria']
    assert client.get(f'/api/rubrics/{rid}/review-workspace').json()['rules'] == before
    restored = _resolve(client, rid, BLOCKING_UNIT, {'action':'restore', 'reason':'移出重新归类'})
    assert restored.status_code == 200, restored.text
    assert restored.json()['unit']['status'] == 'unclaimed'
    assert restored.json()['coverage']['blocking_count'] == 1
    _analysis, task = _draft_task(client, monkeypatch, rid, rubric['criteria'][1])
    # original explicit deduction requires no AI drafting
    assert (task['status'], task['total_items']) == ('succeeded', 0)
    assert task['result']['status'] == 'already_structured'


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
    analysis, _task = _draft_task(client, monkeypatch, rid, rubric['criteria'][1])
    assert any(u['source_refs'] == [unit['unit_id']] and u['text'] == unit['text'] for u in analysis['unresolved_segments'])
