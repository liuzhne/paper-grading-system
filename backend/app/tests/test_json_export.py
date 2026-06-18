"""结构化 JSON 导出端点（/scoring-runs/{id}/export.json）自包含测试。"""

from backend.app.tests.conftest import make_sample_docx

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_run_json_export(client):
    rubric = {
        "name": "导出测试",
        "version": "v1.0",
        "total_score": 10,
        "criteria": [{"code": "C1", "name": "研究方法", "max_score": 10, "display_order": 1}],
    }
    rubric_id = client.post("/api/rubrics", json=rubric).json()["id"]
    batch_id = client.post("/api/batches", json={"name": "批次", "rubric_id": rubric_id}).json()["id"]
    up = client.post(
        "/api/papers/upload",
        data={"batch_id": batch_id},
        files={"file": ("s.docx", make_sample_docx().getvalue(), DOCX_MIME)},
    )
    paper_id = up.json()["id"]
    assert client.post("/api/batches/%s/score" % batch_id).status_code == 200
    run_id = client.get("/api/scoring-runs", params={"paper_id": paper_id}).json()[0]["id"]

    export = client.get("/api/scoring-runs/%s/export.json" % run_id)
    assert export.status_code == 200, export.text
    data = export.json()
    assert data["schema"] == "paper-grading/run-export@1"
    assert data["run"]["id"] == run_id
    assert data["rubric"]["version"] == "v1.0"
    assert len(data["items"]) == 1
    assert data["items"][0]["criterion_code"] == "C1"
    assert "review_logs" in data and "paper" in data

    assert client.get("/api/scoring-runs/does-not-exist/export.json").status_code == 404
