"""导出事件与预检（前端 v2 计划 §5-F、§6）。

**旧表原样保留。** `SpreadsheetWriteLog` 的表名、四种 target_type 与既有写入口
都不动；新的 ExportEvent 是并存的事件级记录，旧 `/export-logs` 合同保持兼容。
一次性重命名+回填会让回退窗口内的旧应用写不进日志。

两条口径值得单独说明：
- **生成成功不等于下载完成。** 客户端断开证明不了文件已落地，状态只到
  「已生成」。
- **历史日志不补造操作人。** 旧表没记，就显示「历史记录未记录」，不能拿
  run.owner 冒充——那是评分的所有者，不是点导出的人。
"""

from backend.app.db import models


def _seed_run(client, *, status="scored"):
    with client.session_factory() as session:
        rubric = models.Rubric(name="ex rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(name="ex batch", rubric_id=rubric.id, status=status)
        session.add(batch)
        session.flush()
        paper = models.Paper(
            batch_id=batch.id, file_name="p.pdf", file_path="p.pdf", status="parsed"
        )
        session.add(paper)
        session.flush()
        run = models.ScoringRun(
            paper_id=paper.id, rubric_id=rubric.id, status=status
        )
        session.add(run)
        session.flush()
        criterion = models.RubricCriterion(
            rubric_id=rubric.id, code="C01", name="c", max_score=25, weight=1
        )
        session.add(criterion)
        session.flush()
        session.add(
            models.ScoreItem(
                scoring_run_id=run.id,
                criterion_id=criterion.id,
                max_score=25,
                ai_score=21,
                final_score=21,
                evidence_sufficient=True,
                reason="r",
                deductions=[],
                deduction_items=[],
                evidence=[],
                need_manual_review=False,
            )
        )
        session.commit()
        return batch.id, run.id


# ---------------------------------------------------------------- 预检


def test_precheck_passes_when_everything_is_confirmed(client):
    batch_id, _ = _seed_run(client)

    body = client.get(f"/api/batches/{batch_id}/export-precheck").json()

    assert body["can_export_final"] is True
    assert body["blocking"] == []


def test_pending_confirmation_blocks_final_grades_only(client):
    """未完成复核不阻止中间态报告导出，只阻止正式成绩。"""
    batch_id, run_id = _seed_run(client)
    with client.session_factory() as session:
        import sqlalchemy as sa

        row = session.scalars(sa.select(models.ScoreItem)).first()
        row.need_manual_review = True
        session.commit()

    body = client.get(f"/api/batches/{batch_id}/export-precheck").json()

    assert body["can_export_final"] is False
    assert any(b["code"] == "pending_review" for b in body["blocking"])
    # 报告与结构化审计导出仍可用，只是会标注未完成。
    assert body["can_export_audit"] is True


def test_failed_materials_are_reported_as_a_warning(client):
    batch_id, _ = _seed_run(client)
    with client.session_factory() as session:
        batch = session.get(models.GradingBatch, batch_id)
        paper = models.Paper(
            batch_id=batch.id, file_name="x.pdf", file_path="x.pdf", status="failed"
        )
        session.add(paper)
        session.commit()

    body = client.get(f"/api/batches/{batch_id}/export-precheck").json()

    assert any(w["code"] == "materials_without_results" for w in body["warnings"])


def test_precheck_carries_the_result_revision(client):
    batch_id, _ = _seed_run(client)

    body = client.get(f"/api/batches/{batch_id}/export-precheck").json()

    assert len(body["result_revision"]) >= 16


def test_sheets_availability_reflects_offline_mode(client, monkeypatch):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "OFFLINE_MODE", True)
    batch_id, _ = _seed_run(client)

    body = client.get(f"/api/batches/{batch_id}/export-precheck").json()

    assert body["channels"]["sheets"] is False


# ---------------------------------------------------------------- 历史


def test_history_includes_legacy_spreadsheet_logs(client):
    batch_id, run_id = _seed_run(client)
    with client.session_factory() as session:
        session.add(
            models.SpreadsheetWriteLog(
                scoring_run_id=run_id, target_type="excel", status="succeeded"
            )
        )
        session.commit()

    body = client.get(f"/api/batches/{batch_id}/export-history").json()

    assert len(body["entries"]) == 1
    entry = body["entries"][0]
    assert entry["channel"] == "xlsx"
    assert entry["source"] == "legacy_run_log"


