"""有数据时拒绝有损降级（前端 v2 计划 §7）。

§7 对每个迁移的验证要求里写着「**有数据时拒绝 lossy downgrade**」。0024 做了，
0025–0028 没有：降级会**静默删掉**复核原因、幂等回执与导出审计事件。

这几列不是缓存，重建不回来：
- `review_reasons` / `review_reason` 是复核者当时看到的理由；
- `review_command_receipts` 是批量采纳的幂等回执，丢了会让一次重放变成二次写入；
- `export_events` 是导出审计，丢了就没有「谁在什么时候导出了什么」。

空库降级仍然允许——回滚一个刚上线还没产生数据的版本是正常操作，把它也堵死会
逼人去手工删表。
"""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def _config(url):
    config = Config()
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture()
def at_head(tmp_path, monkeypatch):
    """升到 head 的一次性库。"""
    from backend.app.core.config import settings

    url = "sqlite+pysqlite:///%s" % (tmp_path / "m.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    command.upgrade(_config(url), "head")
    return url


def _downgrade(url, revision):
    command.downgrade(_config(url), revision)


@pytest.mark.parametrize(
    "revision,insert",
    [
        (
            "0027_export_events",
            "INSERT INTO export_events "
            "(id, channel, scope, status, legacy_log_id, created_at) "
            "VALUES ('e1', 'xlsx', 'batch', 'generated', 'legacy-1', "
            "'2026-09-08 00:00:00')",
        ),
    ],
)
def test_downgrade_refuses_when_the_backfill_would_be_lost(at_head, revision, insert):
    """0028：补录出来的 legacy_log_id 丢了，重新前进会二次补录。"""
    engine = create_engine(at_head)
    with engine.begin() as conn:
        conn.execute(text(insert))

    with pytest.raises(Exception) as excinfo:
        _downgrade(at_head, revision)

    assert "export_event" in str(excinfo.value).lower() or "1" in str(excinfo.value)


def test_export_events_downgrade_refuses_with_rows(at_head):
    engine = create_engine(at_head)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO export_events "
                "(id, channel, scope, status, created_at) "
                "VALUES ('e2', 'xlsx', 'batch', 'generated', "
                "'2026-09-08 00:00:00')"
            )
        )

    with pytest.raises(Exception):
        _downgrade(at_head, "0026_review_command_receipts")


def test_receipts_downgrade_refuses_with_rows(at_head):
    engine = create_engine(at_head)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO review_command_receipts "
                "(id, organization_id, actor_id, command, idempotency_key, "
                "payload_hash, result, created_at) "
                "VALUES ('r1', 'org', 'actor', 'accept', 'k1', 'd1', "
                "'{}', '2026-09-08 00:00:00')"
            )
        )

    with pytest.raises(Exception):
        _downgrade(at_head, "0025_review_contract")


def test_review_contract_downgrade_refuses_with_recorded_reasons(at_head):
    """用 ORM 造这一行：score_items 的必填列不少，手写 INSERT 会随模型演进而腐坏。"""
    from sqlalchemy.orm import Session

    from backend.app.db import models

    engine = create_engine(at_head)
    with Session(engine) as session:
        session.add(
            models.ScoreItem(
                scoring_run_id="run",
                criterion_id="crit",
                max_score=10,
                # ck_score_items_aggregation_state：aggregation 为空时必须有 ai_score。
                ai_score=8,
                final_score=8,
                evidence_sufficient=True,
                reason="理由",
                deductions=[],
                deduction_items=[],
                evidence=[],
                review_reason="证据不足",
            )
        )
        session.commit()

    with pytest.raises(Exception) as excinfo:
        _downgrade(at_head, "0024_batch_status_machine")

    assert "复核原因" in str(excinfo.value)


def test_an_empty_database_can_still_be_rolled_back(at_head):
    """刚上线还没产生数据时回滚是正常操作，不能一并堵死。"""
    _downgrade(at_head, "0024_batch_status_machine")

    engine = create_engine(at_head)
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }

    assert "export_events" not in tables
    assert "review_command_receipts" not in tables
