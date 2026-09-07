"""批次级复核队列（前端 v2 计划 §5-B、§6）。

统一队列同时包含两类工作：

- **普通确认**：``need_manual_review`` 的评分项，采纳或改分即可。
- **阻塞任务**：`/api/v2/manual-review-tasks` 的 open/claimed 任务，必须
  领取后凭冻结证据解决。阻塞未清空前，该 run 不得出现完整总分。

阻塞优先——它代表「结论本身还不成立」，比「模型没把握」更要紧。
"""

from backend.app.db import models


def _seed(client, *, items):
    """items: [(need_review, confidence, reasons)]"""
    with client.session_factory() as session:
        rubric = models.Rubric(name="q rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(
            name="q batch", rubric_id=rubric.id, status="scored_with_errors"
        )
        session.add(batch)
        session.flush()
        paper = models.Paper(
            batch_id=batch.id,
            file_name="p.pdf",
            file_path="p.pdf",
            status="parsed",
            student_id="SE-2026-014",
            student_name="林越",
        )
        session.add(paper)
        session.flush()
        run = models.ScoringRun(
            paper_id=paper.id, rubric_id=rubric.id, status="scored"
        )
        session.add(run)
        session.flush()
        made = []
        for index, (need_review, confidence, reasons) in enumerate(items):
            criterion = models.RubricCriterion(
                rubric_id=rubric.id,
                code="C%02d" % (index + 1),
                name="评分项 %d" % (index + 1),
                max_score=25,
                weight=1,
            )
            session.add(criterion)
            session.flush()
            item = models.ScoreItem(
                scoring_run_id=run.id,
                criterion_id=criterion.id,
                max_score=25,
                ai_score=21,
                final_score=21,
                evidence_sufficient=True,
                reason="r",
                deductions=[],
                deduction_items=[],
                evidence=[],
                confidence=confidence,
                need_manual_review=need_review,
                review_reasons=reasons,
                review_reason=" ".join(r["message"] for r in (reasons or [])) or None,
            )
            session.add(item)
            session.flush()
            made.append(item.id)
        session.commit()
        return batch.id, run.id, made


LOW = [{"source": "deterministic", "code": "low_confidence", "message": "置信度偏低", "rule_code": None}]


def test_queue_lists_only_items_needing_confirmation(client):
    batch_id, _, made = _seed(
        client, items=[(True, 0.5, LOW), (False, 0.95, None), (True, 0.6, LOW)]
    )

    body = client.get(f"/api/batches/{batch_id}/review-queue").json()

    assert len(body["entries"]) == 2
    assert {e["score_item_id"] for e in body["entries"]} == {made[0], made[2]}


def test_queue_entry_carries_the_reason_so_reviewers_know_why(client):
    batch_id, _, _ = _seed(client, items=[(True, 0.5, LOW)])

    entry = client.get(f"/api/batches/{batch_id}/review-queue").json()["entries"][0]

    assert entry["review_reasons"][0]["code"] == "low_confidence"
    assert entry["review_reason"]


def test_missing_confidence_is_shown_as_unavailable_not_zero(client):
    """Core 持久化不写 confidence。显示成 0 会让人以为模型毫无把握。"""
    batch_id, _, _ = _seed(client, items=[(True, None, LOW)])

    entry = client.get(f"/api/batches/{batch_id}/review-queue").json()["entries"][0]

    assert entry["confidence"] is None


def test_entry_identifies_the_material_and_criterion(client):
    batch_id, _, _ = _seed(client, items=[(True, 0.5, LOW)])

    entry = client.get(f"/api/batches/{batch_id}/review-queue").json()["entries"][0]

    assert entry["student_id"] == "SE-2026-014"
    assert entry["criterion_code"] == "C01"
    assert entry["criterion_name"] == "评分项 1"
    assert entry["ai_score"] == 21.0
    assert entry["max_score"] == 25.0


def test_entries_carry_revisions_for_optimistic_writes(client):
    batch_id, _, _ = _seed(client, items=[(True, 0.5, LOW)])

    body = client.get(f"/api/batches/{batch_id}/review-queue").json()

    assert isinstance(body["result_revision"], str)
    assert body["entries"][0]["review_revision"] >= 1


def test_queue_reports_acceptability_per_entry(client):
    """只有仍待普通确认、有有效 AI 分、无未解决阻塞的项才可批量采纳。"""
    batch_id, _, _ = _seed(client, items=[(True, 0.5, LOW)])

    entry = client.get(f"/api/batches/{batch_id}/review-queue").json()["entries"][0]

    assert entry["acceptable"] is True
    assert entry["queue_type"] == "ordinary"


def test_queue_is_paginated_with_a_bounded_default(client):
    batch_id, _, _ = _seed(client, items=[(True, 0.5, LOW)] * 5)

    body = client.get(f"/api/batches/{batch_id}/review-queue?limit=2").json()

    assert len(body["entries"]) == 2
    assert body["next_cursor"] is not None


def test_queue_rejects_an_unbounded_limit(client):
    batch_id, _, _ = _seed(client, items=[(True, 0.5, LOW)])

    assert (
        client.get(f"/api/batches/{batch_id}/review-queue?limit=100000").status_code
        == 422
    )


def test_queue_is_scoped_to_visible_batches(client):
    assert client.get("/api/batches/nope/review-queue").status_code == 404


def test_empty_queue_reports_zero_not_an_error(client):
    batch_id, _, _ = _seed(client, items=[(False, 0.95, None)])

    body = client.get(f"/api/batches/{batch_id}/review-queue").json()

    assert body["entries"] == []
    assert body["ordinary_pending"] == 0
    assert body["blocking_open"] == 0