def test_legacy_entries_do_not_fabricate_an_operator(client):
    """旧表没记操作人。拿 run.owner 冒充会把评分者写成导出者。"""
    batch_id, run_id = _seed_run(client)
    with client.session_factory() as session:
        session.add(
            models.SpreadsheetWriteLog(
                scoring_run_id=run_id, target_type="google_sheets", status="succeeded"
            )
        )
        session.commit()

    entry = client.get(f"/api/batches/{batch_id}/export-history").json()["entries"][0]

    assert entry["actor_id"] is None
    assert "未记录" in entry["actor_display"]


def test_legacy_channel_values_are_mapped_but_original_kept(client):
    batch_id, run_id = _seed_run(client)
    with client.session_factory() as session:
        for target in ("mock_sheet", "google_sheets", "excel", "excel_v2"):
            session.add(
                models.SpreadsheetWriteLog(
                    scoring_run_id=run_id, target_type=target, status="succeeded"
                )
            )
        session.commit()

    entries = client.get(f"/api/batches/{batch_id}/export-history").json()["entries"]

    channels = {e["legacy_target_type"]: e["channel"] for e in entries}
    assert channels == {
        "mock_sheet": "mock_sheet",
        "google_sheets": "sheets",
        "excel": "xlsx",
        "excel_v2": "xlsx",
    }


def test_per_run_logs_are_not_merged_into_one_batch_event(client):
    """一份 v1 批次 xlsx 可能有多条 run 日志；按路径或时间强行合并会造假。"""
    batch_id, run_id = _seed_run(client)
    with client.session_factory() as session:
        for _ in range(3):
            session.add(
                models.SpreadsheetWriteLog(
                    scoring_run_id=run_id, target_type="excel", status="succeeded"
                )
            )
        session.commit()

    entries = client.get(f"/api/batches/{batch_id}/export-history").json()["entries"]

    assert len(entries) == 3


def test_new_events_record_the_real_operator(client):
    batch_id, _ = _seed_run(client)
    revision = client.get(f"/api/batches/{batch_id}/export-precheck").json()[
        "result_revision"
    ]

    created = client.post(
        f"/api/batches/{batch_id}/export-events",
        json={"channel": "html_report", "scope": "batch", "result_revision": revision},
    )

    assert created.status_code == 201
    entry = client.get(f"/api/batches/{batch_id}/export-history").json()["entries"][0]
    assert entry["source"] == "export_event"
    assert entry["actor_id"]
    assert entry["channel"] == "html_report"


def test_generated_is_not_reported_as_downloaded(client):
    """客户端断开证明不了文件已落地。"""
    batch_id, _ = _seed_run(client)
    revision = client.get(f"/api/batches/{batch_id}/export-precheck").json()[
        "result_revision"
    ]

    body = client.post(
        f"/api/batches/{batch_id}/export-events",
        json={"channel": "xlsx", "scope": "batch", "result_revision": revision},
    ).json()

    assert body["status"] == "generated"
    assert "downloaded" not in body["status"]


def test_export_event_rejects_a_stale_result_revision(client):
    batch_id, _ = _seed_run(client)

    response = client.post(
        f"/api/batches/{batch_id}/export-events",
        json={"channel": "xlsx", "scope": "batch", "result_revision": "0" * 64},
    )

    assert response.status_code == 409


def test_history_is_bounded(client):
    batch_id, run_id = _seed_run(client)
    with client.session_factory() as session:
        for _ in range(5):
            session.add(
                models.SpreadsheetWriteLog(
                    scoring_run_id=run_id, target_type="excel", status="succeeded"
                )
            )
        session.commit()

    body = client.get(f"/api/batches/{batch_id}/export-history?limit=2").json()

    assert len(body["entries"]) == 2
    assert body["next_cursor"] is not None


def test_legacy_export_logs_endpoint_keeps_its_shape(client):
    """旧客户端仍在用 /export-logs，合同不能变。"""
    batch_id, run_id = _seed_run(client)
    with client.session_factory() as session:
        session.add(
            models.SpreadsheetWriteLog(
                scoring_run_id=run_id, target_type="excel", status="succeeded"
            )
        )
        session.commit()

    body = client.get(f"/api/export-logs?batch_id={batch_id}").json()

    assert body[0]["target_type"] == "excel"
    assert "channel" not in body[0], "旧响应不得被就地改写"
