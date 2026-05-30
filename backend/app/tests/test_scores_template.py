from io import BytesIO
from types import SimpleNamespace

from openpyxl import load_workbook

from backend.app.eval.labeled_dataset import load_scores_table
from backend.app.eval.scores_template import build_scores_table_template


def test_template_headers_match_criteria_and_roundtrip(tmp_path):
    criteria = [
        SimpleNamespace(code="C01", name="研究方法", max_score=10),
        SimpleNamespace(code="C02", name="参考文献", max_score=10),
    ]
    workbook = load_workbook(BytesIO(build_scores_table_template(criteria)))
    sheet = workbook["教师评分"]
    assert [cell.value for cell in sheet[1]] == ["文件名", "总分", "C01", "C02"]
    assert "填写说明" in workbook.sheetnames

    # 模板表头能被成绩表解析器识别：填一行后往返解析正确。
    sheet.append(["张三.docx", 86, 13, 17])
    path = tmp_path / "filled.xlsx"
    workbook.save(path)
    assert load_scores_table(str(path)) == [{"filename": "张三.docx", "total": 86.0, "items": {"C01": 13.0, "C02": 17.0}}]


def test_scores_template_endpoint_uses_rubric_codes(client):
    rubric = {
        "name": "模板下载",
        "version": "v1.0",
        "total_score": 10,
        "criteria": [{"code": "C01", "name": "研究方法", "max_score": 10, "display_order": 1}],
    }
    rubric_id = client.post("/api/rubrics", json=rubric).json()["id"]
    response = client.get("/api/rubrics/%s/scores-template.xlsx" % rubric_id)
    assert response.status_code == 200
    assert "spreadsheetml" in response.headers["content-type"]
    workbook = load_workbook(BytesIO(response.content))
    assert [cell.value for cell in workbook["教师评分"][1]] == ["文件名", "总分", "C01"]


def test_scores_template_endpoint_404_for_missing_rubric(client):
    assert client.get("/api/rubrics/missing/scores-template.xlsx").status_code == 404
