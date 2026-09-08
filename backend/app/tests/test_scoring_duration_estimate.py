"""预计耗时（前端 v2 计划 决策 12、§5-D、§11）。

§11 把口径与冷启动列为未决，但把未决时的行为定死了：「**无可靠样本显示暂无
估计，不编造分钟数**」。所以这里只做一件事——有足够历史样本就给区间，没有就
明说没有。

§5-D 补了一条硬约束：「耗时是估计值，**不能用于评分租约或超时判定**」。它只进
展示，不参与任何调度决策；这也是为什么返回的是区间而不是一个精确秒数——一个
精确数字会被当成承诺。
"""

import datetime as dt

import pytest

from backend.app.db import models
from backend.app.services.batches.duration import estimate_scoring_duration


MIN_SAMPLES = 3


def _job(session, batch_id, *, seconds, status="completed", items=10):
    started = dt.datetime(2026, 9, 1, 10, 0, 0)
    session.add(
        models.BatchScoringJob(
            grading_batch_id=batch_id,
            generation=1,
            status=status,
            total_items=items,
            observation_policy={},
            observation_policy_hash="0" * 64,
            started_at=started,
            finished_at=started + dt.timedelta(seconds=seconds),
        )
    )


def _org_history(client, samples, *, status="completed"):
    with client.session_factory() as session:
        rubric = models.Rubric(name="dur rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        for index, seconds in enumerate(samples):
            batch = models.GradingBatch(
                name="dur batch %d" % index, rubric_id=rubric.id, status="scored"
            )
            session.add(batch)
            session.flush()
            _job(session, batch.id, seconds=seconds, status=status)
        session.commit()


def test_no_history_means_no_estimate(client):
    """冷启动不编造分钟数。"""
    with client.session_factory() as session:
        estimate = estimate_scoring_duration(session, organization_id=None, item_count=10)

    assert estimate["available"] is False
    assert estimate["seconds_per_item"] is None
    assert "暂无" in estimate["message"]


def test_too_few_samples_still_means_no_estimate(client):
    """两三个样本算出来的均值不比猜测可靠，标成「估计」会让人当真。"""
    _org_history(client, [100.0, 200.0])

    with client.session_factory() as session:
        estimate = estimate_scoring_duration(session, organization_id=None, item_count=10)

    assert estimate["available"] is False
    assert estimate["sample_count"] == 2


def test_enough_samples_produce_a_range(client):
    _org_history(client, [100.0, 110.0, 120.0, 130.0])

    with client.session_factory() as session:
        estimate = estimate_scoring_duration(session, organization_id=None, item_count=20)

    assert estimate["available"] is True
    assert estimate["sample_count"] == 4
    # 区间而不是精确秒数：一个精确数字会被当成承诺。
    assert estimate["low_seconds"] < estimate["high_seconds"]
    assert estimate["seconds_per_item"] == pytest.approx(11.5, abs=0.5)


def test_only_finished_jobs_count(client):
    """还在跑的任务没有结束时间，把它算进去等于用一个未知数做分母。"""
    _org_history(client, [100.0, 110.0, 120.0], status="running")

    with client.session_factory() as session:
        estimate = estimate_scoring_duration(session, organization_id=None, item_count=10)

    assert estimate["available"] is False


def test_zero_items_gives_no_estimate(client):
    _org_history(client, [100.0, 110.0, 120.0, 130.0])

    with client.session_factory() as session:
        estimate = estimate_scoring_duration(session, organization_id=None, item_count=0)

    assert estimate["available"] is False


def test_the_estimate_is_marked_as_display_only(client):
    """§5-D：不能用于评分租约或超时判定。合同里要写明，不能只写在文档里。"""
    _org_history(client, [100.0, 110.0, 120.0, 130.0])

    with client.session_factory() as session:
        estimate = estimate_scoring_duration(session, organization_id=None, item_count=10)

    assert estimate["display_only"] is True


def test_precheck_carries_the_estimate(client):
    """新建任务页在预检之后展示它（决策 12）。"""
    with client.session_factory() as session:
        rubric = models.Rubric(name="pc rubric", version="v1", total_score=100)
        session.add(rubric)
        session.commit()
        rubric_id = rubric.id

    batch = client.post(
        "/api/batches", json={"name": "pc batch", "rubric_id": rubric_id}
    ).json()
    with client.session_factory() as session:
        paper = models.Paper(
            batch_id=batch["id"],
            file_name="p.docx",
            file_path="p.docx",
            status="parsed",
        )
        session.add(paper)
        session.commit()
        paper_id = paper.id

    body = client.post(
        "/api/batches/%s/upload-precheck" % batch["id"],
        json={"paper_ids": [paper_id]},
    ).json()

    assert "duration_estimate" in body
    # 这个环境没有历史任务：明说暂无估计，不给一个编出来的分钟数。
    assert body["duration_estimate"]["available"] is False
    assert "暂无" in body["duration_estimate"]["message"]
