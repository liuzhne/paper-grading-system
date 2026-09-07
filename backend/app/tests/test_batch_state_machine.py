"""批次业务阶段状态机与守卫（前端 v2 计划 §5-C）。

设计要点：
- 七种**业务阶段** `draft / parsing / scoring / scored / scored_with_errors /
  reviewed / archived`，与 BatchScoringJob 的执行状态是两件事。
- 所有触发都经同一服务；客户端不能直接写阶段，尤其不能直接写终态。
- `state_version` 提供乐观并发；期望版本不匹配即冲突，整次不写入。
- `archived` 下拒绝一切写动作，直到显式 reopen。
"""

import pytest

from backend.app.db import models
from backend.app.services.batches import state


def _batch(session, *, status="draft", name="batch"):
    rubric = models.Rubric(name=f"{name} rubric", version="v1", total_score=100)
    session.add(rubric)
    session.flush()
    batch = models.GradingBatch(name=name, rubric_id=rubric.id, status=status)
    session.add(batch)
    session.flush()
    return batch


# ---------------------------------------------------------------- 合法转移


@pytest.mark.parametrize(
    "event, start, expected",
    [
        ("start_parsing", "draft", "parsing"),
        ("finish_parsing", "parsing", "draft"),
        ("start_scoring", "draft", "scoring"),
        ("start_scoring", "scored", "scoring"),
        ("start_scoring", "scored_with_errors", "scoring"),
        # 重评撤销既有复核结论。
        ("start_scoring", "reviewed", "scoring"),
        ("complete_review", "scored", "reviewed"),
        ("complete_review", "scored_with_errors", "reviewed"),
        ("archive", "reviewed", "archived"),
    ],
)
def test_legal_transitions(client, event, start, expected):
    with client.session_factory() as session:
        batch = _batch(session, status=start)

        state.apply_event(session, batch, event)

        assert batch.status == expected


@pytest.mark.parametrize("outcome", ["scored", "scored_with_errors"])
def test_finish_scoring_takes_the_computed_outcome(client, outcome):
    with client.session_factory() as session:
        batch = _batch(session, status="scoring")

        state.apply_event(session, batch, "finish_scoring", outcome=outcome)

        assert batch.status == outcome


def test_finish_scoring_rejects_an_outcome_outside_the_two_terminal_stages(client):
    with client.session_factory() as session:
        batch = _batch(session, status="scoring")

        with pytest.raises(state.BatchStateError):
            state.apply_event(session, batch, "finish_scoring", outcome="reviewed")


# ---------------------------------------------------------------- 非法转移


@pytest.mark.parametrize(
    "event, start",
    [
        # 解析尚未开始就宣称结束。
        ("finish_parsing", "draft"),
        # 没有在评分就宣称评完。
        ("finish_scoring", "draft"),
        ("finish_scoring", "reviewed"),
        # 评分中不能直接跳到复核完成。
        ("complete_review", "scoring"),
        ("complete_review", "draft"),
        # 未复核不得归档。
        ("archive", "scored"),
        ("archive", "draft"),
        # 解析与评分互斥。
        ("start_parsing", "scoring"),
        ("start_scoring", "parsing"),
    ],
)
def test_illegal_transitions_are_rejected(client, event, start):
    with client.session_factory() as session:
        batch = _batch(session, status=start)

        with pytest.raises(state.BatchStateError):
            state.apply_event(session, batch, event)

        assert batch.status == start


# ---------------------------------------------------------------- 归档守卫


@pytest.mark.parametrize(
    "event", ["start_parsing", "start_scoring", "finish_scoring", "complete_review"]
)
def test_archived_batches_reject_every_write_event(client, event):
    with client.session_factory() as session:
        batch = _batch(session, status="archived")

        with pytest.raises(state.BatchArchived):
            state.apply_event(session, batch, event)

        assert batch.status == "archived"


def test_reopen_recomputes_the_stable_stage_instead_of_trusting_the_caller(client):
    """重开后阶段由当前材料与结果推导，不能由调用方指定。"""
    with client.session_factory() as session:
        batch = _batch(session, status="archived")

        state.apply_event(session, batch, "reopen")

        # 没有材料也没有结果时，稳定阶段是 draft。
        assert batch.status == "draft"


def test_guard_writable_rejects_archived_and_allows_others(client):
    with client.session_factory() as session:
        archived = _batch(session, status="archived", name="archived")
        active = _batch(session, status="scored", name="active")

        with pytest.raises(state.BatchArchived):
            state.guard_writable(archived)
        state.guard_writable(active)  # 不抛异常即通过


# ---------------------------------------------------------------- 乐观并发


def test_state_version_increments_on_every_applied_event(client):
    with client.session_factory() as session:
        batch = _batch(session, status="draft")
        before = batch.state_version

        state.apply_event(session, batch, "start_parsing")

        assert batch.state_version == before + 1


def test_stale_expected_version_conflicts_and_writes_nothing(client):
    with client.session_factory() as session:
        batch = _batch(session, status="draft")
        stale = batch.state_version - 1

        with pytest.raises(state.BatchStateConflict):
            state.apply_event(session, batch, "start_parsing", expected_version=stale)

        assert batch.status == "draft"
        assert batch.state_version != stale


def test_matching_expected_version_applies(client):
    with client.session_factory() as session:
        batch = _batch(session, status="draft")

        state.apply_event(
            session, batch, "start_parsing", expected_version=batch.state_version
        )

        assert batch.status == "parsing"


# ---------------------------------------------------------------- 取消


@pytest.mark.parametrize(
    "start, expected",
    [
        # 取消后回落到当前结果所对应的阶段，而不是统一写成 draft。
        ("parsing", "draft"),
        ("scoring", "draft"),
    ],
)
def test_cancel_falls_back_to_the_stage_implied_by_current_results(
    client, start, expected
):
    with client.session_factory() as session:
        batch = _batch(session, status=start)

        state.apply_event(session, batch, "cancel")

        assert batch.status == expected


def test_cancel_is_rejected_when_nothing_is_running(client):
    with client.session_factory() as session:
        batch = _batch(session, status="reviewed")

        with pytest.raises(state.BatchStateError):
            state.apply_event(session, batch, "cancel")


# ---------------------------------------------------------------- 枚举完整性


def test_every_designed_stage_is_representable(client):
    assert set(state.BATCH_STAGES) == {
        "draft",
        "parsing",
        "scoring",
        "scored",
        "scored_with_errors",
        "reviewed",
        "archived",
    }


def test_unknown_event_is_rejected(client):
    with client.session_factory() as session:
        batch = _batch(session, status="draft")

        with pytest.raises(state.BatchStateError):
            state.apply_event(session, batch, "teleport")
