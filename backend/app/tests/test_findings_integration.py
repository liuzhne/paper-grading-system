from io import BytesIO

from docx import Document

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_with_figure_ref():
    doc = Document()
    doc.add_paragraph("第一章 绪论")
    doc.add_paragraph("本文方法见图3，说明实验设计、数据来源与结果分析，论述较充分。")
    doc.add_paragraph("结论")
    doc.add_paragraph("研究结论表明该方法在教学质量评价上有效。")
    doc.add_paragraph("参考文献")
    doc.add_paragraph("[1] 张三. 教学评价研究. 2024.")
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def test_findings_enabled_criterion_deducts_by_rubric_rule(client):
    # 评分项由用户在 rubric 里定义：维度=规范性 + 结构化扣分规则（图引用问题 -2）。
    rubric = {
        "name": "规范性自动扣分",
        "version": "v1.0",
        "total_score": 10,
        "criteria": [
            {
                "code": "D1",
                "name": "规范性",
                "max_score": 10,
                "criterion_type": "deterministic",
                "dimension": "规范性",
                "deduction_rules_structured": [{"match": "图", "points": 2, "reason": "图表引用缺题注", "source": "manual"}],
                "display_order": 1,
            }
        ],
    }
    rubric_id = client.post("/api/rubrics", json=rubric).json()["id"]
    batch_id = client.post("/api/batches", json={"name": "b", "rubric_id": rubric_id}).json()["id"]
    upload = client.post(
        "/api/papers/upload",
        data={"batch_id": batch_id},
        files={"file": ("s.docx", _docx_with_figure_ref(), DOCX_MIME)},
    )
    assert upload.status_code == 200, upload.text
    run = client.post("/api/papers/%s/score" % upload.json()["id"], json={})
    assert run.status_code == 200, run.text

    item = client.get("/api/scoring-runs/%s/items" % run.json()["id"]).json()[0]
    assert any(d["rule_ref"] == "D1" for d in item["deduction_items"])
    assert float(item["final_score"]) == 8.0  # 10 − 2（图3 引用无题注，warning 命中规则）

    # 该 finding 在评分运行里被标记"已计入扣分"。
    coherence = run.json()["coherence_findings"]
    assert any(f.get("deducted_by") == "D1" for f in coherence)


def test_criterion_without_dimension_leaves_findings_advisory(client):
    # 未显式启用（无 dimension/规则）的评分项：findings 仅进报告，不自动扣分。
    rubric = {
        "name": "仅报告不扣",
        "version": "v1.0",
        "total_score": 10,
        "criteria": [{"code": "C1", "name": "研究方法", "max_score": 10, "display_order": 1}],
    }
    rubric_id = client.post("/api/rubrics", json=rubric).json()["id"]
    batch_id = client.post("/api/batches", json={"name": "b", "rubric_id": rubric_id}).json()["id"]
    upload = client.post(
        "/api/papers/upload",
        data={"batch_id": batch_id},
        files={"file": ("s.docx", _docx_with_figure_ref(), DOCX_MIME)},
    )
    run = client.post("/api/papers/%s/score" % upload.json()["id"], json={})
    coherence = run.json()["coherence_findings"]
    assert any(f["kind"] == "figure_ref_missing" for f in coherence)  # 发现仍在
    assert all("deducted_by" not in f for f in coherence)  # 但未被扣分
