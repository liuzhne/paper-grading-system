"""旧写入口不得绕过新守卫（前端 v2 计划 §5-B、§5-C、§8.3）。

§8.3：「旧 SPA 在并存期同步适配 capabilities、review revision 和状态错误，**不成为
绕过新服务守卫的入口**」。§5-B 说得更直白：「不能只有新端点防冲突而旧写入口仍可
覆盖」。

`PATCH /score-items/{id}` 是旧 SPA 的改分入口，两条守卫都没接上：

1. **归档批次仍可改分。** §5-C 明写 archived 下拒绝改分。挡不住的话，归档就只是
   一个标签，而不是写保护——一份已经发出去的成绩仍可能被改动。
2. **改完不动 `review_revision`。** 批量采纳靠它做乐观并发：旧入口改了分却不 bump，
   新端点就以为这一项没变过，会把人工刚改的结果按「采纳 AI 分」覆盖掉。
"""

import pytest

from backend.app.db import models


def _item(client, *, batch_status="scored"):
    with client.session_factory() as session:
        rubric = models.Rubric(name="guard rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        criterion = models.RubricCriterion(
            rubric_id=rubric.id, code="T01", name="选题", max_score=10
        )
        session.add(criterion)
        batch = models.GradingBatch(
            name="guard batch", rubric_id=rubric.id, status=batch_status
        )
        session.add(batch)
        session.flush()
        paper = models.Paper(
            batch_id=batch.id, file_name="p.docx", file_path="p.docx", status="parsed"
        )
        session.add(paper)
        session.flush()
        run = models.ScoringRun(
            paper_id=paper.id, rubric_id=rubric.id, status="scored"
        )
        session.add(run)
        session.flush()
        item = models.ScoreItem(
            scoring_run_id=run.id,
            criterion_id=criterion.id,
            max_score=10,
            ai_score=6,
            final_score=6,
            evidence_sufficient=True,
            reason="理由",
            deductions=[],
            deduction_items=[],
            evidence=[],
            need_manual_review=True,
        )
        session.add(item)
        session.commit()
        return {
            "item_id": item.id,
            "batch_id": batch.id,
            "revision": item.review_revision or 1,
        }


def _patch(client, item_id, score=8):
    return client.patch(
        "/api/score-items/%s" % item_id,
        json={"final_score": score, "reason": "人工复核后调整"},
    )


def test_ordinary_override_still_works(client):
    """收紧守卫不能把正常改分一并挡掉。"""
    made = _item(client)

    response = _patch(client, made["item_id"])

    assert response.status_code == 200, response.text
    assert response.json()["final_score"] == 8


def test_the_legacy_path_bumps_the_review_revision(client):
    """批量采纳靠 revision 做乐观并发。

    旧入口改了分却不 bump，新端点就以为这一项没变过，会把人工刚改的结果按
    「采纳 AI 分」覆盖掉——而覆盖是静默的。
    """
    made = _item(client)

    _patch(client, made["item_id"])

    with client.session_factory() as session:
        item = session.get(models.ScoreItem, made["item_id"])
        assert (item.review_revision or 1) > made["revision"]


def test_archived_batch_refuses_the_legacy_override(client):
    """§5-C：archived 下拒绝改分。挡不住的话归档只是一个标签，不是写保护。"""
    made = _item(client, batch_status="archived")

    response = _patch(client, made["item_id"])

    assert response.status_code == 409
    assert "归档" in response.json()["detail"]

    with client.session_factory() as session:
        item = session.get(models.ScoreItem, made["item_id"])
        assert float(item.final_score) == 6


@pytest.mark.parametrize("stage", ["draft", "scoring"])
def test_other_stages_are_unaffected(client, stage):
    """只挡归档：其它阶段的改分边界由既有服务判断，这里不额外收紧。"""
    made = _item(client, batch_status=stage)

    response = _patch(client, made["item_id"])

    assert response.status_code in (200, 400, 409)
    if response.status_code == 409:
        assert "归档" not in response.json()["detail"]


def _run_id(client, batch_status):
    made = _item(client, batch_status=batch_status)
    with client.session_factory() as session:
        item = session.get(models.ScoreItem, made["item_id"])
        return item.scoring_run_id


def test_archived_batch_refuses_the_legacy_run_review(client):
    """`POST /scoring-runs/{id}/review` 是同一类旧写入口。

    它把 run 与 paper 直接置为 reviewed。归档批次上仍然可用的话，一次误操作就能
    把已封存的批次里的材料改成「已复核」，而归档的意义正是不再变动。
    """
    run_id = _run_id(client, "archived")

    response = client.post(
        "/api/scoring-runs/%s/review" % run_id, json={"reason": "确认"}
    )

    assert response.status_code == 409
    assert "归档" in response.json()["detail"]


def test_run_review_still_works_on_a_live_batch(client):
    run_id = _run_id(client, "scored")

    response = client.post(
        "/api/scoring-runs/%s/review" % run_id, json={"reason": "确认"}
    )

    assert response.status_code == 200, response.text


def test_cli_translates_the_archived_guard_into_a_clean_exit():
    """CLI 也走同一条服务（§2.1「保留 API/CLI」）。

    归档守卫加在服务层之后，CLI 原先只捕 `ValueError`，`BatchArchived` 会直接漏成
    一串 traceback——运维看到的是崩溃，而不是「这个批次已归档」。崩溃与「按规则
    拒绝」是两件事，输出必须能区分。
    """
    import typer

    from backend.app.cli.main import guard_review_write
    from backend.app.services.batches.state import BatchArchived

    def _raises():
        raise BatchArchived("批次已归档，需先显式重新打开才能修改")

    with pytest.raises(typer.Exit) as excinfo:
        guard_review_write(_raises, "覆盖 T01")

    assert excinfo.value.exit_code == 2


def test_cli_still_translates_value_errors():
    """收紧不能把既有的分值校验错误一并吞掉。"""
    import typer

    from backend.app.cli.main import guard_review_write

    def _raises():
        raise ValueError("final_score out of range")

    with pytest.raises(typer.Exit):
        guard_review_write(_raises, "覆盖 T01")


def test_cli_passes_through_a_successful_write():
    from backend.app.cli.main import guard_review_write

    assert guard_review_write(lambda: "ok", "覆盖 T01") == "ok"
