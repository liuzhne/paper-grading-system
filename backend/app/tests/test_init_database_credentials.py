"""初始化凭据：URL 编码的 owner 密码可用，失败路径不向终端泄露凭据。"""

from pathlib import Path
import subprocess
from urllib.parse import quote

from alembic import command
from alembic.config import Config
import psycopg
import pytest
import sqlalchemy
from sqlalchemy.engine import make_url

from backend.app.core.config import settings
from backend.app.scripts import init_database


ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("password", ["Synthetic@Secret", "Synthetic%Secret", "Synthetic/Secret"])
def test_alembic_preserves_encoded_owner_password(monkeypatch, password):
    owner_url = "postgresql+psycopg://postgres.ref:%s@pooler.invalid:5432/postgres" % quote(password, safe="")
    monkeypatch.setattr(settings, "DATABASE_URL", owner_url)
    received = []
    engine = sqlalchemy.create_engine("sqlite+pysqlite:///:memory:")

    def local_engine(config, **_kwargs):
        received.append(config["sqlalchemy.url"])
        return engine

    # 执行真实 Alembic env.py；仅将最终连接替换成内存 SQLite，不访问网络。
    monkeypatch.setattr(sqlalchemy, "engine_from_config", local_engine)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    try:
        command.current(config)
    finally:
        engine.dispose()

    assert received == [owner_url]
    assert make_url(received[0]).password == password


def test_init_rejects_malformed_password_without_echoing_it(monkeypatch, capsys):
    secret = "Synthetic%ZZSecret"
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://postgres:%s@pooler.invalid:5432/postgres" % secret)
    monkeypatch.setattr(init_database, "migration_head", lambda: "test_head")

    def parse_only(url, **_kwargs):
        # 使用真实 libpq URI 解析器复现错误；不创建 socket。
        psycopg.conninfo.conninfo_to_dict(url)
        pytest.fail("无效的 percent escape 必须被拒绝")

    monkeypatch.setattr(psycopg, "connect", parse_only)
    assert init_database.main([]) == 1
    printed = capsys.readouterr()
    assert "ProgrammingError" in printed.err
    assert secret not in printed.out + printed.err
    assert "postgresql://" not in printed.out + printed.err


def test_init_database_error_keeps_sqlstate_without_role_password(monkeypatch, capsys):
    secret = "SyntheticRolePassword2026abcdef"

    def denied(_argv):
        raise psycopg.errors.InsufficientPrivilege("CREATE ROLE pgs_app PASSWORD '%s'" % secret)

    monkeypatch.setattr(init_database, "_main", denied)
    assert init_database.main([]) == 1
    printed = capsys.readouterr()
    assert "SQLSTATE 42501" in printed.err
    assert secret not in printed.out + printed.err


def test_init_migration_failure_never_forwards_child_credentials(monkeypatch, capsys):
    owner_url = "postgresql://postgres.ref:Synthetic%40Secret@pooler.invalid:5432/postgres"

    def failed_run(cmd, **kwargs):
        assert kwargs["capture_output"] is True
        assert kwargs["env"]["DATABASE_URL"] == owner_url.replace("postgresql://", "postgresql+psycopg://", 1)
        assert all("Synthetic" not in item for item in cmd)
        raise subprocess.CalledProcessError(1, cmd, output=owner_url, stderr="password=Synthetic@Secret")

    monkeypatch.setattr(subprocess, "run", failed_run)
    with pytest.raises(SystemExit) as error:
        init_database.run_migrations(owner_url)

    printed = capsys.readouterr()
    output = printed.out + printed.err + str(error.value)
    assert "退出码 1" in output
    assert "Synthetic" not in output
    assert "postgresql://" not in output
