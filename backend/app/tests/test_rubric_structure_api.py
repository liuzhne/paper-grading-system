"""阶段 6 ④接口：导入预检、带结构导入、草稿结构建议、合入与撤销。

C 阶段起结构识别是 AI 任务（kind=structure_suggestion，一次模型调用一个条目）：
草稿目标在条目成功时落库；导入前目标没有评分标准，结果只在任务里。
"""

import json
from io import BytesIO

import pytest
from openpyxl import Workbook

from backend.app.db import models
from backend.app.services.ai_tasks import service as ai_task_service
from backend.app.tests import rubric_parse_fixtures as fx
from backend.app.tests.test_rubric_unit_classifier import FakeScorer

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
E1_STRUCTURE = {
    "sheet": "Sheet", "header_row": "R1",
    "column_mapping": [{"col": "C1", "field": "name", "reason": "名称"},
                       {"col": "C2", "field": "max_score", "reason": "数字"},
                       {"col": "C3", "field": "description", "reason": "说明"}],
    "row_types": [], "unresolved": [],
}


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


def _preview(client, data=None):
    response = client.post("/api/ai-tasks/import-structure",
                           files={"rules_file": ("r.xlsx", data or fx.unmapped_header_xlsx(), XLSX)})
    if response.status_code >= 400:
        return response, None
    _drain(client)
    return response, client.get(f"/api/ai-tasks/{response.json()['id']}").json()


def _suggest(client, rubric_id):
    """提交草稿结构建议并执行完；返回 (提交响应, 任务, 草稿里的建议)。"""
    response = client.post(f"/api/rubrics/{rubric_id}/ai-tasks", json={"kind": "structure_suggestion", "params": {}})
    if response.status_code >= 400:
        return response, None, None
    _drain(client)
    task = client.get(f"/api/ai-tasks/{response.json()['id']}").json()
    view = client.get(f"/api/rubrics/{rubric_id}/parse-coverage").json().get("structure_suggestions")
    return response, task, view


