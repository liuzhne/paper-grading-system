"""人工复核任务的范围过滤与分页（前端 v2 计划 §6）。

复核队列按批次工作，而这个列表此前只能按 status 过滤：要拿一个批次的阻塞任务，
只能把全组织的任务都取回来再在客户端筛。任务累积之后这条路会先变慢、再变错——
一旦加上分页，客户端过滤会把"这一页里没有该批次"显示成"该批次没有阻塞任务"。

组织隔离优先于所有过滤：`batch_id` 指向别的组织的批次时返回空，而不是越过隔离
去查。范围参数是过滤器，不是提权入口。
"""

from backend.app.db import models

#: 0022 回填旧单租户资源时使用的默认组织。
DEFAULT_ORGANIZATION_ID = "00000000-0000-0000-0000-000000000002"


def _fixture(client, *, tasks_per_batch=2, batches=2):
    """造两个批次，各带若干条阻塞任务。"""
    made = {}
    with client.session_factory() as session:
        rubric = models.Rubric(name="scope rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        org_id = None
        for index in range(batches):
            batch = models.GradingBatch(
                name="scope batch %d" % index,
                rubric_id=rubric.id,
                status="scored_with_errors",
            )
            session.add(batch)
            session.flush()
            org_id = batch.organization_id or DEFAULT_ORGANIZATION_ID
            paper = models.Paper(
                batch_id=batch.id,
                file_name="p%d.docx" % index,
                file_path="p%d.docx" % index,
                status="parsed",
            )
            session.add(paper)
            session.flush()
            run = models.ScoringRun(
                paper_id=paper.id, rubric_id=rubric.id, status="scored"
            )
            session.add(run)
            session.flush()
            for position in range(tasks_per_batch):
                session.add(
                    models.ManualReviewTask(
                        organization_id=org_id,
                        scoring_run_id=run.id,
                        criterion_code="T%02d" % position,
                        trigger_code="provider_error",
                        trigger_message="Provider 调用失败，该项结论尚不成立。",
                        status="open",
                        priority=position,
                    )
                )
            made[batch.id] = run.id
        session.commit()
    return made


def test_filtering_by_batch_returns_only_that_batch(client):
    made = _fixture(client)
    target = next(iter(made))

    body = client.get("/api/v2/manual-review-tasks?batch_id=%s" % target).json()

    assert body
    assert {row["scoring_run_id"] for row in body} == {made[target]}


def test_filtering_by_run_narrows_further(client):
    made = _fixture(client)
    target_batch, target_run = next(iter(made.items()))

    body = client.get(
        "/api/v2/manual-review-tasks?scoring_run_id=%s" % target_run
    ).json()

    assert body
    assert all(row["scoring_run_id"] == target_run for row in body)


def test_without_scope_every_task_is_returned(client):
    _fixture(client)

    body = client.get("/api/v2/manual-review-tasks").json()

    assert len(body) == 4


def test_limit_and_offset_page_through_a_stable_order(client):
    _fixture(client, tasks_per_batch=3, batches=1)

    first = client.get("/api/v2/manual-review-tasks?limit=2").json()
    second = client.get("/api/v2/manual-review-tasks?limit=2&offset=2").json()

    assert len(first) == 2
    assert len(second) == 1
    # 顺序稳定，两页不重不漏。
    assert {row["id"] for row in first}.isdisjoint({row["id"] for row in second})


def test_scope_filters_do_not_cross_organizations(client):
    """范围参数是过滤器，不是提权入口。

    直接调服务层：仓库默认的开发模式没有组织上下文，走 HTTP 只会跳过组织过滤，
    测不到真正的判定（与 `test_ops_permissions` 同样的理由）。
    """
    from backend.app.services.submissions.review_tasks import list_manual_tasks

    made = _fixture(client)
    target = next(iter(made))
    with client.session_factory() as session:
        batch = session.get(models.GradingBatch, target)
        batch.organization_id = "some-other-organization"
        session.add(batch)
        session.commit()

        # 任务行上的 organization_id 仍是旧值——隔离不能靠它兜底。
        tasks = list_manual_tasks(
            session,
            organization_id=DEFAULT_ORGANIZATION_ID,
            batch_id=target,
        )

    assert list(tasks) == []


def test_unknown_batch_returns_empty_rather_than_everything(client):
    """过滤参数解析失败时回落成「不过滤」，会把全组织的任务倒给调用方。"""
    _fixture(client)

    body = client.get("/api/v2/manual-review-tasks?batch_id=does-not-exist").json()

    assert body == []
