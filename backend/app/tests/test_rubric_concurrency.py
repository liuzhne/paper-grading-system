"""评分标准草稿的乐观并发（v3 计划 §4.3 的复核结论）。

计划原本要给 `Rubric` 加 `state_version`。**核实后发现是多余的**：编辑草稿的实际
路径是 `recompile`，它已经要求显式的 `supersedes_compilation_id`，拿着过期值提交
会被 `RUBRIC_RECOMPILE_STALE` 拒绝——这就是编译层面的乐观并发。

`PATCH /rubrics/{id}` 在这条路径上基本不可达：`POST /rubrics` 会自动产生一份编译
产物，之后 PATCH 一律返回 `RUBRIC_RECOMPILE_REQUIRED`。再加一个 rubric 级版本号
只会多一个没人读的列和一次迁移。

这组用例把既有机制钉住，免得以后有人以为这里没有并发保护而重复造一个。
"""

from sqlalchemy import select

from backend.app.db import models


def _draft(client):
    created = client.post(
        "/api/rubrics",
        json={
            "name": "并发测试标准",
            "version": "v1.0",
            "total_score": 100,
            "criteria": [
                {"code": "T01", "name": "选题", "max_score": 50},
                {"code": "T02", "name": "方法", "max_score": 50},
            ],
        },
    )
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _active_compilation_id(client, rubric_id):
    with client.session_factory() as session:
        return session.scalar(
            select(models.RubricCompilation.id).where(
                models.RubricCompilation.rubric_id == rubric_id
            )
        )


def test_creating_a_rubric_already_produces_a_compilation(client):
    """这条是上面结论的前提，单独钉住。

    它一旦不成立（比如将来创建不再自动编译），`PATCH` 就会重新变成可达路径，
    那时才需要重新考虑 rubric 级的并发保护。
    """
    rubric_id = _draft(client)

    assert _active_compilation_id(client, rubric_id) is not None


def test_patch_refuses_once_a_compilation_exists(client):
    """有执行草稿后不能直接覆盖历史内容——这是既有约束，不是并发保护的替代品。"""
    rubric_id = _draft(client)

    response = client.patch("/api/rubrics/%s" % rubric_id, json={"name": "直接改"})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RUBRIC_RECOMPILE_REQUIRED"


def test_recompiling_with_a_stale_compilation_is_rejected(client):
    """第二个人拿着过期的执行草稿提交时被拒——这就是这里的乐观并发。"""
    rubric_id = _draft(client)
    stale = _active_compilation_id(client, rubric_id)

    first = client.post(
        "/api/rubrics/%s/recompile" % rubric_id,
        json={
            "supersedes_compilation_id": stale,
            "version": "v1.1",
            "criteria": [
                {"code": "T01", "name": "选题", "max_score": 60},
                {"code": "T02", "name": "方法", "max_score": 40},
            ],
        },
    )
    assert first.status_code == 200, first.text

    conflicted = client.post(
        "/api/rubrics/%s/recompile" % rubric_id,
        json={
            "supersedes_compilation_id": stale,
            "version": "v1.2",
            "criteria": [
                {"code": "T01", "name": "选题", "max_score": 70},
                {"code": "T02", "name": "方法", "max_score": 30},
            ],
        },
    )

    assert conflicted.status_code == 422, conflicted.text
    detail = conflicted.json()["detail"]
    # 实际走的是 `RUBRIC_RECOMPILE_BLOCKED`：服务端先判「你基于的草稿还是不是当前
    # 活跃的那份」，不是当前的就整体拒绝。`RUBRIC_RECOMPILE_STALE`（409）留给
    # 「那份草稿已经不存在」的情形——两者都拒，但原因不同，文案也不同。
    assert detail["code"] == "RUBRIC_RECOMPILE_BLOCKED"
    assert detail["context"]["reason"] == (
        "draft recompilation predecessor is not the active compilation"
    )
    # 只说「冲突」没有用，要告诉人下一步做什么。
    assert "重试" in detail["user_action"]
