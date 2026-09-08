"""首版只接受单评（前端 v2 计划 §5-G，决策 4）。

界面把「双评取均」「双评 + 仲裁」置灰了，但接口是公开的。字段被静默忽略时，
`POST /batches {"review_mode": "dual"}` 返回 200——调用方据此认为系统在做双评，
而实际上每份材料只评了一次。**错误结论比报错更贵**：它会一直被当成双评结果用。

§5-G 同时要求「不持久化没有变化语义的 review_mode 字段」：存一个不影响任何行为
的值，会让人以为这批是按那个模式评的。
"""

import pytest

from backend.app.db import models


def _rubric_id(client):
    with client.session_factory() as session:
        rubric = models.Rubric(name="mode rubric", version="v1", total_score=100)
        session.add(rubric)
        session.commit()
        return rubric.id


def _create(client, **extra):
    return client.post(
        "/api/batches",
        json={"name": "mode batch", "rubric_id": _rubric_id(client), **extra},
    )


def test_omitting_the_field_still_works(client):
    """绝大多数调用方不会传这个字段，不能因为加了校验就把它们挡住。"""
    assert _create(client).status_code == 200


def test_explicit_single_is_accepted(client):
    response = _create(client, review_mode="single")

    assert response.status_code == 200


@pytest.mark.parametrize("mode", ["dual", "dual_arbitration", "double", "", "SINGLE"])
def test_any_other_mode_is_rejected(client, mode):
    """静默忽略会让调用方以为系统在做双评，而实际每份材料只评了一次。"""
    response = _create(client, review_mode=mode)

    assert response.status_code == 422, response.text
    assert "single" in response.text or "单评" in response.text


def test_the_field_is_not_persisted(client):
    """存一个不影响任何行为的值，会让人以为这批是按那个模式评的。"""
    body = _create(client, review_mode="single").json()

    assert "review_mode" not in body
    with client.session_factory() as session:
        batch = session.get(models.GradingBatch, body["id"])
        assert not hasattr(batch, "review_mode")


def test_the_error_says_what_to_do(client):
    """置灰的界面已经解释过一次，接口的错误也要能自解释。"""
    response = _create(client, review_mode="dual")

    assert "首版" in response.text or "single" in response.text
