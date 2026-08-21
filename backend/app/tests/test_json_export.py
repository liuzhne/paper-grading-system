"""结构化 JSON 导出端点（/scoring-runs/{id}/export.json）自包含测试。"""

from backend.app.core.config import settings
from backend.app.tests.conftest import make_sample_docx
from backend.app.tests.conftest import publish_rubric_via_api
from backend.app.services.scoring.profiles.thesis import ThesisProfile

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_run_json_export(client, monkeypatch):
    projections = []
    original = ThesisProfile.build_artifact_projection

    def record_projection(profile, **kwargs):
        projection = original(profile, **kwargs)
        projections.append(projection)
        return projection

    monkeypatch.setattr(
        ThesisProfile,
        "build_artifact_projection",
        record_projection,
    )
    rubric = {
        "name": "导出测试",
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
    # owner_id 自 batch→paper→run 贯通（单租户=dev 用户）
    assert data["run"]["owner_id"] == settings.DEFAULT_DEV_USER_ID
    assert projections and projections[-1]["profile_key"] == "thesis"

    assert client.get("/api/scoring-runs/does-not-exist/export.json").status_code == 404
