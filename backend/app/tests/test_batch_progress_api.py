"""`GET /api/batches/{id}/progress`（前端 v2 计划 §5-D、§6）。

阶段与执行状态必须并行返回：一次 job 失败不代表批次阶段就是失败，
只看阶段字段会漏掉「任务已中断、等待恢复」这类情况。
"""

from datetime import datetime

from backend.app.db import models


def _seed_batch(client, *, papers=0, status="draft"):
    with client.session_factory() as session:
        rubric = models.Rubric(name="prog rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(
            name="prog batch", rubric_id=rubric.id, status=status
        )
        session.add(batch)
        session.flush()
        for index in range(papers):
            session.add(
                models.Paper(
                    batch_id=batch.id,
                    file_name="p%d.docx" % index,
                    file_path="p%d.docx" % index,
                    status="parsed",
                )
            )
        session.commit()
        return batch.id


def test_progress_reports_stage_and_counts(client):
    batch_id = _seed_batch(client, papers=3)

    body = client.get(f"/api/batches/{batch_id}/progress").json()

    assert body["stage"] == "draft"
    assert body["counts"]["total"] == 3
    assert body["counts"]["scored"] == 0
    assert body["counts"]["pending"] == 3


def test_progress_exposes_the_result_selection_revision(client):
    batch_id = _seed_batch(client, papers=1)

    body = client.get(f"/api/batches/{batch_id}/progress").json()

    assert isinstance(body["result_revision"], str)
    assert len(body["result_revision"]) >= 16


def test_progress_carries_state_version_for_optimistic_writes(client):
    batch_id = _seed_batch(client, papers=1)

    body = client.get(f"/api/batches/{batch_id}/progress").json()

    assert isinstance(body["state_version"], int)


def test_empty_batch_reports_null_ratio_not_a_fabricated_percentage(client):
    batch_id = _seed_batch(client, papers=0)

    body = client.get(f"/api/batches/{batch_id}/progress").json()

    assert body["counts"]["total"] == 0
    assert body["completion_ratio"] is None


def test_progress_reports_job_status_alongside_the_stage(client):
    """阶段字段不表达任务故障，job 状态必须单独给出。"""
    batch_id = _seed_batch(client, papers=1, status="scoring")
    with client.session_factory() as session:
        batch = session.get(models.GradingBatch, batch_id)
        paper = batch.papers[0]
        job = models.BatchScoringJob(
            grading_batch_id=batch_id,
            generation=1,
            status="failed",
            observation_policy={},
            observation_policy_hash="0" * 64,
        )
        session.add(job)
        session.flush()
        session.add(
            models.BatchScoringItem(
                job_id=job.id, paper_id=paper.id, status="failed"
            )
        )
        session.commit()

    body = client.get(f"/api/batches/{batch_id}/progress").json()

    assert body["stage"] == "scoring"
    assert body["job"]["status"] == "failed"
    assert body["job"]["generation"] == 1


def test_progress_without_any_job_reports_null_job(client):
    batch_id = _seed_batch(client, papers=1)

    body = client.get(f"/api/batches/{batch_id}/progress").json()

    assert body["job"] is None


def test_available_actions_reflect_the_stage_guard(client):
    """可执行动作由服务端给出，前端不自行推断转移合法性。"""
    draft = _seed_batch(client, papers=1, status="draft")
    reviewed = _seed_batch(client, papers=1, status="reviewed")

    draft_actions = client.get(f"/api/batches/{draft}/progress").json()["available_actions"]
    reviewed_actions = client.get(f"/api/batches/{reviewed}/progress").json()[
        "available_actions"
    ]

    assert "start_scoring" in draft_actions
    assert "archive" not in draft_actions
    assert "archive" in reviewed_actions


def test_archived_batch_offers_only_reopen(client):
    batch_id = _seed_batch(client, papers=1, status="archived")

    body = client.get(f"/api/batches/{batch_id}/progress").json()

    assert body["available_actions"] == ["reopen"]


def test_progress_is_scoped_to_the_visible_batch(client):
    assert client.get("/api/batches/does-not-exist/progress").status_code == 404