def test_preview_estimate_returns_failure_codes_without_calling_model(client, use_scorer):
    scorer = use_scorer()
    response = client.post("/api/rubrics/import-files/structure-suggestions/estimate",
                           files={"rules_file": ("r.xlsx", fx.unmapped_header_xlsx(), XLSX)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["failure_codes"] == ["E1"]
    assert body["estimate"]["calls"] == 1 and body["estimate"]["chars"] > 0
    assert scorer.calls == []


def test_preview_task_then_import_with_confirmed_structure(client, use_scorer):
    use_scorer(E1_STRUCTURE)
    response, task = _preview(client)
    assert response.status_code == 202, response.text
    assert task["rubric_id"] is None
    assert task["status"] == "succeeded", task
    body = task["result"]
    assert body["target"] == "import"
    assert body["failure_codes"] == ["E1"]
    assert body["override"]["column_mapping"] == {"name": 1, "max_score": 2, "description": 3}
    assert body["preview"] == [{"name": "选题", "max_score": 10.0, "row_number": 2}]
    imported = client.post(
        "/api/rubrics/import-files",
        data={"name": "按结构导入", "version": "v1", "structure_override": json.dumps(body["override"])},
        files={"rules_file": ("r.xlsx", fx.unmapped_header_xlsx(), XLSX)},
    )
    assert imported.status_code == 200, imported.text
    assert [c["name"] for c in imported.json()["rubric"]["criteria"]] == ["选题"]


def test_preview_task_repairs_invalid_structure_once(client, use_scorer):
    bad = {**E1_STRUCTURE, "header_row": "R99"}
    scorer = use_scorer({**bad, "row_types": [{"row": "R99", "type": "criterion", "reason": "x"}]}, E1_STRUCTURE)
    _response, task = _preview(client)
    assert task["status"] == "succeeded", task
    assert len(scorer.calls) == 2
    assert "ROW_OUT_OF_RANGE" in scorer.calls[1][0]


def test_preview_task_is_reused_for_the_same_file_and_pruned_when_a_new_one_starts(client, use_scorer):
    use_scorer(E1_STRUCTURE, E1_STRUCTURE)
    first, task = _preview(client)
    again = client.post("/api/ai-tasks/import-structure",
                        files={"rules_file": ("r.xlsx", fx.unmapped_header_xlsx(), XLSX)})
    assert again.status_code == 200 and again.json()["id"] == task["id"]
    other = client.post("/api/ai-tasks/import-structure",
                        files={"rules_file": ("r.xlsx", _extra_column_book(), XLSX)})
    assert other.status_code == 202, other.text
    # 导入前识别没有评分标准，不随发布清理：再次发起时删掉已结束的旧任务。
    assert client.get(f"/api/ai-tasks/{task['id']}").status_code == 404


def test_preview_task_is_only_visible_to_its_owner(client, use_scorer):
    use_scorer(E1_STRUCTURE)
    _response, task = _preview(client)
    from fastapi import HTTPException

    from backend.app.api.deps import CurrentPrincipal
    from backend.app.api.routes import ai_tasks as routes

    principal = CurrentPrincipal(
        user_id="someone-else", organization_id=None, organization_role="teacher", platform_role="user",
    )
    with client.session_factory() as session:
        with pytest.raises(HTTPException) as excinfo:
            routes._task_or_404(session, task["id"], principal)
        assert excinfo.value.status_code == 404


def test_old_structure_endpoints_are_retired(client):
    rubric = _import(client, _extra_column_book(), name="旧接口")
    old_preview = client.post("/api/rubrics/import-files/structure-suggestions", data={"dry_run": "true"},
                              files={"rules_file": ("r.xlsx", fx.unmapped_header_xlsx(), XLSX)})
    assert old_preview.status_code == 410
    assert old_preview.json()["detail"]["context"]["replacement"] == "POST /api/ai-tasks/import-structure"
    old_draft = client.post(f"/api/rubrics/{rubric['id']}/structure-suggestions", json={})
    assert old_draft.status_code == 410
    assert old_draft.json()["detail"]["context"]["kind"] == "structure_suggestion"


def test_rubric_less_tasks_are_limited_to_structure_suggestion(client):
    from sqlalchemy.exc import IntegrityError

    with client.session_factory() as session:
        session.add(models.AITask(kind="rule_review", rubric_id=None, scope={}, input_snapshot={},
                                  fingerprint="a" * 64, status="queued"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_import_rejects_invalid_structure_override(client):
    response = client.post(
        "/api/rubrics/import-files",
        data={"name": "坏结构", "version": "v1",
              "structure_override": json.dumps({"sheet": "Sheet", "header_row": 1, "column_mapping": {"name": 1}})},
        files={"rules_file": ("r.xlsx", fx.unmapped_header_xlsx(), XLSX)},
    )
    assert response.status_code == 422
    assert "SCORE_COLUMN_REQUIRED" in response.text
    malformed = client.post("/api/rubrics/import-files",
                            data={"name": "坏", "version": "v1", "structure_override": "{"},
                            files={"rules_file": ("r.xlsx", fx.unmapped_header_xlsx(), XLSX)})
    assert malformed.status_code == 422


def _extra_column_book():
    """确定性解析把“分值”当满分列、忽略“细则”“配分”；“创新点”一行分值为空被丢弃。
    LLM 建议改认“配分”为满分列、“细则”为说明列：补全说明，并新增“创新点”。"""

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "评分"
    sheet.append(["评分项", "分值", "细则", "配分"])
    sheet.append(["选题", 10, "选题新颖", 10])
    sheet.append(["方法", 20, "方法可行", 20])
    sheet.append(["创新点", None, "有创新", 10])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


DRAFT_STRUCTURE = {
    "sheet": "评分", "header_row": "R1",
    "column_mapping": [{"col": "C1", "field": "name", "reason": "名称"},
                       {"col": "C2", "field": "ignore", "reason": "旧分值列"},
                       {"col": "C3", "field": "description", "reason": "细则"},
                       {"col": "C4", "field": "max_score", "reason": "配分"}],
    "row_types": [], "unresolved": [],
}


def _import(client, data, name="结构建议"):
    response = client.post("/api/rubrics/import-files", data={"name": name, "version": "v1"},
                           files={"rules_file": ("r.xlsx", data, XLSX)})
    assert response.status_code == 200, response.text
    return response.json()["rubric"]


def test_draft_structure_estimate_does_not_call_the_model(client, use_scorer):
    scorer = use_scorer()
    rubric = _import(client, _extra_column_book(), name="草稿估算")
    response = client.post(f"/api/rubrics/{rubric['id']}/structure-suggestions/estimate")
    assert response.status_code == 200, response.text
    assert response.json()["estimate"]["calls"] == 1
    assert scorer.calls == []


def test_draft_structure_suggestion_diff_merge_and_undo(client, use_scorer):
    rubric = _import(client, _extra_column_book())
    assert [c["name"] for c in rubric["criteria"]] == ["选题", "方法"]
    use_scorer(DRAFT_STRUCTURE)
    suggested, task, body = _suggest(client, rubric["id"])
    assert suggested.status_code == 202, suggested.text
    assert task["status"] == "succeeded", task
    assert task["result"]["target"] == "draft"
    kinds = {(i["kind"], i.get("field")) for i in body["items"]}
    assert kinds == {("fill", "description"), ("new", None)}
    assert body["stale"] is False
    assert client.get(f"/api/rubrics/{rubric['id']}/parse-coverage").json()["structure_suggestions"]["status"] == "pending"

    merged = client.post(f"/api/rubrics/{rubric['id']}/suggestions/merge",
                         json={"fingerprint": body["fingerprint"], "confirm": [], "exclude": [], "reason": "合入"})
    assert merged.status_code == 200, merged.text
    names = [c["name"] for c in merged.json()["rubric"]["criteria"]]
    assert names == ["选题", "方法", "创新点"]
    assert merged.json()["rubric"]["criteria"][0]["description"] == "选题新颖"
    state = client.get(f"/api/rubrics/{rubric['id']}/parse-coverage").json()
    assert state["structure_suggestions"]["status"] == "merged"

    # 同一建议不能再次合入
    again = client.post(f"/api/rubrics/{rubric['id']}/suggestions/merge",
                        json={"fingerprint": body["fingerprint"], "confirm": [], "exclude": [], "reason": "再次"})
    assert again.status_code == 409


def test_merge_is_refused_when_suggestion_is_stale(client, use_scorer):
    rubric = _import(client, _extra_column_book(), name="过期建议")
    use_scorer(DRAFT_STRUCTURE)
    _response, _task, body = _suggest(client, rubric["id"])
    response = client.post(f"/api/rubrics/{rubric['id']}/suggestions/merge",
                           json={"fingerprint": "0" * 64, "confirm": [], "exclude": [], "reason": "x"})
    assert response.status_code == 409
    assert "SUGGESTION_STALE" in response.text
    assert body["fingerprint"] != "0" * 64


def test_merge_is_refused_after_manual_edits(client, use_scorer):
    rubric = _import(client, _extra_column_book(), name="人工编辑后")
    workspace = client.get(f"/api/rubrics/{rubric['id']}/review-workspace").json()
    recompiled = client.post(f"/api/rubrics/{rubric['id']}/recompile", json={
        "supersedes_compilation_id": workspace["compilation_id"], "version": "v2", "criteria": rubric["criteria"],
        "atomic_rules": [{"id": r["id"], "content_token": r["content_token"], "changes": {"rule_text": r["rule_text"]}}
                         for r in workspace["rules"]]})
    assert recompiled.status_code == 200, recompiled.text
    use_scorer(DRAFT_STRUCTURE)
    response, _task, _view = _suggest(client, rubric["id"])
    assert response.status_code == 409
    assert "STRUCTURE_REPARSE_AFTER_EDIT" in response.text


def test_undo_restores_pre_merge_criteria(client, use_scorer):
    rubric = _import(client, _extra_column_book(), name="撤销合入")
    use_scorer(DRAFT_STRUCTURE)
    _response, _task, body = _suggest(client, rubric["id"])
    merged = client.post(f"/api/rubrics/{rubric['id']}/suggestions/merge",
                         json={"fingerprint": body["fingerprint"], "confirm": [], "exclude": [], "reason": "合入"})
    assert merged.status_code == 200, merged.text
    undone = client.post(f"/api/rubrics/{rubric['id']}/suggestions/undo", json={"reason": "撤销"})
    assert undone.status_code == 409  # 新增的评分项不能被删除：撤销只能在没有新增时回退
    assert "新增" in undone.text


def test_undo_reverts_a_merge_without_new_criteria(client, use_scorer):
    rubric = _import(client, _extra_column_book(), name="撤销补全")
    use_scorer(DRAFT_STRUCTURE)
    _response, _task, body = _suggest(client, rubric["id"])
    new_item = next(i for i in body["items"] if i["kind"] == "new")
    merged = client.post(f"/api/rubrics/{rubric['id']}/suggestions/merge",
                         json={"fingerprint": body["fingerprint"], "confirm": [], "exclude": [new_item["id"]],
                               "reason": "只合入补全"})
    assert merged.status_code == 200, merged.text
    assert [c["description"] for c in merged.json()["rubric"]["criteria"]] == ["选题新颖", "方法可行"]
    undone = client.post(f"/api/rubrics/{rubric['id']}/suggestions/undo", json={"reason": "撤销"})
    assert undone.status_code == 200, undone.text
    assert [c["description"] for c in undone.json()["rubric"]["criteria"]] == [None, None]
    state = client.get(f"/api/rubrics/{rubric['id']}/parse-coverage").json()
    assert state["structure_suggestions"]["status"] == "undone"


def test_structure_row_match_keeps_ids_and_requires_name_confirmation(client, use_scorer):
    book = Workbook()
    sheet = book.active
    sheet.title = '评分'
    sheet.append(['评分项', '分值', '备用名称'])
    sheet.append(['研究方法', 10, '方法与实现'])
    output = BytesIO()
    book.save(output)
    rubric = _import(client, output.getvalue())
    before = rubric['criteria'][0]
    structure = {**DRAFT_STRUCTURE, 'column_mapping': [
        {'col':'C3', 'field':'name', 'reason':'名称'},
        {'col':'C2', 'field':'max_score', 'reason':'满分'},
        {'col':'C1', 'field':'ignore', 'reason':'旧名称'},
    ]}
    use_scorer(structure)
    base = f"/api/rubrics/{rubric['id']}"
    response, task, suggestion = _suggest(client, rubric['id'])
    assert response.status_code == 202, response.text
    assert task['status'] == 'succeeded', task
    assert not any(i['kind'] == 'removed' for i in suggestion['items'])
    name_change = next(i for i in suggestion['items'] if i.get('field') == 'name')
    merged = client.post(base + '/suggestions/merge', json={
        'fingerprint':suggestion['fingerprint'], 'confirm':[name_change['id']],
        'exclude':[], 'reason':'合成行对齐验证',
    })
    assert merged.status_code == 200, merged.text
    after = client.get(base).json()['criteria'][0]
    assert (after['id'], after['code']) == (before['id'], before['code'])
    assert after['name'] == '方法与实现'


def test_draft_changed_while_recognizing_fails_the_task(client, use_scorer):
    rubric = _import(client, _extra_column_book(), name="识别期间重新导入")
    use_scorer(DRAFT_STRUCTURE)
    response = client.post(f"/api/rubrics/{rubric['id']}/ai-tasks", json={"kind": "structure_suggestion", "params": {}})
    assert response.status_code == 202, response.text
    with client.session_factory() as session:
        task = session.get(models.AITask, response.json()["id"])
        task.input_snapshot = {**task.input_snapshot, "compilation_id": "replaced-compilation"}
        session.commit()
    _drain(client)
    task = client.get(f"/api/ai-tasks/{response.json()['id']}").json()
    assert task["status"] == "failed"
    assert task["error_code"] == "STRUCTURE_SOURCE_CHANGED"
    assert client.get(f"/api/rubrics/{rubric['id']}/parse-coverage").json().get("structure_suggestions") is None
