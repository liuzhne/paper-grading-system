"""归档与重新打开（前端 v2 计划 §6，阶段 1/2）。

在这两个端点存在之前，`archived` 是一个**无法到达的状态**：`PATCH /batches/{id}`
明确拒绝改 status 并让调用方去用「对应的阶段动作端点」，而那个端点从未建过。
评分任务页的状态图例又把 `archived` 列了出来——用户看得见、走不到。

两个动作都带 `state_version`：归档是把批次冻住，重开是解冻，都不能凭一个过期
的页面状态执行。目标阶段由服务端从状态机推导，不由调用方指定——`reopen` 回到
归档前的稳定阶段，客户端说了不算。
"""

import pytest

from backend.app.db import models


def _batch(client):
    with client.session_factory() as session:
        rubric = models.Rubric(name="archive rubric", version="v1", total_score=100)
        session.add(rubric)
        session.commit()
        rubric_id = rubric.id
    return client.post(
        "/api/batches", json={"name": "archive batch", "rubric_id": rubric_id}
    ).json()


def _force_stage(client, batch_id, stage):
    """直接落库设阶段。到达 reviewed 要跑完整条评分与复核链路，那是别的测试
    的职责；这里要测的是归档动作本身。"""
    with client.session_factory() as session:
        batch = session.get(models.GradingBatch, batch_id)
        batch.status = stage
        session.add(batch)
        session.commit()
        return batch.state_version


def test_archive_moves_a_reviewed_batch_to_archived(client):
    batch = _batch(client)
    version = _force_stage(client, batch["id"], "reviewed")

    response = client.post(
        "/api/batches/%s/archive" % batch["id"],
        json={"state_version": version},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "archived"
    assert body["state_version"] == version + 1


def test_archive_rejects_a_batch_that_has_not_been_reviewed(client):
    """归档意味着结论已定。draft 批次归档会把一个没有结论的批次冻住。"""
    batch = _batch(client)
    version = _force_stage(client, batch["id"], "draft")

    response = client.post(
        "/api/batches/%s/archive" % batch["id"],
        json={"state_version": version},
    )

    assert response.status_code == 409
    assert "archive" in response.json()["detail"] or "阶段" in response.json()["detail"]


def test_archive_rejects_a_stale_version(client):
    batch = _batch(client)
    version = _force_stage(client, batch["id"], "reviewed")

    response = client.post(
        "/api/batches/%s/archive" % batch["id"],
        json={"state_version": version + 5},
    )

    assert response.status_code == 409


def test_reopen_returns_to_the_stage_the_batch_was_archived_from(client):
    batch = _batch(client)
    version = _force_stage(client, batch["id"], "reviewed")
    archived = client.post(
        "/api/batches/%s/archive" % batch["id"],
        json={"state_version": version},
    ).json()

    response = client.post(
        "/api/batches/%s/reopen" % batch["id"],
        json={"state_version": archived["state_version"]},
    )

    assert response.status_code == 200, response.text
    # 目标由服务端从**当前结果**推导，不是记住归档前那个值：这个批次没有任何
    # 材料与评分，稳定阶段就是 draft。记住旧值会把一个空批次重开成 reviewed，
    # 凭空造出一个不存在的结论。
    assert response.json()["status"] == "draft"


def test_reopen_rejects_a_batch_that_is_not_archived(client):
    batch = _batch(client)
    version = _force_stage(client, batch["id"], "reviewed")

    response = client.post(
        "/api/batches/%s/reopen" % batch["id"],
        json={"state_version": version},
    )

    assert response.status_code == 409


@pytest.mark.parametrize("action", ["archive", "reopen"])
def test_actions_reject_an_unknown_batch(client, action):
    response = client.post(
        "/api/batches/does-not-exist/%s" % action, json={"state_version": 1}
    )

    assert response.status_code == 404


def test_archived_batch_refuses_writes(client):
    """归档不是一个标签，是写保护。"""
    batch = _batch(client)
    version = _force_stage(client, batch["id"], "reviewed")
    client.post(
        "/api/batches/%s/archive" % batch["id"], json={"state_version": version}
    )

    response = client.patch(
        "/api/batches/%s" % batch["id"], json={"name": "改个名字"}
    )

    assert response.status_code == 409


def test_reopen_lands_on_the_stage_the_results_actually_support(client):
    """有已评分材料时，重开落在结果支持的阶段，而不是一律回 draft。"""
    batch = _batch(client)
    with client.session_factory() as session:
        rubric_id = session.get(models.GradingBatch, batch["id"]).rubric_id
        paper = models.Paper(
            batch_id=batch["id"],
            file_name="p.docx",
            file_path="p.docx",
            status="parsed",
        )
        session.add(paper)
        session.flush()
        session.add(
            models.ScoringRun(paper_id=paper.id, rubric_id=rubric_id, status="scored")
        )
        session.commit()

    version = _force_stage(client, batch["id"], "reviewed")
    archived = client.post(
        "/api/batches/%s/archive" % batch["id"], json={"state_version": version}
    ).json()
    response = client.post(
        "/api/batches/%s/reopen" % batch["id"],
        json={"state_version": archived["state_version"]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "scored"


def test_writing_to_an_archived_batch_is_a_conflict_not_a_crash(client):
    """`BatchArchived` 以前无人捕获，写归档批次会 500。

    归档此前不可达，所以这条路径从没被走到过；补上端点之后它就是日常路径了，
    而 500 会让前端把「需要先重新打开」显示成「服务器出错」。
    """
    batch = _batch(client)
    version = _force_stage(client, batch["id"], "reviewed")
    client.post(
        "/api/batches/%s/archive" % batch["id"], json={"state_version": version}
    )

    response = client.patch("/api/batches/%s" % batch["id"], json={"name": "x"})

    assert response.status_code == 409
    assert "归档" in response.json()["detail"]


def _job(client, batch_id, status):
    from backend.app.services.batch_scoring import jobs as job_module

    with client.session_factory() as session:
        session.add(
            models.BatchScoringJob(
                grading_batch_id=batch_id,
                generation=1,
                status=status,
                total_items=0,
                observation_policy={},
                observation_policy_hash="0" * 64,
            )
        )
        session.commit()
    return job_module.ACTIVE_JOB_STATUSES


@pytest.mark.parametrize("status", ["queued", "running", "cancel_requested"])
def test_archive_refuses_while_a_job_is_still_active(client, status):
    """§5-C：仅 reviewed **且无活动任务**可归档。

    阶段判断挡不住这一格：`reviewed` 与一个仍在跑的任务可以并存（任务刚被拉起、
    阶段投影还没跟上）。归档会把批次转成只读，而那个任务还会继续写。
    """
    batch = _batch(client)
    _job(client, batch["id"], status)
    version = _force_stage(client, batch["id"], "reviewed")

    response = client.post(
        "/api/batches/%s/archive" % batch["id"], json={"state_version": version}
    )

    assert response.status_code == 409
    assert "任务" in response.json()["detail"]


def test_archive_allows_a_finished_job(client):
    batch = _batch(client)
    _job(client, batch["id"], "completed")
    version = _force_stage(client, batch["id"], "reviewed")

    response = client.post(
        "/api/batches/%s/archive" % batch["id"], json={"state_version": version}
    )

    assert response.status_code == 200
