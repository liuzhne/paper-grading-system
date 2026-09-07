"""当前结果选择器（前端 v2 计划 §5-B）。

队列、KPI、复核统计、分布图与导出预检必须共用同一套「哪些 run 算数」的判据，
否则同一批次在不同页面上会显示互相矛盾的数字。

核心约束：
- 有批任务时以当前 generation 的材料范围为准；新 generation 尚未产出结果的
  材料标为待评，**不能用上一轮的旧 run 冒充新结果**。
- 无批任务的历史/单篇评分按确定的 `(created_at, id)` 排序取最新。
- 选择集合返回 revision，供写操作做前置条件检查。
"""

from datetime import datetime, timedelta

from backend.app.db import models
from backend.app.services.batches import results


def _seed(session, *, papers=1):
    rubric = models.Rubric(name="sel rubric", version="v1", total_score=100)
    session.add(rubric)
    session.flush()
    batch = models.GradingBatch(name="sel batch", rubric_id=rubric.id, status="draft")
    session.add(batch)
    session.flush()
    made = []
    for index in range(papers):
        paper = models.Paper(
            batch_id=batch.id,
            file_name="p%d.docx" % index,
            file_path="p%d.docx" % index,
            status="parsed",
        )
        session.add(paper)
        made.append(paper)
    session.flush()
    return batch, made


def _run(session, paper, *, status="scored", created_at=None, score=None):
    run = models.ScoringRun(
        paper_id=paper.id,
        rubric_id=paper.batch.rubric_id,
        status=status,
        final_total_score=score,
        created_at=created_at or datetime(2026, 9, 1, 12, 0, 0),
    )
    session.add(run)
    session.flush()
    return run


# ---------------------------------------------------------------- 基本选择


def test_paper_without_any_run_is_pending(client):
    with client.session_factory() as session:
        batch, papers = _seed(session)

        selection = results.select_current_results(session, batch)

        assert selection.run_id_for(papers[0].id) is None
        assert selection.pending_count == 1
        assert selection.scored_count == 0


def test_latest_run_wins_by_created_at_then_id(client):
    """同一时刻的两个 run 必须有确定顺序，否则统计会在两次请求间跳变。"""
    with client.session_factory() as session:
        batch, papers = _seed(session)
        moment = datetime(2026, 9, 1, 12, 0, 0)
        first = _run(session, papers[0], created_at=moment)
        second = _run(session, papers[0], created_at=moment)
        expected = max([first.id, second.id])

        selection = results.select_current_results(session, batch)

        assert selection.run_id_for(papers[0].id) == expected


def test_newer_run_supersedes_older(client):
    with client.session_factory() as session:
        batch, papers = _seed(session)
        _run(session, papers[0], created_at=datetime(2026, 9, 1, 10, 0, 0))
        newer = _run(session, papers[0], created_at=datetime(2026, 9, 2, 10, 0, 0))

        selection = results.select_current_results(session, batch)

        assert selection.run_id_for(papers[0].id) == newer.id


# ---------------------------------------------------------------- generation


def _job(session, batch, *, generation, status="running"):
    job = models.BatchScoringJob(
        grading_batch_id=batch.id,
        generation=generation,
        status=status,
        observation_policy={},
        observation_policy_hash="0" * 64,
    )
    session.add(job)
    session.flush()
    return job


def test_new_generation_marks_unfinished_papers_pending_not_stale_scored(client):
    """重评开始后，尚未出新结果的材料是「待评」，不能显示上一轮的分数。"""
    with client.session_factory() as session:
        batch, papers = _seed(session, papers=2)
        _job(session, batch, generation=1, status="completed")
        old_a = _run(session, papers[0], created_at=datetime(2026, 9, 1, 10, 0, 0))
        _run(session, papers[1], created_at=datetime(2026, 9, 1, 10, 0, 0))

        # 第二轮开始，只有第一份出了新结果。
        job2 = _job(session, batch, generation=2, status="running")
        session.add(
            models.BatchScoringItem(
                job_id=job2.id, paper_id=papers[0].id, status="succeeded"
            )
        )
        session.add(
            models.BatchScoringItem(
                job_id=job2.id, paper_id=papers[1].id, status="pending"
            )
        )
        new_a = _run(session, papers[0], created_at=datetime(2026, 9, 3, 10, 0, 0))
        session.flush()

        selection = results.select_current_results(session, batch)

        assert selection.generation == 2
        assert selection.run_id_for(papers[0].id) == new_a.id
        assert selection.run_id_for(papers[0].id) != old_a.id
        # 第二份还没跑完：待评，而不是沿用第一轮的分数。
        assert selection.run_id_for(papers[1].id) is None
        assert selection.pending_count == 1


def test_selection_scope_follows_the_current_generation_items(client):
    """当前 generation 的材料范围决定分母，不是批次里所有材料。"""
    with client.session_factory() as session:
        batch, papers = _seed(session, papers=3)
        job = _job(session, batch, generation=1)
        # 只对前两份重评。
        for paper in papers[:2]:
            session.add(
                models.BatchScoringItem(
                    job_id=job.id, paper_id=paper.id, status="pending"
                )
            )
        session.flush()

        selection = results.select_current_results(session, batch)

        assert selection.total_count == 2
        assert papers[2].id not in selection.paper_ids


# ---------------------------------------------------------------- revision


def test_revision_is_stable_for_an_unchanged_selection(client):
    with client.session_factory() as session:
        batch, papers = _seed(session)
        _run(session, papers[0])

        first = results.select_current_results(session, batch).revision
        second = results.select_current_results(session, batch).revision

        assert first == second


def test_revision_changes_when_a_new_run_supersedes(client):
    with client.session_factory() as session:
        batch, papers = _seed(session)
        _run(session, papers[0], created_at=datetime(2026, 9, 1, 10, 0, 0))
        before = results.select_current_results(session, batch).revision

        _run(session, papers[0], created_at=datetime(2026, 9, 2, 10, 0, 0))
        after = results.select_current_results(session, batch).revision

        assert before != after


def test_revision_is_not_a_raw_identifier_dump(client):
    """revision 是稳定摘要，不应把 run/paper id 直接暴露给客户端。"""
    with client.session_factory() as session:
        batch, papers = _seed(session)
        run = _run(session, papers[0])

        revision = results.select_current_results(session, batch).revision

        assert run.id not in revision
        assert papers[0].id not in revision
        assert len(revision) >= 16


# ---------------------------------------------------------------- 计数


def test_counts_split_scored_failed_and_pending(client):
    with client.session_factory() as session:
        batch, papers = _seed(session, papers=3)
        _run(session, papers[0], status="scored")
        _run(session, papers[1], status="failed")
        # 第三份没有 run。

        selection = results.select_current_results(session, batch)

        assert selection.total_count == 3
        assert selection.scored_count == 1
        assert selection.failed_count == 1
        assert selection.pending_count == 1


def test_reviewed_runs_count_as_scored(client):
    with client.session_factory() as session:
        batch, papers = _seed(session)
        _run(session, papers[0], status="reviewed")

        selection = results.select_current_results(session, batch)

        assert selection.scored_count == 1
        assert selection.reviewed_count == 1


def test_empty_batch_reports_zeroes_not_a_fabricated_percentage(client):
    with client.session_factory() as session:
        batch, _ = _seed(session, papers=0)

        selection = results.select_current_results(session, batch)

        assert selection.total_count == 0
        assert selection.scored_count == 0
        assert selection.completion_ratio is None, "空批次不应编造百分比"
