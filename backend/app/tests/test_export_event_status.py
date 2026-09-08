"""导出事件的三种状态（前端 v2 计划 §5-F）。

§5-F 要求「状态区分生成中/生成成功/失败」。实现此前只会写 `generated`：一次
失败的 Sheets 写入不留任何痕迹，历史只显示成功——而输出中心存在的理由正是如实
显示导出发生过什么。**按遗漏说谎和按内容说谎一样糟**。

失败必须带原因，成功不许带：一条挂着错误信息的成功记录读起来像出过问题，会让
对账的人去追一个不存在的故障。

已定案的事件不能回退到「生成中」——审计记录不倒着走。
"""

import pytest

from backend.app.db import models
from backend.app.services.batches.results import select_current_results


def _batch_with_revision(client):
    with client.session_factory() as session:
        rubric = models.Rubric(name="ev rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(
            name="ev batch", rubric_id=rubric.id, status="scored"
        )
        session.add(batch)
        session.commit()
        revision = select_current_results(session, batch).revision
        return batch.id, revision


def _post(client, batch_id, revision, **extra):
    return client.post(
        "/api/batches/%s/export-events" % batch_id,
        json={
            "channel": "xlsx",
            "scope": "batch",
            "result_revision": revision,
            **extra,
        },
    )


def test_status_defaults_to_generated_for_existing_clients(client):
    batch_id, revision = _batch_with_revision(client)

    body = _post(client, batch_id, revision).json()

    assert body["status"] == "generated"


@pytest.mark.parametrize("status", ["generating", "generated"])
def test_the_three_states_are_accepted(client, status):
    batch_id, revision = _batch_with_revision(client)

    response = _post(client, batch_id, revision, status=status)

    assert response.status_code == 201, response.text
    assert response.json()["status"] == status


def test_a_failure_is_recordable(client):
    """失败必须留痕，否则历史只剩成功，读起来像从没出过问题。"""
    batch_id, revision = _batch_with_revision(client)

    response = _post(
        client,
        batch_id,
        revision,
        status="failed",
        error_message="Sheets 写入被拒绝：目标表无权限。",
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "failed"
    assert "权限" in response.json()["error_message"]


def test_a_failure_without_a_reason_is_rejected(client):
    """「失败了但不知道为什么」对对账毫无用处。"""
    batch_id, revision = _batch_with_revision(client)

    response = _post(client, batch_id, revision, status="failed")

    assert response.status_code == 422


def test_a_success_may_not_carry_an_error_message(client):
    """挂着错误信息的成功记录会让人去追一个不存在的故障。"""
    batch_id, revision = _batch_with_revision(client)

    response = _post(
        client, batch_id, revision, status="generated", error_message="???"
    )

    assert response.status_code == 422


def test_an_unknown_status_is_rejected(client):
    batch_id, revision = _batch_with_revision(client)

    assert _post(client, batch_id, revision, status="downloaded").status_code == 422


def test_finalising_a_generating_event(client):
    batch_id, revision = _batch_with_revision(client)
    event_id = _post(client, batch_id, revision, status="generating").json()["id"]

    response = client.patch(
        "/api/batches/%s/export-events/%s" % (batch_id, event_id),
        json={"status": "generated"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "generated"


def test_finalising_as_failed_keeps_the_reason(client):
    batch_id, revision = _batch_with_revision(client)
    event_id = _post(client, batch_id, revision, status="generating").json()["id"]

    response = client.patch(
        "/api/batches/%s/export-events/%s" % (batch_id, event_id),
        json={"status": "failed", "error_message": "网络中断"},
    )

    assert response.status_code == 200
    assert response.json()["error_message"] == "网络中断"


def test_generating_is_never_a_valid_target(client):
    """定案是单向的：没有任何理由把一条记录改回「还在生成」。"""
    batch_id, revision = _batch_with_revision(client)
    event_id = _post(client, batch_id, revision, status="generating").json()["id"]

    response = client.patch(
        "/api/batches/%s/export-events/%s" % (batch_id, event_id),
        json={"status": "generating"},
    )

    assert response.status_code == 422


def test_a_settled_event_cannot_be_rewritten(client):
    """审计记录不倒着走：改写它等于让历史配合当下的说法。"""
    batch_id, revision = _batch_with_revision(client)
    event_id = _post(client, batch_id, revision, status="generated").json()["id"]

    response = client.patch(
        "/api/batches/%s/export-events/%s" % (batch_id, event_id),
        json={"status": "failed", "error_message": "其实失败了"},
    )

    assert response.status_code == 409
    assert "已定案" in response.json()["detail"]


def test_history_shows_the_failure(client):
    batch_id, revision = _batch_with_revision(client)
    _post(
        client,
        batch_id,
        revision,
        status="failed",
        error_message="Sheets 写入被拒绝。",
    )

    history = client.get("/api/batches/%s/export-history" % batch_id).json()

    assert [row["status"] for row in history["entries"]] == ["failed"]


def test_export_event_write_paths_are_role_gated():
    """写导出审计必须与同文件其它写端点同一门控（§2.1 权限矩阵）。

    `_visible_batch` 只查组织归属、不查角色。少了 `require_organization_role`，
    任何能看到该批次的成员都能写入导出审计——包括把一次成功标记成失败并附上
    任意原因。审计记录能被随手改写，它就不再是审计。

    静态断言：这两条路径没有可靠的低权限运行时夹具（开发模式放行一切），而
    「漏了一行守卫」正是这种夹具最容易漏掉的缺陷。
    """
    import inspect

    from backend.app.api.routes import batches as routes

    for name in ("create_export_event", "update_export_event"):
        source = inspect.getsource(getattr(routes, name))
        assert "require_organization_role" in source, name


def test_every_batch_write_route_is_role_gated():
    """把上一条扩到全文件：漏一行守卫不该靠人逐个记住。

    只看写方法（POST/PATCH/DELETE）。读端点的边界是组织归属，不是角色。
    """
    import inspect
    import re

    from backend.app.api.routes import batches as routes

    source = inspect.getsource(routes)
    # 逐个函数切片：装饰器行 + 函数体，直到下一个装饰器。
    blocks = re.split(r"\n(?=@router\.)", source)
    missing = []
    for block in blocks:
        header = block.split("\n", 1)[0]
        if not re.match(r"@router\.(post|patch|delete)\(", header):
            continue
        name = re.search(r"\ndef (\w+)\(", block)
        if not name:
            continue
        gated = "require_organization_role" in block
        # 委派给另一个已门控端点的薄包装（如 /start -> /score）不必重复检查；
        # 要求它自己再写一遍只会制造两处可能失配的守卫。
        delegates = re.search(r"return \w+_endpoint\(", block) is not None
        if not gated and not delegates:
            missing.append(name.group(1))

    assert not missing, "写端点缺少角色门控：%s" % ", ".join(missing)
