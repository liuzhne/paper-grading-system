"""`GET /api/batches/overview`（前端 v2 计划 §6）。

工作台 KPI。与旧的 `GET /batches` 数组合同**分开**：后者保持原形状不变，
聚合投影走这个新端点，避免为了加字段而破坏旧客户端。

计数一律经结果选择器，和评分任务页、复核页同源；否则工作台说 18 份已确认、
复核页说 16 份，用户无从判断哪个是真的。
"""

from backend.app.db import models


def _seed(client, specs):
    """specs: [(status, total_papers, scored, failed)]"""
    with client.session_factory() as session:
        rubric = models.Rubric(name="ov rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        for index, (status, total, scored, failed) in enumerate(specs):
            batch = models.GradingBatch(
                name="batch %d" % index, rubric_id=rubric.id, status=status
            )
            session.add(batch)
            session.flush()
            for position in range(total):
                paper = models.Paper(
                    batch_id=batch.id,
                    file_name="p.pdf",
                    file_path="p.pdf",
                    status="parsed",
                )
                session.add(paper)
                session.flush()
                if position < scored:
                    session.add(
                        models.ScoringRun(
                            paper_id=paper.id, rubric_id=rubric.id, status="scored"
                        )
                    )
                elif position < scored + failed:
                    session.add(
                        models.ScoringRun(
                            paper_id=paper.id, rubric_id=rubric.id, status="failed"
                        )
                    )
        session.commit()


def test_overview_counts_batches_by_stage(client):
    _seed(client, [("draft", 0, 0, 0), ("scoring", 2, 1, 0), ("archived", 1, 1, 0)])

    body = client.get("/api/batches/overview").json()

    assert body["stage_counts"]["draft"] == 1
    assert body["stage_counts"]["scoring"] == 1
    assert body["stage_counts"]["archived"] == 1
    assert body["total_batches"] == 3


def test_active_batches_exclude_archived_and_reviewed(client):
    """「进行中」是待办口径：已归档与已复核的批次不该继续占用注意力。"""
    _seed(
        client,
        [
            ("scoring", 1, 0, 0),
            ("scored_with_errors", 1, 1, 0),
            ("reviewed", 1, 1, 0),
            ("archived", 1, 1, 0),
        ],
    )

    body = client.get("/api/batches/overview").json()

    assert body["active_batches"] == 2


def test_material_totals_come_from_the_result_selector(client):
    _seed(client, [("scoring", 3, 1, 1)])

    body = client.get("/api/batches/overview").json()

    assert body["material_counts"]["total"] == 3
    assert body["material_counts"]["scored"] == 1
    assert body["material_counts"]["failed"] == 1
    assert body["material_counts"]["pending"] == 1


def test_archived_batches_do_not_inflate_the_todo_counters(client):
    """归档批次的材料不计入待办，否则待评数永远降不下去。"""
    _seed(client, [("scoring", 2, 0, 0), ("archived", 5, 5, 0)])

    body = client.get("/api/batches/overview").json()

    assert body["material_counts"]["pending"] == 2


def test_empty_deployment_reports_zeroes(client):
    body = client.get("/api/batches/overview").json()

    assert body["total_batches"] == 0
    assert body["active_batches"] == 0
    assert body["material_counts"]["total"] == 0


def test_overview_is_scoped_to_the_current_organization(client, monkeypatch):
    """跨组织数据不得进入 KPI。"""
    from backend.app.core.config import settings

    _seed(client, [("scoring", 1, 0, 0)])

    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "bootstrap-admin-password")
    monkeypatch.setattr(settings, "AUTH_SECRET", "d" * 48)
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", False)
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "bootstrap-admin-password"},
        ).status_code
        == 204
    )

    body = client.get("/api/batches/overview").json()

    # 种子批次没有 organization_id，登录后的默认组织看不到它。
    assert body["total_batches"] == 0
