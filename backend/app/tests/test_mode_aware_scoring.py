from copy import deepcopy

from backend.app.db import models
from backend.app.tests.conftest import make_sample_docx
from backend.app.tests.conftest import publish_rubric_via_api

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _score_single_criterion(client, criterion):
    rubric = {"name": "模式测试-%s" % criterion["code"], "version": "v1.0", "total_score": criterion["max_score"], "criteria": [criterion]}
    rubric_id = client.post("/api/rubrics", json=rubric).json()["id"]
    publish_rubric_via_api(client, rubric_id)
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
    with client.session_factory() as db:
        stored_item = db.get(models.ScoreItem, items[0]["id"])
        stored_run = db.get(models.ScoringRun, run.json()["id"])
        audit = {
            "rule_results_schema_version": stored_item.rule_results_schema_version,
            "rule_results": deepcopy(stored_item.rule_results),
            "plan": deepcopy(stored_run.execution_plan_snapshot),
        }
    return items[0], audit


def test_deductive_criterion_uses_the_published_atomic_rule(client):
    item, audit = _score_single_criterion(
        client,
        {
            "code": "D1",
            "name": "研究方法",
            "max_score": 20,
            "scoring_mode": "deductive",
            "deduction_rules_structured": [
                {
                    "match": "研究方法论述不足",
                    "points": 20,
                    "reason": "研究方法论述不足",
                }
            ],
            "display_order": 1,
        },
    )
    assert item["deduction_items"] == []  # M4 audit lives in rule_results.
    assert audit["rule_results_schema_version"] == "rule-results@2"
    assert len(audit["rule_results"]) == 1
    result = audit["rule_results"][0]
    assert result["direction"] == "deduct"
    assert result["effect_type"] == "score"
    assert result["criterion_code"] == "D1"
    assert result["rule_code"].startswith("manual.d1.deduct.")


def test_banded_criterion_freezes_the_published_levels(client):
    bands = [
        {"label": "优", "points": 20},
        {"label": "良", "points": 14},
        {"label": "中", "points": 8},
        {"label": "差", "points": 0},
    ]
    item, audit = _score_single_criterion(
        client,
        {"code": "B1", "name": "创新性", "max_score": 20, "scoring_mode": "banded", "rubric_levels": bands, "display_order": 1},
    )
    assert item["band_selection"] is None  # M4 audit lives in rule_results.
    result = audit["rule_results"][0]
    assert result["direction"] == "band"
    assert result["effect_type"] == "score"
    assert result["criterion_code"] == "B1"
    node = next(
        node
        for node in audit["plan"]["nodes"]
        if node["criterion_code"] == "B1"
    )
    assert {
        level["level_code"]
        for level in node["atomic_rule_snapshot"]["levels"]
    } == {
        "优",
        "良",
        "中",
        "差",
    }
