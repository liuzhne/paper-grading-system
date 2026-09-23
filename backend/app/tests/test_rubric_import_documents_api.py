"""阶段 4 接口：仅 Word / 仅 Excel / 双文件导入，以及双文件冲突进入发布门禁。"""

from urllib.parse import quote

from backend.app.tests import rubric_parse_fixtures as fx

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _post(client, *, rules=None, template=None, name="文档角色"):
    files = {}
    if rules is not None:
        files["rules_file"] = ("rules.xlsx", rules, XLSX)
    if template is not None:
        files["template_file"] = ("template.docx", template, DOCX)
    return client.post("/api/rubrics/import-files", data={"name": name, "version": "v1"}, files=files)


def test_word_only_import_creates_draft_from_word_tables(client):
    response = _post(client, template=fx.rules_docx())
    assert response.status_code == 200, response.text
    body = response.json()
    assert [c["name"] for c in body["rubric"]["criteria"]] == ["问题分析", "方案设计", "报告规范"]
    assert body["unclaimed_summary"]["blocking"] == 1  # “迟交一天扣5分。”
    assert body["conflicts"] == []


def test_import_without_any_file_is_rejected(client):
    response = client.post("/api/rubrics/import-files", data={"name": "空", "version": "v1"})
    assert response.status_code == 400
    assert "至少上传一份" in response.text


def test_word_without_criteria_is_rejected_with_400(client):
    from io import BytesIO

    from docx import Document

    document = Document()
    document.add_paragraph("没有评分项")
    buffer = BytesIO()
    document.save(buffer)
    response = _post(client, template=buffer.getvalue())
    assert response.status_code == 400
    assert "Word 未解析到有效评分项" in response.text


def test_dual_upload_conflicts_block_publication_until_resolved(client):
    response = _post(client, rules=fx.simple_rules_xlsx(), template=fx.template_docx_with_rule_table(), name="冲突")
    assert response.status_code == 200, response.text
    body = response.json()
    assert {c["type"] for c in body["conflicts"]} == {"score_mismatch", "word_only"}
    rubric_id = body["rubric"]["id"]

    coverage = client.get(f"/api/rubrics/{rubric_id}/parse-coverage").json()
    assert [c["resolved"] for c in coverage["conflicts"]] == [False, False]
    blockers = client.get(f"/api/rubrics/{rubric_id}/review-workspace").json()["structural_blockers"]
    assert "unresolved_source_units" in {b["code"] for b in blockers}

    for conflict in body["conflicts"]:
        resolved = client.post(
            f"/api/rubrics/{rubric_id}/units/{quote(conflict['anchor_unit_id'], safe='')}/resolve",
            json={"action": "not_rule", "reason": "以 Excel 为准"},
        )
        assert resolved.status_code == 200, resolved.text
    coverage = client.get(f"/api/rubrics/{rubric_id}/parse-coverage").json()
    assert [c["resolved"] for c in coverage["conflicts"]] == [True, True]
    blockers = client.get(f"/api/rubrics/{rubric_id}/review-workspace").json()["structural_blockers"]
    assert "unresolved_source_units" not in {b["code"] for b in blockers}
