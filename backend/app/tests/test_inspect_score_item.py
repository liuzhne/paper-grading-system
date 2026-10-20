"""只读诊断：一个评分项能不能改分、为什么（RUNBOOK「评分工作区里改分点保存没反应」）。"""

import json

from sqlalchemy import func
from sqlalchemy import select

from backend.app.db import models
from backend.app.scripts import inspect_score_item as script
from backend.app.tests.conftest import make_sample_docx
from backend.app.tests.conftest import publish_rubric_via_api


def _criterion(code, name):
    return {
        "code": code,
        "name": name,
        "max_score": 15,
        "criterion_type": "deterministic",
        "evidence_hints": [name],
        "deduction_rules": ["%s不足扣分" % name],
        "scoring_mode": "deductive",
        "deduction_rules_structured": [
            {
                "match": "%s不足" % name,
                "points": 15,
                "reason": "%s不足扣分" % name,
                "checker_key": "thesis.legacy_required_fields.v1",
                "checker_params": {"criterion_code": code, "applies_to": "global"},
            }
        ],
    }


def _scored_items(client):
    rubric = client.post(
        "/api/rubrics",
        json={
            "name": "诊断用评分标准",
            "version": "v1",
            "total_score": 30,
            "criteria": [_criterion("C01", "研究方法"), _criterion("C02", "参考文献")],
        },
    )
    assert rubric.status_code == 200, rubric.text
    publish_rubric_via_api(client, rubric.json()["id"])
    batch = client.post("/api/batches", json={"name": "诊断批次", "rubric_id": rubric.json()["id"]})
    assert batch.status_code == 200, batch.text
    paper = client.post(
        "/api/papers/upload",
        data={"batch_id": batch.json()["id"]},
        files={
            "file": (
                "sample.docx",
                make_sample_docx().getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert paper.status_code == 200, paper.text
    scored = client.post("/api/papers/%s/score" % paper.json()["id"])
    assert scored.status_code == 200, scored.text
    run_id = scored.json()["id"]
    return client.get("/api/scoring-runs/%s/items" % run_id).json()


def test_a_normal_item_can_be_overridden(client):
    items = _scored_items(client)

    with client.session_factory() as db:
        report = script.inspect_score_item(db, items[0]["id"])

    assert report["found"] is True
    assert report["item"]["auto_score_status"] == "calculated"
    assert report["override_allowed"] is True
    assert set(report["checks"].values()) == {"ok"}
    assert "规则上可以改分" in report["next_step"]


def test_an_invalid_item_explains_the_rejection_and_the_retry(client):
    items = _scored_items(client)
    with client.session_factory() as db:
        item = db.get(models.ScoreItem, items[0]["id"])
        # 与生产那份评分一样：模型没给出有效结论，分数为空（约束要求 invalid 时 ai_score 为空）。
        item.auto_score_status = "invalid"
        item.ai_score = None
        item.final_score = None
        if item.aggregation_schema_version is None:
            item.aggregation_schema_version = "criterion-aggregation@2"
            item.aggregation = {"raw": None, "max": str(item.max_score), "weight": None, "contribution": None}
        db.commit()

    with client.session_factory() as db:
        report = script.inspect_score_item(db, items[0]["id"])

    assert report["override_allowed"] is False
    assert report["checks"]["ordinary_override"].startswith("rejected: ordinary override cannot resolve")
    assert "重试失败项" in report["next_step"]
    assert report["run"]["unresolved_items"] == 1
    # 这一项的接口也确实会被拒（诊断与接口用同一道检查）。
    assert client.patch(
        "/api/score-items/%s" % items[0]["id"], json={"final_score": 10, "reason": "复核"}
    ).status_code == 400


def test_the_script_is_read_only_and_prints_no_student_identity(client, monkeypatch, capsys):
    items = _scored_items(client)
    with client.session_factory() as db:
        paper = db.scalars(select(models.Paper)).one()
        paper.student_name = "张三"
        paper.student_id = "20260001"
        db.commit()
        before = db.scalar(select(func.count(models.ReviewLog.id)))

    monkeypatch.setattr("backend.app.db.session.SessionLocal", client.session_factory)

    assert script.main([items[0]["id"]]) == 0
    printed = capsys.readouterr().out
    report = json.loads(printed)
    assert report["item"]["id"] == items[0]["id"]
    assert "张三" not in printed and "20260001" not in printed

    with client.session_factory() as db:
        assert db.scalar(select(func.count(models.ReviewLog.id))) == before


def test_unknown_item_and_bad_usage(client, monkeypatch, capsys):
    monkeypatch.setattr("backend.app.db.session.SessionLocal", client.session_factory)

    assert script.main(["no-such-item"]) == 1
    assert json.loads(capsys.readouterr().out)["found"] is False
    assert script.main([]) == 2
    assert "用法" in capsys.readouterr().err
