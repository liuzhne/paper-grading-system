"""`GET /batches` 的可选 status 过滤（前端 v2 计划 §6）。

前端此前在客户端过滤：把全部批次取回来再按阶段筛。批次多起来之后，这等于为
了看一个阶段而下载全部——而且分页一旦加上，客户端过滤会在"这一页里没有该
阶段"时显示成"没有该阶段的批次"。

**必须保留数组合同**：旧 SPA 与 CLI 直接把响应当数组用，换成 `{items: [...]}`
会让它们静默拿到空列表。
"""

import pytest

from backend.app.db import models


def _rubric_id(client):
    with client.session_factory() as session:
        rubric = models.Rubric(name="filter rubric", version="v1", total_score=100)
        session.add(rubric)
        session.commit()
        return rubric.id


def _batch_in_stage(client, rubric_id, name, stage):
    batch = client.post(
        "/api/batches", json={"name": name, "rubric_id": rubric_id}
    ).json()
    if stage != "draft":
        with client.session_factory() as session:
            row = session.get(models.GradingBatch, batch["id"])
            row.status = stage
            session.add(row)
            session.commit()
    return batch["id"]


@pytest.fixture()
def seeded(client):
    rubric_id = _rubric_id(client)
    return {
        "draft": _batch_in_stage(client, rubric_id, "草稿批次", "draft"),
        "scoring": _batch_in_stage(client, rubric_id, "评分中批次", "scoring"),
        "reviewed": _batch_in_stage(client, rubric_id, "已复核批次", "reviewed"),
    }


def test_without_the_filter_every_batch_is_returned(client, seeded):
    body = client.get("/api/batches").json()

    assert isinstance(body, list)
    assert {row["id"] for row in body} == set(seeded.values())


def test_filtering_by_one_stage(client, seeded):
    body = client.get("/api/batches?status=scoring").json()

    assert [row["id"] for row in body] == [seeded["scoring"]]


def test_filtering_accepts_several_stages(client, seeded):
    """待办视图要的是「draft 或 scoring」，不是两次请求再在前端合并。"""
    body = client.get("/api/batches?status=draft&status=scoring").json()

    assert {row["id"] for row in body} == {seeded["draft"], seeded["scoring"]}


def test_response_stays_a_bare_array(client, seeded):
    """旧 SPA 与 CLI 直接当数组用；换成信封会让它们静默拿到空列表。"""
    body = client.get("/api/batches?status=draft").json()

    assert isinstance(body, list)


def test_unknown_stage_is_rejected_rather_than_silently_empty(client, seeded):
    """拼错阶段名返回空数组，读起来就是「该阶段没有批次」——两者必须能区分。"""
    response = client.get("/api/batches?status=not_a_stage")

    assert response.status_code == 422


def test_a_stage_with_no_batches_returns_an_empty_array(client, seeded):
    body = client.get("/api/batches?status=archived").json()

    assert body == []
