from backend.app.tests.conftest import make_sample_docx

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _score_single_criterion(client, criterion):
    rubric = {"name": "模式测试-%s" % criterion["code"], "version": "v1.0", "total_score": criterion["max_score"], "criteria": [criterion]}
    rubric_id = client.post("/api/rubrics", json=rubric).json()["id"]
    batch_id = client.post("/api/batches", json={"name": "批次", "rubric_id": rubric_id}).json()["id"]
    upload = client.post(
        "/api/papers/upload",
        data={"batch_id": batch_id},
        files={"file": ("s.docx", make_sample_docx().getvalue(), DOCX_MIME)},
    )
    assert upload.status_code == 200, upload.text
    run = client.post("/api/papers/%s/score" % upload.json()["id"], json={})
    assert run.status_code == 200, run.text
    items = client.get("/api/scoring-runs/%s/items" % run.json()["id"]).json()
    assert len(items) == 1
    return items[0]


def test_deductive_criterion_scored_from_structured_points(client):
    item = _score_single_criterion(
        client,
        {"code": "D1", "name": "研究方法", "max_score": 20, "scoring_mode": "deductive", "display_order": 1},
    )
    assert item["deduction_items"]
    assert all(isinstance(d["points"], (int, float)) for d in item["deduction_items"])
    assert "扣分制核算" in item["reason"]
    total_points = sum(float(d["points"]) for d in item["deduction_items"])
    assert abs(float(item["final_score"]) - max(0.0, 20 - total_points)) < 0.01


def test_banded_criterion_selects_a_band(client):
    bands = [
        {"label": "优", "points": 20},
        {"label": "良", "points": 14},
        {"label": "中", "points": 8},
        {"label": "差", "points": 0},
    ]
    item = _score_single_criterion(
        client,
        {"code": "B1", "name": "创新性", "max_score": 20, "scoring_mode": "banded", "rubric_levels": bands, "display_order": 1},
    )
    assert item["band_selection"] is not None
    assert item["band_selection"]["level"] in {"优", "良", "中", "差"}
    assert float(item["final_score"]) in {20.0, 14.0, 8.0, 0.0}
