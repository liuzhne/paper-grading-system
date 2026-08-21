from backend.app.tests.conftest import make_sample_docx_with_required_owner
from backend.app.tests.conftest import publish_rubric_via_api

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _upload(client, batch_id, name):
    response = client.post(
        "/api/papers/upload",
        data={"batch_id": batch_id},
        files={
            "file": (
                name,
                make_sample_docx_with_required_owner().getvalue(),
                DOCX_MIME,
            )
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_batch_ranking_and_drift(client):
    rubric = {
        "name": "L2批量",
        "version": "v1.0",
        "total_score": 10,
        "criteria": [
            {
                "code": "C1",
                "name": "研究方法",
                "max_score": 10,
                "criterion_type": "deterministic",
                "scoring_mode": "deductive",
                "deduction_rules_structured": [
                    {
                        "match": "研究方法论述不足",
                        "points": 10,
                        "reason": "研究方法论述不足",
                        "checker_key": "thesis.legacy_required_fields.v1",
                        "checker_params": {
                            "criterion_code": "C1",
                            "applies_to": "global",
                        },
                    }
                ],
                "display_order": 1,
            }
        ],
    }
    rubric_id = client.post("/api/rubrics", json=rubric).json()["id"]
    publish_rubric_via_api(client, rubric_id)
    batch_id = client.post("/api/batches", json={"name": "批次", "rubric_id": rubric_id}).json()["id"]
    paper_ids = [_upload(client, batch_id, "s0.docx"), _upload(client, batch_id, "s1.docx")]

    assert client.post("/api/batches/%s/score" % batch_id).status_code == 200

    # 两篇都人工下调到 2.0 → 制造系统性漂移（AI 偏宽）。
    for paper_id in paper_ids:
        run_id = client.get("/api/scoring-runs", params={"paper_id": paper_id}).json()[0]["id"]
        item_id = client.get("/api/scoring-runs/%s/items" % run_id).json()[0]["id"]
        patch = client.patch("/api/score-items/%s" % item_id, json={"final_score": 2.0, "reason": "人工下调"})
        assert patch.status_code == 200, patch.text

    ranking = client.get("/api/batches/%s/ranking" % batch_id).json()
    assert ranking["scored_count"] == 2
    assert ranking["ranking"][0]["rank"] == 1
    assert all("percentile" in row for row in ranking["ranking"])

    drift = client.get("/api/batches/%s/drift" % batch_id).json()
    c1 = next(row for row in drift["criteria"] if row["criterion_code"] == "C1")
    assert c1["n_adjusted"] == 2
    assert c1["bias"] < 0  # final < ai → 人工下调
    assert c1["flagged"] is True
    assert "偏宽" in c1["direction"]


def test_ranking_404_for_missing_batch(client):
    assert client.get("/api/batches/nope/ranking").status_code == 404
    assert client.get("/api/batches/nope/drift").status_code == 404
