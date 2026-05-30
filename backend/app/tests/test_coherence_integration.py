from backend.app.tests.conftest import make_sample_docx

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_scoring_run_records_merged_coherence_findings(client):
    rubric = {
        "name": "一致性测试",
        "version": "v1.0",
        "total_score": 10,
        "criteria": [{"code": "C1", "name": "研究方法", "max_score": 10, "display_order": 1}],
    }
    rubric_id = client.post("/api/rubrics", json=rubric).json()["id"]
    batch_id = client.post("/api/batches", json={"name": "b", "rubric_id": rubric_id}).json()["id"]
    upload = client.post(
        "/api/papers/upload",
        data={"batch_id": batch_id},
        files={"file": ("s.docx", make_sample_docx().getvalue(), DOCX_MIME)},
    )
    run = client.post("/api/papers/%s/score" % upload.json()["id"], json={})
    assert run.status_code == 200, run.text

    findings = run.json()["coherence_findings"]
    assert isinstance(findings, list)
    # 样例论文参考文献[1]未被正文引用 → 确定性一致性发现至少含一条 reference_uncited（已落到评分运行）。
    assert any(f["kind"] == "reference_uncited" for f in findings)
