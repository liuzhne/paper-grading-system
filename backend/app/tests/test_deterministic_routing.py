from backend.app.tests.conftest import make_sample_docx

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_deterministic_criterion_is_scored_by_checker_not_llm(client):
    rubric_payload = {
        "name": "确定性规则",
        "version": "v1.0",
        "total_score": 15,
        "criteria": [
            {
                "code": "D01",
                "name": "参考文献",
                "max_score": 15,
                "criterion_type": "deterministic",
                "evidence_hints": ["参考文献"],
                "deduction_rules": [],
                "display_order": 1,
            }
        ],
    }
    rubric_id = client.post("/api/rubrics", json=rubric_payload).json()["id"]
    batch_id = client.post("/api/batches", json={"name": "批次", "rubric_id": rubric_id}).json()["id"]

    upload = client.post(
        "/api/papers/upload",
        data={"batch_id": batch_id},
        files={"file": ("sample.docx", make_sample_docx().getvalue(), DOCX_MIME)},
    )
    assert upload.status_code == 200, upload.text
    paper_id = upload.json()["id"]

    run = client.post("/api/papers/%s/score" % paper_id, json={})
    assert run.status_code == 200, run.text
    run_id = run.json()["id"]
    # 确定性评分不消耗 token。
    assert run.json()["total_tokens"] in (0, None)

    items = client.get("/api/scoring-runs/%s/items" % run_id).json()
    assert len(items) == 1
    item = items[0]
    # 确定性检查器：confidence 恒为 1.0（mock 永不返回 1.0），证据齐全 → 满分，无缺失引用扣分。
    assert float(item["confidence"]) == 1.0
    assert float(item["final_score"]) == 15.0
    assert all(d.get("rule_ref") != "CITATION_MISSING" for d in item["deduction_items"])
