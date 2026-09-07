"""0024 的存量普查与 fail-closed 行为（前端 v2 计划 §7）。

pytest 主体走 `create_all` 不经 Alembic，因此迁移必须单独验证。
重点：**未知阶段值必须终止迁移**，不能统一改写成 draft——一个被悄悄重置为
draft 的批次会丢掉「已评分且已复核」这个事实。
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from backend.app.core.config import settings


ROOT = Path(__file__).resolve().parents[3]

#: 刻意用可识别的名字：批次名带院系与课程信息，不得进入迁移的报错。
BATCH_NAME = "计算机学院-2026届-机密批次名"


def _alembic_config():
    """`alembic/env.py` 无条件用 settings.DATABASE_URL 覆盖 sqlalchemy.url。

    因此隔离**必须**打在 settings 上（见 sqlite_url fixture）；在 Config 上
    set_main_option 是无效的，会让迁移打到 .env.local 指向的真实库。
    """
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _upgrade_to(url, revision):
    assert settings.DATABASE_URL == url, "settings 未被隔离，迁移会打到真实库"
    command.upgrade(_alembic_config(), revision)


def _seed_batch(engine, *, status):
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO rubrics (id, name, version, total_score, status, "
                "created_at) VALUES "
                "('r-0024', 'rubric', 'v1', 100, 'draft', :now)"
            ),
            {"now": "2026-09-07 00:00:00"},
        )
        connection.execute(
            sa.text(
                "INSERT INTO grading_batches (id, name, rubric_id, status, "
                "created_at, updated_at) VALUES "
                "('b-0024', :batch_name, 'r-0024', :status, :now, :now)"
            ),
            {
                "status": status,
                "now": "2026-09-07 00:00:00",
                "batch_name": BATCH_NAME,
            },
        )


@pytest.fixture
def sqlite_url(tmp_path, monkeypatch):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "m0024.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    return url


def test_upgrade_adds_state_version_defaulting_to_one(sqlite_url):
    _upgrade_to(sqlite_url, "0023_rule_scoring_review_tasks")
    engine = sa.create_engine(sqlite_url)
    _seed_batch(engine, status="scored")

    _upgrade_to(sqlite_url, "0024_batch_status_machine")

    with engine.begin() as connection:
        row = connection.execute(
            sa.text("SELECT status, state_version FROM grading_batches WHERE id='b-0024'")
        ).mappings().one()
    assert row["status"] == "scored"
    assert row["state_version"] == 1


@pytest.mark.parametrize(
    "legacy, expected", [("active", "scoring"), ("completed", "scored")]
)
def test_documented_legacy_values_are_renamed_not_reset(sqlite_url, legacy, expected):
    _upgrade_to(sqlite_url, "0023_rule_scoring_review_tasks")
    engine = sa.create_engine(sqlite_url)
    _seed_batch(engine, status=legacy)

    _upgrade_to(sqlite_url, "0024_batch_status_machine")

    with engine.begin() as connection:
        status = connection.execute(
            sa.text("SELECT status FROM grading_batches WHERE id='b-0024'")
        ).scalar_one()
    assert status == expected


def test_unknown_stage_value_fails_closed_without_touching_data(sqlite_url):
    _upgrade_to(sqlite_url, "0023_rule_scoring_review_tasks")
    engine = sa.create_engine(sqlite_url)
    _seed_batch(engine, status="mystery_stage")

    with pytest.raises(Exception) as excinfo:
        _upgrade_to(sqlite_url, "0024_batch_status_machine")

    # 报告只含值与计数，不含批次名/ID（携带院系与课程信息）。
    message = str(excinfo.value)
    assert "mystery_stage" in message
    assert "b-0024" not in message
    assert BATCH_NAME not in message

    with engine.begin() as connection:
        status = connection.execute(
            sa.text("SELECT status FROM grading_batches WHERE id='b-0024'")
        ).scalar_one()
        columns = {
            row["name"]
            for row in connection.execute(
                sa.text("PRAGMA table_info(grading_batches)")
            ).mappings()
        }
    assert status == "mystery_stage", "失败的迁移不得改写数据"
    assert "state_version" not in columns, "失败的迁移不得留下半应用的结构"


def test_constraint_rejects_an_out_of_vocabulary_stage(sqlite_url):
    _upgrade_to(sqlite_url, "0024_batch_status_machine")
    engine = sa.create_engine(sqlite_url)

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO rubrics (id, name, version, total_score, status, "
                "created_at) VALUES "
                "('r-ck', 'rubric', 'v1', 100, 'draft', :now)"
            ),
            {"now": "2026-09-07 00:00:00"},
        )

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO grading_batches (id, name, rubric_id, status, "
                    "state_version, created_at, updated_at) VALUES "
                    "('b-ck', 'batch', 'r-ck', 'not_a_stage', 1, :now, :now)"
                ),
                {"now": "2026-09-07 00:00:00"},
            )


def test_downgrade_removes_the_guards(sqlite_url):
    _upgrade_to(sqlite_url, "0024_batch_status_machine")

    command.downgrade(_alembic_config(), "0023_rule_scoring_review_tasks")

    engine = sa.create_engine(sqlite_url)
    with engine.begin() as connection:
        columns = {
            row["name"]
            for row in connection.execute(
                sa.text("PRAGMA table_info(grading_batches)")
            ).mappings()
        }
    assert "state_version" not in columns
