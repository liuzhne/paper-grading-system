"""补录脚本不得默认连到远端库（前端 v2 计划 §12.3 的发布安全边界）。

`backfill_export_events` 是**写**入口。它此前直接用 `settings.DATABASE_URL`，而
`settings` 会加载 `.env.local`——开发机上那通常是生产 Supabase。RUNBOOK 里那条
命令又不带 `DATABASE_URL`，于是「按文档执行一次 dry-run」实际会连上生产库；
去掉 `--dry-run` 就是一次没人授权的生产写入。

本仓库已经为**测试**加过同类防线（conftest 的
`never_let_tests_reach_a_remote_database`），运维脚本却没有。

守卫的形状照搬备份恢复那条约定：远端目标必须**显式确认**，而不是默认放行。
本地库（sqlite / localhost）照常直接跑，否则日常开发会被逼着每次加旗标。
"""

import pytest

from backend.app.scripts import backfill_export_events as script


def test_local_sqlite_needs_no_confirmation():
    assert script.requires_confirmation("sqlite+pysqlite:///tmp/dev.db") is False


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://u:p@localhost:5432/pgs",
        "postgresql+psycopg://u:p@127.0.0.1:5432/pgs",
        # IPv6 在 URL 里必须加方括号。
        "postgresql+psycopg://u:p@[::1]:5432/pgs",
    ],
)
def test_local_postgres_needs_no_confirmation(url):
    assert script.requires_confirmation(url) is False


def test_remote_host_requires_confirmation():
    assert (
        script.requires_confirmation(
            "postgresql+psycopg://u:p@aws-1-ap-southeast-1.pooler.supabase.com/pgs"
        )
        is True
    )


def test_running_against_a_remote_database_is_refused(monkeypatch, capsys):
    """默认拒绝，而不是默认执行。"""
    from backend.app.core.config import settings

    monkeypatch.setattr(
        settings, "DATABASE_URL", "postgresql+psycopg://u:p@db.example.com/pgs"
    )

    assert script.main([]) == 2

    output = capsys.readouterr().out
    assert "db.example.com" in output
    # 报错要给出下一步，否则运维只会去掉 --dry-run 再试一次。
    assert "--i-know-this-is-not-local" in output


def test_dry_run_against_a_remote_database_is_refused_too(monkeypatch):
    """dry-run 也要拦。

    它同样建立连接、同样按 `.env.local` 指向生产；「只读所以没关系」正是让人
    在生产上养成随手执行习惯的那句话。
    """
    from backend.app.core.config import settings

    monkeypatch.setattr(
        settings, "DATABASE_URL", "postgresql+psycopg://u:p@db.example.com/pgs"
    )

    assert script.main(["--dry-run"]) == 2


def test_the_confirmation_flag_lets_a_deliberate_run_through(monkeypatch):
    """显式确认后放行——守卫是防误触，不是禁止运维。"""
    from backend.app.core.config import settings

    monkeypatch.setattr(
        settings, "DATABASE_URL", "postgresql+psycopg://u:p@db.example.com/pgs"
    )
    called = {}

    def _fake_run(dry_run):
        called["dry_run"] = dry_run
        return 0

    monkeypatch.setattr(script, "_run", _fake_run)

    assert script.main(["--dry-run", "--i-know-this-is-not-local"]) == 0
    assert called["dry_run"] is True
