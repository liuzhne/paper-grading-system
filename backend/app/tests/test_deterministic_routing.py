from copy import deepcopy

from backend.app.db import models
from backend.app.tests.conftest import make_sample_docx
from backend.app.tests.conftest import publish_rubric_via_api

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
                "scoring_mode": "deductive",
                "evidence_hints": ["参考文献"],
                "deduction_rules": [],
                "deduction_rules_structured": [
                    {
                        "match": "参考文献缺失",
                        "points": 15,
                        "reason": "参考文献缺失",
                        "checker_key": "thesis.legacy_required_fields.v1",
                        "checker_params": {
                            "criterion_code": "D01",
                            "applies_to": "global",
                        },
                    }
                ],
                "display_order": 1,
            }
        ],
    }
    rubric_id = client.post("/api/rubrics", json=rubric_payload).json()["id"]
    publish_rubric_via_api(client, rubric_id)
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
    # M4 的权威审计轨迹是 rule_results；不再以兼容用的
    # confidence / deduction_items 字段判定执行语义。
    with client.session_factory() as db:
        stored_item = db.get(models.ScoreItem, item["id"])
        stored_run = db.get(models.ScoringRun, run_id)
        rule_results = deepcopy(stored_item.rule_results)
        plan = deepcopy(stored_run.execution_plan_snapshot)
        schema_version = stored_item.rule_results_schema_version

    assert schema_version == "rule-results@2"
    assert len(rule_results) == 1
    result = rule_results[0]
    assert result["criterion_code"] == "D01"
    assert result["direction"] == "deduct"
    assert result["effect_type"] == "score"
    assert result["rule_code"].startswith("manual.d01.deduct.")

    node = next(node for node in plan["nodes"] if node["criterion_code"] == "D01")
    rule_snapshot = node["atomic_rule_snapshot"]
    assert rule_snapshot["judge_type"] == "deterministic"
    assert rule_snapshot["checker_key"] == "thesis.legacy_required_fields.v1"
