"""批次阶段的 API 守卫（前端 v2 计划 §5-C、§6）。

客户端不能直接写业务阶段：创建只允许 draft，PATCH 不接受阶段变更。
兼容期对携带**未变化** status 的旧请求可作 no-op，改变阶段必须走动作服务。
"""

import pytest

from backend.app.db import models


def _rubric_id(client):
    with client.session_factory() as session:
        rubric = models.Rubric(name="guard rubric", version="v1", total_score=100)
        session.add(rubric)
        session.commit()
        return rubric.id


def _create(client, rubric_id, **extra):
    return client.post(
        "/api/batches", json={"name": "guard batch", "rubric_id": rubric_id, **extra}
    )


def test_create_defaults_to_draft(client):
    response = _create(client, _rubric_id(client))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "draft"
    assert body["state_version"] >= 1


@pytest.mark.parametrize(
    "status", ["scoring", "scored", "scored_with_errors", "reviewed", "archived"]
)
def test_create_rejects_any_stage_other_than_draft(client, status):
    response = _create(client, _rubric_id(client), status=status)

    assert response.status_code == 422


def test_create_still_accepts_an_explicit_draft(client):
    response = _create(client, _rubric_id(client), status="draft")

    assert response.status_code == 200
    assert response.json()["status"] == "draft"


def test_patch_cannot_change_the_stage(client):
    batch_id = _create(client, _rubric_id(client)).json()["id"]

    response = client.patch(f"/api/batches/{batch_id}", json={"status": "reviewed"})

    assert response.status_code == 409
    assert client.get(f"/api/batches/{batch_id}").json()["status"] == "draft"


def test_patch_with_an_unchanged_stage_is_a_no_op_for_legacy_clients(client):
    batch_id = _create(client, _rubric_id(client)).json()["id"]

    response = client.patch(
        f"/api/batches/{batch_id}", json={"name": "renamed", "status": "draft"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "renamed"
    assert body["status"] == "draft"


def test_patch_without_status_still_edits_other_fields(client):
    batch_id = _create(client, _rubric_id(client)).json()["id"]

    response = client.patch(f"/api/batches/{batch_id}", json={"department": "计算机学院"})

    assert response.status_code == 200
    assert response.json()["department"] == "计算机学院"


def test_read_exposes_state_version_for_optimistic_concurrency(client):
    batch_id = _create(client, _rubric_id(client)).json()["id"]

    body = client.get(f"/api/batches/{batch_id}").json()

    assert isinstance(body["state_version"], int)
