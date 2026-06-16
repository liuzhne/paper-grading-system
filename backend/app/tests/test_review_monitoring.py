from backend.app.tests.conftest import make_sample_docx

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _upload(client, batch_id, name):
    response = client.post(
        "/api/papers/upload",
        data={"batch_id": batch_id},
        files={"file": (name, make_sample_docx().getvalue(), DOCX_MIME)},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _make_scored_batch(client, n=4):
    rubric = {
        "name": "监控批",
        "version": "v1.0",
        "total_score": 10,
        "criteria": [{"code": "C1", "name": "研究方法", "max_score": 10, "display_order": 1}],
    }
    rubric_id = client.post("/api/rubrics", json=rubric).json()["id"]
    batch_id = client.post("/api/batches", json={"name": "批次", "rubric_id": rubric_id}).json()["id"]
    paper_ids = [_upload(client, batch_id, "s%d.docx" % i) for i in range(n)]
    assert client.post("/api/batches/%s/score" % batch_id).status_code == 200
    return batch_id, paper_ids


def test_review_sample_is_deterministic_and_hits_ratio(client):
    batch_id, _ = _make_scored_batch(client, n=4)

    first = client.get("/api/batches/%s/review-sample" % batch_id, params={"ratio": 0.5}).json()
    second = client.get("/api/batches/%s/review-sample" % batch_id, params={"ratio": 0.5}).json()

    assert first["total"] == 4
    assert first["target"] == 2
    assert first["sample_size"] >= 2  # 抽样补足；若有 must-review 可更多
    # 确定性：同 batch+seed 两次结果一致（可复现，设计哲学#5）
    assert [r["paper_id"] for r in first["selected"]] == [r["paper_id"] for r in second["selected"]]


def test_drift_monitor_low_coverage_untrusted(client):
    batch_id, _ = _make_scored_batch(client, n=4)
    # 未做任何人工复核 → 覆盖率 0 < 门槛 → 信号不可信
    monitor = client.get("/api/batches/%s/drift-monitor" % batch_id).json()
    assert monitor["scored_count"] == 4
    assert monitor["reviewed_count"] == 0
    assert monitor["review_coverage"] == 0.0
    assert "不可信" in monitor["verdict"]


def test_drift_monitor_flags_when_covered_and_biased(client):
    batch_id, paper_ids = _make_scored_batch(client, n=4)
    # 全部人工下调 → 高覆盖 + 系统性偏宽
    for paper_id in paper_ids:
        run_id = client.get("/api/scoring-runs", params={"paper_id": paper_id}).json()[0]["id"]
        item_id = client.get("/api/scoring-runs/%s/items" % run_id).json()[0]["id"]
        patch = client.patch("/api/score-items/%s" % item_id, json={"final_score": 2.0, "reason": "人工下调"})
        assert patch.status_code == 200, patch.text

    monitor = client.get("/api/batches/%s/drift-monitor" % batch_id).json()
    assert monitor["reviewed_count"] == 4
    assert monitor["review_coverage"] == 1.0
    assert "C1" in monitor["flagged_criteria"]
    assert monitor["verdict"] == "检测到系统性漂移，建议校准"


def test_review_sample_404_for_missing_batch(client):
    assert client.get("/api/batches/nope/review-sample").status_code == 404
    assert client.get("/api/batches/nope/drift-monitor").status_code == 404
