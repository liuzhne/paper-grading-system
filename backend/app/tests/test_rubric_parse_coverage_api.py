"""导入接口收敛、覆盖率查询、单元人工处理与“疑似规则未处理”发布门禁。"""

from io import BytesIO
from urllib.parse import quote

from openpyxl import Workbook

from backend.app.tests.conftest import make_rules_xlsx
from backend.app.tests.conftest import publish_rubric_via_api
from backend.app.tests.conftest import review_rubric_via_api

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BLOCKING_UNIT = "xlsx:评分规则!R4C4"


def _rules_with_blocking_row():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "评分规则"
    sheet.append(["编号", "评分项", "分值", "评分说明", "扣分规则"])
    sheet.append(["C01", "研究方法", 60, "方法合理。", "方法说明不足扣3分"])
    sheet.append(["C02", "文献综述", 40, "综述充分。", "文献不足扣2分"])
    sheet.append([None, "写作规范", None, "错别字每处扣1分"])  # 无满分 → 丢弃，含扣分信号
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _clean_rules():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "评分规则"
    sheet.append(["编号", "评分项", "分值", "评分说明", "扣分规则"])
    sheet.append(["C01", "研究方法", 60, "方法合理。", "方法说明不足扣3分"])
    sheet.append(["C02", "文献综述", 40, "综述充分。", "文献不足扣2分"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _import(client, rules, name="覆盖率门禁"):
    response = client.post(
        "/api/rubrics/import-files",
        data={"name": name, "version": "v1"},
        files={"rules_file": ("rules.xlsx", rules, XLSX)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _resolve(client, rubric_id, unit_id, body):
    return client.post(f"/api/rubrics/{rubric_id}/units/{quote(unit_id, safe='')}/resolve", json=body)


def test_import_response_contains_coverage_triggers_and_unclaimed_summary(client):
    body = _import(client, _rules_with_blocking_row())
    assert body["coverage"]["blocking_count"] == 1
    assert body["unclaimed_summary"] == {"total": 2, "suspected": 1, "blocking": 1}
    assert {t["code"] for t in body["triggers"]} >= {"E5"}
    assert body["warnings"] == []
    assert body["template_summary"]["section_titles"] == []


def test_import_keeps_existing_response_for_clean_workbook(client):
    body = _import(client, make_rules_xlsx().getvalue(), name="干净模板")
    assert body["coverage"]["blocking_count"] == 0
    assert body["triggers"] == []
    assert body["rubric"]["criteria"][0]["code"] == "C01"


def test_invalid_rules_file_is_rejected_with_400(client):
    response = client.post(
        "/api/rubrics/import-files",
        data={"name": "坏文件", "version": "v1"},
        files={"rules_file": ("rules.xlsx", b"not a workbook", XLSX)},
    )
    assert response.status_code == 400


def test_parse_coverage_endpoint_reports_latest_ledger(client):
    rubric_id = _import(client, _rules_with_blocking_row())["rubric"]["id"]
    response = client.get(f"/api/rubrics/{rubric_id}/parse-coverage")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_ledger"] is True
    assert body["coverage"]["blocking_count"] == 1
    assert body["extraction"]["dropped_rows"][0]["row_number"] == 4
    blocking = [item for item in body["coverage"]["unclaimed"] if item["blocking"]]
    assert [item["unit_id"] for item in blocking] == [BLOCKING_UNIT]


def test_parse_coverage_for_manual_rubric_has_no_ledger(client):
    created = client.post("/api/rubrics", json={
        "name": "手工", "version": "v1", "total_score": 10,
        "criteria": [{"code": "T01", "name": "论证", "max_score": 10, "scoring_mode": "deductive",
                      "deduction_rules_structured": [{"match": "缺少论证", "points": 2, "reason": "合成"}]}]})
    assert created.status_code == 200, created.text
    body = client.get(f"/api/rubrics/{created.json()['id']}/parse-coverage").json()
    assert body["has_ledger"] is False
    assert body["coverage"] is None


def test_unresolved_blocking_unit_blocks_publication_until_resolved(client):
    rubric_id = _import(client, _rules_with_blocking_row())["rubric"]["id"]
    identity = review_rubric_via_api(client, rubric_id)
    denied = client.post(f"/api/rubrics/{rubric_id}/publish",
                         json={"compilation_id": identity["compilation_id"], "reason": "尝试发布"})
    assert denied.status_code == 400, denied.text  # 现有发布接口对生命周期错误返回 400
    assert "unresolved_source_units" in denied.text

    # 发布被拒后退回草稿再处理单元
    assert client.post(f"/api/rubrics/{rubric_id}/return-to-draft", json={"reason": "处理未认领内容"}).status_code == 200
    resolved = _resolve(client, rubric_id, BLOCKING_UNIT, {"action": "not_rule", "reason": "已并入 C02 的评分说明"})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["coverage"]["blocking_count"] == 0
    publish_rubric_via_api(client, rubric_id)


def test_resolve_assign_claims_unit_for_existing_criterion_and_is_audited(client):
    rubric_id = _import(client, _rules_with_blocking_row())["rubric"]["id"]
    response = _resolve(client, rubric_id, BLOCKING_UNIT, {"action": "assign", "criterion_code": "C02",
                                                           "reason": "属于文献综述的补充"})
    assert response.status_code == 200, response.text
    unit = response.json()["unit"]
    assert unit["status"] == "consumed"
    assert unit["claimed_by"] == ["C02.manual"]
    assert unit["extracted_by"] == "human"
    with client.session_factory() as session:
        from sqlalchemy import select

        from backend.app.db import models

        compilation = session.scalars(select(models.RubricCompilation).where(
            models.RubricCompilation.rubric_id == rubric_id)).one()
        events = [e for e in compilation.human_changes if e.get("action") == "unit_resolution"]
        assert events[-1]["unit_id"] == BLOCKING_UNIT
        assert events[-1]["decision"] == "assign"


def test_resolve_rejects_bad_requests(client):
    rubric_id = _import(client, _rules_with_blocking_row())["rubric"]["id"]
    assert _resolve(client, rubric_id, "xlsx:评分规则!R99C1", {"action": "not_rule", "reason": "x"}).status_code == 404
    assert _resolve(client, rubric_id, BLOCKING_UNIT, {"action": "delete", "reason": "x"}).status_code == 422
    assert _resolve(client, rubric_id, BLOCKING_UNIT, {"action": "assign", "reason": "x"}).status_code == 422
    assert _resolve(client, rubric_id, BLOCKING_UNIT,
                    {"action": "assign", "criterion_code": "NOPE", "reason": "x"}).status_code == 422
    assert _resolve(client, rubric_id, BLOCKING_UNIT, {"action": "not_rule", "reason": " "}).status_code == 422


def test_resolve_is_rejected_for_published_rubric(client):
    rubric_id = _import(client, _clean_rules(), name="已发布")["rubric"]["id"]
    publish_rubric_via_api(client, rubric_id)
    response = _resolve(client, rubric_id, "xlsx:评分规则!R1C1", {"action": "not_rule", "reason": "x"})
    assert response.status_code == 409


def test_recompile_carries_ledger_and_resolutions_forward(client):
    body = _import(client, _rules_with_blocking_row())
    rubric = body["rubric"]
    workspace = client.get(f"/api/rubrics/{rubric['id']}/review-workspace").json()
    first = workspace["rules"][0]
    recompiled = client.post(f"/api/rubrics/{rubric['id']}/recompile", json={
        "supersedes_compilation_id": workspace["compilation_id"], "version": "v2", "criteria": rubric["criteria"],
        "atomic_rules": [{"id": rule["id"], "content_token": rule["content_token"],
                          "changes": {"rule_text": "修改后的合成条款" if rule is first else rule["rule_text"]}}
                         for rule in workspace["rules"]]})
    assert recompiled.status_code == 200, recompiled.text
    after = client.get(f"/api/rubrics/{rubric['id']}/parse-coverage").json()
    assert after["has_ledger"] is True
    assert after["coverage"]["blocking_count"] == 1
    assert after["compilation_id"] != workspace["compilation_id"]
    assert "unresolved_source_units" in {
        item["code"] for item in client.get(f"/api/rubrics/{rubric['id']}/review-workspace").json()["structural_blockers"]
    }


def test_batch_resolve_marks_all_units_and_is_all_or_nothing(client):
    body = _import(client, _rules_with_blocking_row())
    rubric_id = body["rubric"]["id"]
    units = [item["unit_id"] for item in body["coverage"]["unclaimed"]]
    assert len(units) == 2
    bad = client.post(f"/api/rubrics/{rubric_id}/units/resolve-batch",
                      json={"unit_ids": [*units, "xlsx:评分规则!R99C9"], "action": "not_rule", "reason": "批量忽略"})
    assert bad.status_code == 404
    assert client.get(f"/api/rubrics/{rubric_id}/parse-coverage").json()["unclaimed_summary"]["total"] == 2
    ok = client.post(f"/api/rubrics/{rubric_id}/units/resolve-batch",
                     json={"unit_ids": units, "action": "not_rule", "reason": "批量忽略"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["coverage"]["blocking_count"] == 0
    assert [u["status"] for u in ok.json()["units"]] == ["context", "context"]
    empty = client.post(f"/api/rubrics/{rubric_id}/units/resolve-batch",
                        json={"unit_ids": [], "action": "not_rule", "reason": "x"})
    assert empty.status_code == 422
