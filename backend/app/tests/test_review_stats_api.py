"""复核统计、时间线与完成复核（前端 v2 计划 §5-B、§5-C、§6）。

统计口径上最容易做错的三处：

- **已确认数按「当前评分项的最后一次有效确认」算**，不是按 ReviewLog 条数。
  同一项被改两次仍然只是一项已确认。
- **平均调整幅度只纳入有 AI 分的人工调整项**，并公开分母；否则采纳系统给分
  （差值为 0）会把平均值稀释成毫无意义的数字。
- **完成复核要求普通待确认为 0 且阻塞为 0**，且结果集合未变。
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


def _seed(client, *, items, status="scored"):
    """items: [(need_review, ai_score, final_score)]"""
    with client.session_factory() as session:
        rubric = models.Rubric(name="s rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(name="s batch", rubric_id=rubric.id, status=status)
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
        for index, (need_review, ai, final) in enumerate(items):
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
                ai_score=ai,
                final_score=final,
                evidence_sufficient=True,
                reason="r",
                deductions=[],
                deduction_items=[],
                evidence=[],
                confidence=0.5,
                need_manual_review=need_review,
                review_reasons=LOW if need_review else None,
            )
            session.add(item)
            session.flush()
            ids.append(item.id)
        session.commit()
        return batch.id, run.id, ids


def _log(client, run_id, item_id, before, after, reason="人工调整"):
    with client.session_factory() as session:
        session.add(
            models.ReviewLog(
                scoring_run_id=run_id,
                score_item_id=item_id,
                reviewer_id="u1",
                before_score=before,
                after_score=after,
                reason=reason,
            )
        )
        session.commit()


# ---------------------------------------------------------------- stats


def test_stats_report_pending_and_confirmed_counts(client):
    batch_id, _, _ = _seed(
        client, items=[(True, 21, 21), (False, 20, 20), (False, 18, 22)]
    )

    body = client.get(f"/api/batches/{batch_id}/review-stats").json()

    assert body["ordinary_pending"] == 1
    assert body["confirmed_items"] == 2


def test_repeated_edits_count_as_one_confirmed_item(client):
    """同一项改两次仍是一项已确认，不能按日志条数累加。"""
    batch_id, run_id, ids = _seed(client, items=[(False, 18, 22)])
    _log(client, run_id, ids[0], 18, 20)
    _log(client, run_id, ids[0], 20, 22)

    body = client.get(f"/api/batches/{batch_id}/review-stats").json()

    assert body["confirmed_items"] == 1
    assert body["adjusted_items"] == 1


def test_accepted_and_adjusted_are_counted_separately(client):
    batch_id, _, _ = _seed(client, items=[(False, 20, 20), (False, 18, 22)])

    body = client.get(f"/api/batches/{batch_id}/review-stats").json()

    assert body["accepted_items"] == 1
    assert body["adjusted_items"] == 1


def test_average_adjustment_excludes_accepted_items_and_publishes_its_denominator(
    client,
):
    """采纳项差值为 0，混进来会把平均调整幅度稀释成没有意义的数字。"""
    batch_id, _, _ = _seed(
        client, items=[(False, 20, 20), (False, 18, 22), (False, 10, 12)]
    )

    body = client.get(f"/api/batches/{batch_id}/review-stats").json()

    assert body["adjustment_sample_size"] == 2
    assert body["average_adjustment"] == 3.0


def test_average_adjustment_is_null_when_no_adjustments_exist(client):
    batch_id, _, _ = _seed(client, items=[(False, 20, 20)])

    body = client.get(f"/api/batches/{batch_id}/review-stats").json()

    assert body["average_adjustment"] is None
    assert body["adjustment_sample_size"] == 0


def test_items_without_an_ai_score_are_excluded_from_the_average(client):
    """没有 AI 分就没有「调整幅度」可言。

    这种形态只出现在 Core 的 blocked/invalid 评分项上：schema 的
    ck_score_items_aggregation_state 要求 aggregation 为空时必须有 ai_score，
    所以要按真实形态造数，而不是伪造一个数据库不接受的行。
    """
    batch_id, run_id, _ = _seed(client, items=[(False, 18, 22)])
    with client.session_factory() as session:
        run = session.get(models.ScoringRun, run_id)
        criterion = models.RubricCriterion(
            rubric_id=run.rubric_id, code="CX", name="被阻断项", max_score=25, weight=1
        )
        session.add(criterion)
        session.flush()
        session.add(
            models.ScoreItem(
                scoring_run_id=run_id,
                criterion_id=criterion.id,
                max_score=25,
                ai_score=None,
                final_score=15,
                evidence_sufficient=False,
                reason="blocked",
                deductions=[],
                deduction_items=[],
                evidence=[],
                need_manual_review=False,
                aggregation={"blocked": True},
                aggregation_schema_version="agg@1",
                auto_score_status="blocked",
            )
        )
        session.commit()

    body = client.get(f"/api/batches/{batch_id}/review-stats").json()

    assert body["adjustment_sample_size"] == 1


# ---------------------------------------------------------------- timeline


def test_timeline_returns_review_logs_newest_first(client):
    batch_id, run_id, ids = _seed(client, items=[(False, 18, 22)])
    _log(client, run_id, ids[0], 18, 20, reason="第一次")
    _log(client, run_id, ids[0], 20, 22, reason="第二次")

    body = client.get(f"/api/batches/{batch_id}/review-timeline").json()

    assert len(body["entries"]) == 2
    assert body["entries"][0]["reason"] == "第二次"


def test_timeline_entries_identify_the_item_and_scores(client):
    batch_id, run_id, ids = _seed(client, items=[(False, 18, 22)])
    _log(client, run_id, ids[0], 18, 22)

    entry = client.get(f"/api/batches/{batch_id}/review-timeline").json()["entries"][0]

    assert entry["score_item_id"] == ids[0]
    assert entry["before_score"] == 18.0
    assert entry["after_score"] == 22.0
    assert entry["criterion_code"] == "C01"


def test_timeline_is_bounded(client):
    batch_id, run_id, ids = _seed(client, items=[(False, 18, 22)])
    for _ in range(5):
        _log(client, run_id, ids[0], 18, 22)

    body = client.get(f"/api/batches/{batch_id}/review-timeline?limit=2").json()

    assert len(body["entries"]) == 2
    assert body["next_cursor"] is not None


# ---------------------------------------------------------------- complete


def test_complete_review_transitions_the_batch(client):
    batch_id, _, _ = _seed(client, items=[(False, 20, 20)])
    revision = client.get(f"/api/batches/{batch_id}/review-queue").json()[
        "result_revision"
    ]

    response = client.post(
        f"/api/batches/{batch_id}/complete-review",
        json={"result_revision": revision},
    )

    assert response.status_code == 200
    assert client.get(f"/api/batches/{batch_id}").json()["status"] == "reviewed"


def test_complete_review_refuses_while_ordinary_items_are_pending(client):
    batch_id, _, _ = _seed(client, items=[(True, 21, 21)])
    revision = client.get(f"/api/batches/{batch_id}/review-queue").json()[
        "result_revision"
    ]

    response = client.post(
        f"/api/batches/{batch_id}/complete-review",
        json={"result_revision": revision},
    )

    assert response.status_code == 409
    assert client.get(f"/api/batches/{batch_id}").json()["status"] == "scored"


def test_complete_review_refuses_on_a_stale_result_revision(client):
    batch_id, _, _ = _seed(client, items=[(False, 20, 20)])

    response = client.post(
        f"/api/batches/{batch_id}/complete-review",
        json={"result_revision": "0" * 64},
    )

    assert response.status_code == 409


def test_complete_review_refuses_from_a_stage_that_has_no_results(client):
    batch_id, _, _ = _seed(client, items=[(False, 20, 20)], status="draft")
    revision = client.get(f"/api/batches/{batch_id}/review-queue").json()[
        "result_revision"
    ]

    response = client.post(
        f"/api/batches/{batch_id}/complete-review",
        json={"result_revision": revision},
    )

    assert response.status_code == 409
