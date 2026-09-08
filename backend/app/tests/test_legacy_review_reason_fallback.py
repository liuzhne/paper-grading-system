"""历史 run 的原因显示回退（前端 v2 计划 §5-B）。

`review_reasons` 是 0025 才加的列。在它之前完成的评分，这一列是空的——复核
队列会显示一行标着「需要确认」却**不给任何原因**，复核者无从判断该看什么。

§5-B 的要求是「新列可空，按已有 issue/任务数据提供显示回退；**不为补文案重评
历史**」。所以回退只能用评分项上已有的数据推导，不重新调用模型、不改写历史行。

推不出来时必须说「原因未记录」，而不是挑一条看起来合理的凑上去——编一个原因
比不给原因更糟，复核者会照着那个不存在的线索去核对。
"""

import pytest

from backend.app.db import models
from backend.app.services.batches import review_queue


def _legacy_item(client, **fields):
    """造一条 0025 之前形态的评分项：需要复核，但没有结构化原因。"""
    with client.session_factory() as session:
        rubric = models.Rubric(name="legacy rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        criterion = models.RubricCriterion(
            rubric_id=rubric.id, code="T01", name="选题与意义", max_score=10
        )
        session.add(criterion)
        batch = models.GradingBatch(
            name="legacy batch", rubric_id=rubric.id, status="scored"
        )
        session.add(batch)
        session.flush()
        paper = models.Paper(
            batch_id=batch.id, file_name="p.docx", file_path="p.docx", status="parsed"
        )
        session.add(paper)
        session.flush()
        run = models.ScoringRun(paper_id=paper.id, rubric_id=rubric.id, status="scored")
        session.add(run)
        session.flush()
        defaults = {
            "scoring_run_id": run.id,
            "criterion_id": criterion.id,
            "max_score": 10,
            "ai_score": 6,
            "final_score": 6,
            "evidence_sufficient": True,
            "reason": "历史评分理由",
            "deductions": [],
            "deduction_items": [],
            "evidence": [],
            "confidence": 0.9,
            "need_manual_review": True,
            # 关键：历史行没有这两列的值。
            "review_reasons": None,
            "review_reason": None,
        }
        defaults.update(fields)
        session.add(models.ScoreItem(**defaults))
        session.commit()

        payload = review_queue.build_review_queue(session, batch)

    ordinary = [r for r in payload["entries"] if r["queue_type"] == "ordinary"]
    assert ordinary, payload
    return ordinary[0]


def test_low_confidence_is_recovered_from_the_stored_score(client):
    entry = _legacy_item(client, confidence=0.4)

    codes = [reason["code"] for reason in entry["review_reasons"]]
    assert "low_confidence" in codes
    assert entry["review_reason"]


def test_missing_confidence_is_recovered_as_unavailable_not_zero(client):
    entry = _legacy_item(client, confidence=None)

    codes = [reason["code"] for reason in entry["review_reasons"]]
    assert "confidence_unavailable" in codes
    assert "low_confidence" not in codes


def test_insufficient_evidence_is_recovered(client):
    entry = _legacy_item(client, evidence_sufficient=False)

    codes = [reason["code"] for reason in entry["review_reasons"]]
    assert "evidence_insufficient" in codes


def test_old_deterministic_notes_are_recovered_from_deduction_items(client):
    """旧管线把这两句确定性文案追加进了 `deduction_items`，那就是「已有 issue 数据」。"""
    entry = _legacy_item(
        client,
        deduction_items=[
            {"points": 0, "reason": "部分证据引用未通过原文校验。"},
        ],
    )

    codes = [reason["code"] for reason in entry["review_reasons"]]
    assert "evidence_verification_failed" in codes


def test_nothing_derivable_says_so_instead_of_inventing_a_reason(client):
    """编一个原因比不给原因更糟：复核者会照着那个不存在的线索去核对。"""
    entry = _legacy_item(client, confidence=0.95, evidence_sufficient=True)

    reasons = entry["review_reasons"]
    assert [r["code"] for r in reasons] == ["reason_not_recorded"]
    assert "未记录" in reasons[0]["message"]


def test_items_not_flagged_for_review_get_no_fabricated_reasons():
    """没被标记复核的项不该凭空长出原因。

    直接验证函数：这类项根本不进复核队列，走队列断言不到它。
    """
    from backend.app.services.scoring.review_reasons import recover_for_legacy_item

    class _Item:
        need_manual_review = False
        confidence = 0.4
        evidence_sufficient = True
        deduction_items = []

    assert recover_for_legacy_item(_Item()) == []


def test_stored_reasons_win_over_the_fallback(client):
    """回退只在列为空时生效，绝不覆盖已经记录下来的原因。"""
    stored = [
        {
            "source": "deterministic",
            "code": "evidence_insufficient",
            "message": "证据不足以支撑该项给分，需人工核对原文。",
            "rule_code": None,
        }
    ]
    entry = _legacy_item(
        client, review_reasons=stored, review_reason=stored[0]["message"], confidence=0.1
    )

    assert entry["review_reasons"] == stored


@pytest.mark.parametrize("source_field", ["source", "code", "message", "rule_code"])
def test_fallback_entries_keep_the_same_shape(client, source_field):
    """回退产出的条目必须和正常管线同形，否则前端要写两套渲染。"""
    entry = _legacy_item(client, confidence=0.3)

    for reason in entry["review_reasons"]:
        assert source_field in reason
