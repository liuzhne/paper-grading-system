"""阶段 6 ④接口：导入预检、带结构导入、草稿结构建议、合入与撤销。"""

import json
from io import BytesIO

from openpyxl import Workbook

from backend.app.api.routes import rubrics as rubric_routes
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


def _use_scorer(monkeypatch, *responses):
    scorer = FakeScorer(*responses)
    monkeypatch.setattr(rubric_routes, "get_llm_scorer", lambda *a, **k: scorer)
    return scorer


def test_preview_dry_run_returns_estimate_without_calling_model(client, monkeypatch):
    scorer = _use_scorer(monkeypatch)
    response = client.post("/api/rubrics/import-files/structure-suggestions", data={"dry_run": "true"},
                           files={"rules_file": ("r.xlsx", fx.unmapped_header_xlsx(), XLSX)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["failure_codes"] == ["E1"]
    assert body["estimate"]["calls"] == 1 and body["estimate"]["chars"] > 0
    assert scorer.calls == []


def test_preview_then_import_with_confirmed_structure(client, monkeypatch):
    _use_scorer(monkeypatch, E1_STRUCTURE)
    preview = client.post("/api/rubrics/import-files/structure-suggestions",
                          files={"rules_file": ("r.xlsx", fx.unmapped_header_xlsx(), XLSX)})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["override"]["column_mapping"] == {"name": 1, "max_score": 2, "description": 3}
    assert body["preview"] == [{"name": "选题", "max_score": 10.0, "row_number": 2}]
    imported = client.post(
        "/api/rubrics/import-files",
        data={"name": "按结构导入", "version": "v1", "structure_override": json.dumps(body["override"])},
        files={"rules_file": ("r.xlsx", fx.unmapped_header_xlsx(), XLSX)},
    )
    assert imported.status_code == 200, imported.text
    assert [c["name"] for c in imported.json()["rubric"]["criteria"]] == ["选题"]


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


def test_draft_structure_suggestion_diff_merge_and_undo(client, monkeypatch):
    rubric = _import(client, _extra_column_book())
    assert [c["name"] for c in rubric["criteria"]] == ["选题", "方法"]
    _use_scorer(monkeypatch, DRAFT_STRUCTURE)
    suggested = client.post(f"/api/rubrics/{rubric['id']}/structure-suggestions", json={})
    assert suggested.status_code == 200, suggested.text
    body = suggested.json()
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


def test_merge_is_refused_when_suggestion_is_stale(client, monkeypatch):
    rubric = _import(client, _extra_column_book(), name="过期建议")
    _use_scorer(monkeypatch, DRAFT_STRUCTURE)
    body = client.post(f"/api/rubrics/{rubric['id']}/structure-suggestions", json={}).json()
    response = client.post(f"/api/rubrics/{rubric['id']}/suggestions/merge",
                           json={"fingerprint": "0" * 64, "confirm": [], "exclude": [], "reason": "x"})
    assert response.status_code == 409
    assert "SUGGESTION_STALE" in response.text
    assert body["fingerprint"] != "0" * 64


def test_merge_is_refused_after_manual_edits(client, monkeypatch):
    rubric = _import(client, _extra_column_book(), name="人工编辑后")
    workspace = client.get(f"/api/rubrics/{rubric['id']}/review-workspace").json()
    recompiled = client.post(f"/api/rubrics/{rubric['id']}/recompile", json={
        "supersedes_compilation_id": workspace["compilation_id"], "version": "v2", "criteria": rubric["criteria"],
        "atomic_rules": [{"id": r["id"], "content_token": r["content_token"], "changes": {"rule_text": r["rule_text"]}}
                         for r in workspace["rules"]]})
    assert recompiled.status_code == 200, recompiled.text
    _use_scorer(monkeypatch, DRAFT_STRUCTURE)
    response = client.post(f"/api/rubrics/{rubric['id']}/structure-suggestions", json={})
    assert response.status_code == 409
    assert "STRUCTURE_REPARSE_AFTER_EDIT" in response.text


def test_undo_restores_pre_merge_criteria(client, monkeypatch):
    rubric = _import(client, _extra_column_book(), name="撤销合入")
    _use_scorer(monkeypatch, DRAFT_STRUCTURE)
    body = client.post(f"/api/rubrics/{rubric['id']}/structure-suggestions", json={}).json()
    merged = client.post(f"/api/rubrics/{rubric['id']}/suggestions/merge",
                         json={"fingerprint": body["fingerprint"], "confirm": [], "exclude": [], "reason": "合入"})
    assert merged.status_code == 200, merged.text
    undone = client.post(f"/api/rubrics/{rubric['id']}/suggestions/undo", json={"reason": "撤销"})
    assert undone.status_code == 409  # 新增的评分项不能被删除：撤销只能在没有新增时回退
    assert "新增" in undone.text


def test_undo_reverts_a_merge_without_new_criteria(client, monkeypatch):
    rubric = _import(client, _extra_column_book(), name="撤销补全")
    _use_scorer(monkeypatch, DRAFT_STRUCTURE)
    body = client.post(f"/api/rubrics/{rubric['id']}/structure-suggestions", json={}).json()
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
