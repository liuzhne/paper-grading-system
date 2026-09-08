"""分数分布（前端 v2 计划 §6，阶段 1/6）。

§11 把**分桶策略**和**「上一批次」的定义**列为未决：rubric 满分可变，非论文
Profile 的量纲也不同，固定档位会把两个不可比的批次画在同一张图上。未决时的
处理办法是「不展示跨标准均分差，原始有效终分与缺结果数仍可显示」——这里就
只交付那一部分。

因此这个端点给的是**原始有效终分**与**缺结果计数**，不给分桶、不给与其它批次
的差值。等分桶策略定下来再加，不在此之前先编一套档位出来。

有效终分只取「当前结果选择」里的 run，与 KPI、复核队列、导出预检同源；否则
同一个批次在四个地方会显示四个数。终分缺失时回落 `ai_total_score`——与
`calibration/analytics` 一致，而不是把这份材料算成没有结果。
"""

import pytest

from sqlalchemy import select

from backend.app.db import models


def _batch_with_scores(client, scores, *, unscored=0):
    """按给定终分造材料；`unscored` 份材料没有任何 run。"""
    with client.session_factory() as session:
        rubric = models.Rubric(name="dist rubric", version="v1", total_score=100)
        session.add(rubric)
        session.commit()
        rubric_id = rubric.id

    batch = client.post(
        "/api/batches", json={"name": "dist batch", "rubric_id": rubric_id}
    ).json()

    with client.session_factory() as session:
        for index, total in enumerate(scores):
            paper = models.Paper(
                batch_id=batch["id"],
                file_name="p%d.docx" % index,
                file_path="p%d.docx" % index,
                status="parsed",
            )
            session.add(paper)
            session.flush()
            session.add(
                models.ScoringRun(
                    paper_id=paper.id,
                    rubric_id=rubric_id,
                    status="scored",
                    final_total_score=total,
                )
            )
        for index in range(unscored):
            session.add(
                models.Paper(
                    batch_id=batch["id"],
                    file_name="u%d.docx" % index,
                    file_path="u%d.docx" % index,
                    status="uploaded",
                )
            )
        session.commit()
    return batch


def test_returns_the_raw_valid_final_scores(client):
    batch = _batch_with_scores(client, [88.0, 72.5, 91.0])

    body = client.get("/api/batches/%s/score-distribution" % batch["id"]).json()

    assert sorted(body["scores"]) == [72.5, 88.0, 91.0]
    assert body["scored_count"] == 3
    assert body["max_score"] == 100.0


def test_counts_materials_without_results_separately(client):
    """缺结果不是 0 分。把它们并进分布会把平均分拉低成一个假数字。"""
    batch = _batch_with_scores(client, [90.0], unscored=2)

    body = client.get("/api/batches/%s/score-distribution" % batch["id"]).json()

    assert body["scores"] == [90.0]
    assert body["scored_count"] == 1
    assert body["without_results"] == 2


def test_does_not_invent_buckets_or_cross_batch_deltas(client):
    """分桶与「上一批次」都还没定（§11）。端点不能先自己编一套。"""
    batch = _batch_with_scores(client, [60.0, 80.0])

    body = client.get("/api/batches/%s/score-distribution" % batch["id"]).json()

    assert "buckets" not in body
    assert "previous_batch_delta" not in body
    # 未决状态要显式说明，否则前端无从判断「没有分桶」是缺陷还是设计。
    assert body["bucketing"] is None


def test_shares_the_result_revision_with_the_other_views(client):
    batch = _batch_with_scores(client, [70.0])

    distribution = client.get(
        "/api/batches/%s/score-distribution" % batch["id"]
    ).json()
    progress = client.get("/api/batches/%s/progress" % batch["id"]).json()

    assert distribution["result_revision"] == progress["result_revision"]


def test_empty_batch_reports_nothing_rather_than_zero(client):
    batch = _batch_with_scores(client, [])

    body = client.get("/api/batches/%s/score-distribution" % batch["id"]).json()

    assert body["scores"] == []
    assert body["scored_count"] == 0
    assert body["average"] is None


def test_average_is_over_scored_materials_only(client):
    batch = _batch_with_scores(client, [80.0, 90.0], unscored=2)

    body = client.get("/api/batches/%s/score-distribution" % batch["id"]).json()

    assert body["average"] == pytest.approx(85.0)


def test_unknown_batch_is_not_found(client):
    assert client.get("/api/batches/nope/score-distribution").status_code == 404


def test_blocking_count_is_reported_alongside_the_scores(client):
    """§5-D：分布「只包含有效且非空终分，另报未评/**阻塞**数量」。

    阻塞项的总分尚不成立。不单独报出来，读者会把一条完整的分布曲线当成这批
    的最终形态，而它其实还会变。
    """
    batch = _batch_with_scores(client, [70.0, 80.0])

    with client.session_factory() as session:
        run = session.scalars(select(models.ScoringRun)).first()
        session.add(
            models.ManualReviewTask(
                organization_id="00000000-0000-0000-0000-000000000002",
                scoring_run_id=run.id,
                criterion_code="T01",
                trigger_message="Provider 调用失败。",
                trigger_code="provider_error",
                status="open",
                priority=0,
            )
        )
        session.commit()

    body = client.get("/api/batches/%s/score-distribution" % batch["id"]).json()

    assert body["blocking_open"] == 1
    # 阻塞不改变已产出的终分，只是提示这张图还会变。
    assert body["scored_count"] == 2


def test_blocking_count_is_zero_when_nothing_is_blocked(client):
    batch = _batch_with_scores(client, [70.0])

    body = client.get("/api/batches/%s/score-distribution" % batch["id"]).json()

    assert body["blocking_open"] == 0
