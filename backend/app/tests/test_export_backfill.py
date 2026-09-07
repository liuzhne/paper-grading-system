"""旧导出日志的幂等补录（前端 v2 计划 §5-F、§7，阶段 6B）。

补录把 `SpreadsheetWriteLog` 的历史行映射成 `ExportEvent`，让输出中心切读
新表后仍能看到历史。三条硬约束：

1. **幂等。** 重复执行不产生重复事件。回退窗口内旧应用只写旧表，重新前进时
   要能再跑一次把这段补上——`alembic upgrade head` 不会重跑已完成的迁移，
   所以补录必须是可重入的独立入口。
2. **不补造操作人。** 旧表没记就留空，不能拿 run.owner 顶。
3. **不按路径或时间合并。** 一份 v1 批次 xlsx 可能对应多条 run 日志；合并
   会造出一个从未被记录过的「批次导出事件」。
"""

from backend.app.db import models
from backend.app.services.batches.export_backfill import backfill_legacy_export_logs


def _seed(client, *, logs):
    with client.session_factory() as session:
        rubric = models.Rubric(name="bf rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(name="bf batch", rubric_id=rubric.id, status="scored")
        session.add(batch)
        session.flush()
        paper = models.Paper(
            batch_id=batch.id, file_name="p.pdf", file_path="p.pdf", status="parsed"
        )
        session.add(paper)
        session.flush()
        run = models.ScoringRun(paper_id=paper.id, rubric_id=rubric.id, status="scored")
        session.add(run)
        session.flush()
        for target_type, status in logs:
            session.add(
                models.SpreadsheetWriteLog(
                    scoring_run_id=run.id, target_type=target_type, status=status
                )
            )
        session.commit()
        return batch.id, run.id


def _events(client):
    import sqlalchemy as sa

    with client.session_factory() as session:
        return session.scalars(sa.select(models.ExportEvent)).all()


def test_backfill_creates_one_event_per_legacy_row(client):
    _seed(client, logs=[("excel", "succeeded"), ("google_sheets", "succeeded")])

    with client.session_factory() as session:
        result = backfill_legacy_export_logs(session)
        session.commit()

    assert result["created"] == 2
    assert len(_events(client)) == 2


def test_backfill_is_idempotent(client):
    _seed(client, logs=[("excel", "succeeded")])

    with client.session_factory() as session:
        backfill_legacy_export_logs(session)
        session.commit()
    with client.session_factory() as session:
        second = backfill_legacy_export_logs(session)
        session.commit()

    assert second["created"] == 0
    assert second["skipped"] == 1
    assert len(_events(client)) == 1


def test_rows_added_after_a_first_pass_are_picked_up_on_rerun(client):
    """回退窗口内旧应用只写旧表；重新前进时再跑一次要能补上这段。"""
    batch_id, run_id = _seed(client, logs=[("excel", "succeeded")])
    with client.session_factory() as session:
        backfill_legacy_export_logs(session)
        session.commit()

    with client.session_factory() as session:
        session.add(
            models.SpreadsheetWriteLog(
                scoring_run_id=run_id, target_type="google_sheets", status="succeeded"
            )
        )
        session.commit()
    with client.session_factory() as session:
        again = backfill_legacy_export_logs(session)
        session.commit()

    assert again["created"] == 1
    assert len(_events(client)) == 2


def test_backfilled_events_carry_no_fabricated_operator(client):
    _seed(client, logs=[("excel", "succeeded")])

    with client.session_factory() as session:
        backfill_legacy_export_logs(session)
        session.commit()

    assert _events(client)[0].actor_id is None


def test_per_run_rows_are_not_merged(client):
    _seed(client, logs=[("excel", "succeeded")] * 3)

    with client.session_factory() as session:
        backfill_legacy_export_logs(session)
        session.commit()

    assert len(_events(client)) == 3


def test_channel_mapping_matches_the_history_projection(client):
    _seed(
        client,
        logs=[
            ("mock_sheet", "succeeded"),
            ("google_sheets", "succeeded"),
            ("excel", "succeeded"),
            ("excel_v2", "succeeded"),
        ],
    )

    with client.session_factory() as session:
        backfill_legacy_export_logs(session)
        session.commit()

    channels = sorted(event.channel for event in _events(client))
    assert channels == ["mock_sheet", "sheets", "xlsx", "xlsx"]


def test_unknown_target_type_stops_the_backfill(client):
    """未登记的通道不猜。整批停下比写入一个错误映射好。"""
    _seed(client, logs=[("excel", "succeeded"), ("mystery_channel", "succeeded")])

    with client.session_factory() as session:
        try:
            backfill_legacy_export_logs(session)
        except ValueError as exc:
            assert "mystery_channel" in str(exc)
        else:
            raise AssertionError("未知通道应终止补录")
        session.rollback()

    assert _events(client) == []


def test_failed_legacy_rows_keep_their_status(client):
    _seed(client, logs=[("google_sheets", "failed")])

    with client.session_factory() as session:
        backfill_legacy_export_logs(session)
        session.commit()

    assert _events(client)[0].status == "failed"


def test_backfilled_events_are_visible_in_history_without_duplication(client):
    """补录后历史不能把同一次导出显示成两条。"""
    batch_id, _ = _seed(client, logs=[("excel", "succeeded")])

    with client.session_factory() as session:
        backfill_legacy_export_logs(session)
        session.commit()

    entries = client.get(f"/api/batches/{batch_id}/export-history").json()["entries"]

    assert len(entries) == 1
    assert entries[0]["source"] == "export_event"
