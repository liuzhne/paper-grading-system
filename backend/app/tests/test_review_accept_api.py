"""批量采纳合同（前端 v2 计划 §5-B）。

设计稿的按钮写「全部采纳系统给分」，但服务端**不做无界全批扫描**：请求必须
携带用户预览过并提交的有限集合，按钮相应改为「采纳本页可采纳项」。理由是
一次点击不该在服务端展开成对成百上千条记录的隐式写入。

三条硬约束：

1. **整次要么全写、要么全不写。** 任一项过期或不合格返回 409，不留下写了
   一半的状态。
2. **不覆盖已被人工修改的分数。** 采纳的语义是「确认系统给分」，不是「把
   别人改过的分改回去」。
3. **幂等。** 同键同载荷返回原结果；同键不同载荷是冲突，不是覆盖。
"""

from backend.app.db import models


LOW = [
    {
        "source": "deterministic",
        "code": "low_confidence",
        "message": "置信度偏低",
        "rule_code": None,
    }
]


def _seed(client, *, count=2, need_review=True):
    with client.session_factory() as session:
        rubric = models.Rubric(name="a rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(
            name="a batch", rubric_id=rubric.id, status="scored"
        )
        session.add(batch)
        session.flush()
        paper = models.Paper(
            batch_id=batch.id, file_name="p.pdf", file_path="p.pdf", status="parsed"
        )
        session.add(paper)
        session.flush()
        run = models.ScoringRun(paper_id=paper.id, rubric_id=rubric.id, status="scored")
        session.add(run)
        session.flush()
        ids = []
        for index in range(count):
            criterion = models.RubricCriterion(
                rubric_id=rubric.id,
                code="C%02d" % (index + 1),
                name="项 %d" % index,
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
                confidence=0.5,
                need_manual_review=need_review,
                review_reasons=LOW,
            )
            session.add(item)
            session.flush()
            ids.append(item.id)
        session.commit()
        return batch.id, ids


def _payload(client, batch_id, item_ids, *, key="idem-1"):
    queue = client.get(f"/api/batches/{batch_id}/review-queue").json()
    revisions = {e["score_item_id"]: e["review_revision"] for e in queue["entries"]}
    return {
        "result_revision": queue["result_revision"],
        "idempotency_key": key,
        "reason": "采纳系统给分",
        "items": [
            {"score_item_id": item_id, "review_revision": revisions.get(item_id, 1)}
            for item_id in item_ids
        ],
    }


def test_accepting_a_previewed_set_confirms_those_items(client):
    batch_id, ids = _seed(client, count=2)

    response = client.post(
        f"/api/batches/{batch_id}/review-queue/accept",
        json=_payload(client, batch_id, ids),
    )

    assert response.status_code == 200
    assert response.json()["accepted_count"] == 2
    assert client.get(f"/api/batches/{batch_id}/review-queue").json()["entries"] == []


def test_acceptance_writes_a_review_log_per_item(client):
    batch_id, ids = _seed(client, count=2)

    client.post(
        f"/api/batches/{batch_id}/review-queue/accept",
        json=_payload(client, batch_id, ids),
    )

    with client.session_factory() as session:
        logs = session.scalars(models.ReviewLog.__table__.select()).all()
    assert len(logs) == 2


def test_a_stale_item_revision_rejects_the_whole_request(client):
    """整次要么全写、要么全不写，不留下写了一半的状态。"""
    batch_id, ids = _seed(client, count=2)
    payload = _payload(client, batch_id, ids)
    payload["items"][1]["review_revision"] = 999

    response = client.post(
        f"/api/batches/{batch_id}/review-queue/accept", json=payload
    )

    assert response.status_code == 409
    assert len(client.get(f"/api/batches/{batch_id}/review-queue").json()["entries"]) == 2


def test_a_stale_result_revision_is_rejected(client):
    batch_id, ids = _seed(client, count=1)
    payload = _payload(client, batch_id, ids)
    payload["result_revision"] = "0" * 64

    response = client.post(
        f"/api/batches/{batch_id}/review-queue/accept", json=payload
    )

    assert response.status_code == 409


def test_an_item_already_changed_by_a_human_is_never_overwritten(client):
    """采纳是「确认系统给分」，不是「把别人改过的分改回去」。"""
    batch_id, ids = _seed(client, count=1)
    client.patch(
        f"/api/score-items/{ids[0]}", json={"final_score": 24, "reason": "人工上调"}
    )

    payload = _payload(client, batch_id, ids)
    response = client.post(
        f"/api/batches/{batch_id}/review-queue/accept", json=payload
    )

    assert response.status_code == 409
    with client.session_factory() as session:
        item = session.get(models.ScoreItem, ids[0])
        assert float(item.final_score) == 24.0


def test_same_key_and_payload_returns_the_original_receipt(client):
    batch_id, ids = _seed(client, count=1)
    payload = _payload(client, batch_id, ids)

    first = client.post(f"/api/batches/{batch_id}/review-queue/accept", json=payload)
    second = client.post(f"/api/batches/{batch_id}/review-queue/accept", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["accepted_count"] == first.json()["accepted_count"]
    assert second.json()["replayed"] is True
    with client.session_factory() as session:
        logs = session.scalars(models.ReviewLog.__table__.select()).all()
    assert len(logs) == 1, "重放不得重复写入复核记录"


def test_same_key_different_payload_is_a_conflict_not_an_overwrite(client):
    batch_id, ids = _seed(client, count=2)
    first_payload = _payload(client, batch_id, ids[:1])
    client.post(f"/api/batches/{batch_id}/review-queue/accept", json=first_payload)

    second_payload = _payload(client, batch_id, ids[1:], key="idem-1")
    response = client.post(
        f"/api/batches/{batch_id}/review-queue/accept", json=second_payload
    )

    assert response.status_code == 409


def test_the_batch_is_bounded(client):
    batch_id, ids = _seed(client, count=1)
    payload = _payload(client, batch_id, ids)
    payload["items"] = payload["items"] * 200

    response = client.post(
        f"/api/batches/{batch_id}/review-queue/accept", json=payload
    )

    assert response.status_code == 422


def test_an_empty_set_is_rejected(client):
    batch_id, ids = _seed(client, count=1)
    payload = _payload(client, batch_id, ids)
    payload["items"] = []

    response = client.post(
        f"/api/batches/{batch_id}/review-queue/accept", json=payload
    )

    assert response.status_code == 422


def test_archived_batches_reject_acceptance(client):
    batch_id, ids = _seed(client, count=1)
    payload = _payload(client, batch_id, ids)
    with client.session_factory() as session:
        batch = session.get(models.GradingBatch, batch_id)
        batch.status = "archived"
        session.commit()

    response = client.post(
        f"/api/batches/{batch_id}/review-queue/accept", json=payload
    )

    assert response.status_code == 409
