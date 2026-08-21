from io import BytesIO

from docx import Document

from backend.app.db import models
from backend.app.tests.conftest import publish_rubric_via_api

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


def test_rule_deduction_and_unrelated_finding_have_separate_audit_trails(client):
    # 冻结规则授权“负责人字段缺失 -2”；文档另外会产生一条
    # figure_ref_missing 报告 finding，二者的证据链不得混同。
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
                "scoring_mode": "deductive",
                "dimension": "规范性",
                "deduction_rules_structured": [
                    {
                        "match": "负责人字段缺失",
                        "points": 2,
                        "reason": "负责人字段缺失",
                        "source": "manual",
                        "checker_key": "thesis.legacy_required_fields.v1",
                        "checker_params": {
                            "criterion_code": "D1",
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
    with client.session_factory() as db:
        stored = db.get(models.ScoreItem, item["id"])
        assert stored.rule_results_schema_version == "rule-results@2"
        rule_results = stored.rule_results
    assert len(rule_results) == 1
    result = rule_results[0]
    assert result["criterion_code"] == "D1"
    assert result["direction"] == "deduct"
    assert result["status"] == "triggered"
    assert result["calculated_effect"] == "-2"
    assert len(result["score_contributions"]) == 1
    contribution = result["score_contributions"][0]
    assert contribution["kind"] == "deduction"
    assert contribution["amount"] == "-2"
    assert contribution["occurrence_id"] in {
        occurrence["occurrence_id"] for occurrence in result["occurrences"]
    }
    assert float(item["final_score"]) == 8.0

    # 冻结 AtomicRule 的 occurrence/contribution 是权威扣分轨迹。图引用
    # finding 仍作为报告建议，因 finding_code/locator 不匹配而不得伪标消费。
    coherence = run.json()["coherence_findings"]
    assert any(f["kind"] == "figure_ref_missing" for f in coherence)
    assert all("deducted_by" not in f for f in coherence)


def test_criterion_without_dimension_leaves_findings_advisory(client):
    # 未显式启用（无 dimension/规则）的评分项：findings 仅进报告，不自动扣分。
    rubric = {
        "name": "仅报告不扣",
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
