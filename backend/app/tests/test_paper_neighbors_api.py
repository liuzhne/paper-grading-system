"""`GET /api/papers/{id}/neighbors`（前端 v2 计划 §6）。

评分工作区的上一份 / 下一份导航。排序必须与左栏材料列表**完全一致**，
否则「下一份」跳到的不是用户看到的下一行。
"""

from backend.app.db import models


def _seed(client, *, count=3, other_batch=False):
    with client.session_factory() as session:
        rubric = models.Rubric(name="n rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(name="n batch", rubric_id=rubric.id, status="scored")
        session.add(batch)
        session.flush()
        ids = []
        for index in range(count):
            paper = models.Paper(
                batch_id=batch.id,
                file_name="p%d.pdf" % index,
                file_path="p%d.pdf" % index,
                status="parsed",
                student_id="SE-2026-%03d" % (index + 1),
            )
            session.add(paper)
            session.flush()
            ids.append(paper.id)
        outside = None
        if other_batch:
            second = models.GradingBatch(
                name="other", rubric_id=rubric.id, status="scored"
            )
            session.add(second)
            session.flush()
            paper = models.Paper(
                batch_id=second.id,
                file_name="x.pdf",
                file_path="x.pdf",
                status="parsed",
                student_id="OTHER-001",
            )
            session.add(paper)
            session.flush()
            outside = paper.id
        session.commit()
        return batch.id, ids, outside


def _listed(client, batch_id):
    """期望值一律从实际列表推导，不假设 seed 顺序等于展示顺序。"""
    return [p["id"] for p in client.get(f"/api/papers?batch_id={batch_id}").json()]


def test_middle_paper_has_both_neighbours(client):
    batch_id, _, _ = _seed(client, count=3)
    listed = _listed(client, batch_id)

    body = client.get(f"/api/papers/{listed[1]}/neighbors?batch_id={batch_id}").json()

    assert body["previous_paper_id"] == listed[0]
    assert body["next_paper_id"] == listed[2]
    assert body["position"] == 2
    assert body["total"] == 3


def test_first_paper_has_no_previous(client):
    batch_id, _, _ = _seed(client, count=3)
    listed = _listed(client, batch_id)

    body = client.get(f"/api/papers/{listed[0]}/neighbors?batch_id={batch_id}").json()

    assert body["previous_paper_id"] is None
    assert body["next_paper_id"] == listed[1]
    assert body["position"] == 1


def test_last_paper_has_no_next(client):
    batch_id, _, _ = _seed(client, count=3)
    listed = _listed(client, batch_id)

    body = client.get(f"/api/papers/{listed[2]}/neighbors?batch_id={batch_id}").json()

    assert body["previous_paper_id"] == listed[1]
    assert body["next_paper_id"] is None


def test_single_paper_batch_has_neither(client):
    batch_id, ids, _ = _seed(client, count=1)

    body = client.get(f"/api/papers/{ids[0]}/neighbors?batch_id={batch_id}").json()

    assert body["previous_paper_id"] is None
    assert body["next_paper_id"] is None
    assert body["total"] == 1


def test_ordering_matches_the_material_list(client):
    """与 GET /papers?batch_id= 的顺序一致，否则「下一份」会跳错行。"""
    batch_id, _, _ = _seed(client, count=4)

    listed = [p["id"] for p in client.get(f"/api/papers?batch_id={batch_id}").json()]
    walked = [listed[0]]
    while True:
        body = client.get(
            f"/api/papers/{walked[-1]}/neighbors?batch_id={batch_id}"
        ).json()
        if body["next_paper_id"] is None:
            break
        walked.append(body["next_paper_id"])

    assert walked == listed


def test_paper_outside_the_batch_is_rejected(client):
    """paper 必须属于该 batch，否则会把别的批次的材料串进导航。"""
    batch_id, _, outside = _seed(client, count=2, other_batch=True)

    response = client.get(f"/api/papers/{outside}/neighbors?batch_id={batch_id}")

    assert response.status_code == 404


def test_unknown_paper_is_404(client):
    batch_id, _, _ = _seed(client, count=1)

    assert (
        client.get(f"/api/papers/nope/neighbors?batch_id={batch_id}").status_code == 404
    )


def test_batch_id_is_required(client):
    _, ids, _ = _seed(client, count=1)

    assert client.get(f"/api/papers/{ids[0]}/neighbors").status_code == 422
