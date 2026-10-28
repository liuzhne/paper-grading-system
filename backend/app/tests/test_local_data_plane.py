"""迁出 Supabase：本机数据库的复制、备份、迁移脚本与切换工作流（docs/迁出Supabase到单机Docker方案.md）。"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
import yaml

from backend.app.db import models
from backend.app.db.models import Base
from backend.app.scripts import copy_fixed_config
from backend.app.scripts import nightly_backup
from backend.app.scripts import production_migrate
from backend.app.services.deployment.backup import current_migration_head


ROOT = Path(__file__).resolve().parents[3]
CIPHERTEXT = "ciphertext-never-printed"


# ---- 复制固定配置与账号 ----


def test_copied_tables_only_point_at_tables_copied_before_them():
    """外键只能指向排在前面、同样会复制的表：否则新库要么插不进去，要么留下悬空引用。"""

    order = {name: index for index, name in enumerate(copy_fixed_config.TABLES)}
    for name in copy_fixed_config.TABLES:
        table = Base.metadata.tables[name]
        for key in table.foreign_keys:
            target = key.column.table.name
            assert target in order, "%s.%s 指向不复制的 %s" % (name, key.parent.name, target)
            assert order[target] <= order[name], "%s 要排在 %s 后面" % (name, target)
        # 主键都是字符串 UUID：复制时保留原值，没有序列要重置。
        assert [column.type.python_type for column in table.primary_key.columns] == [str]


def _database(path, head: str | None):
    engine = create_engine(path if isinstance(path, str) else "sqlite:///%s" % path)
    Base.metadata.create_all(engine)
    if head:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            connection.execute(text("INSERT INTO alembic_version VALUES (:head)"), {"head": head})
    return engine


def _seed(engine):
    with Session(engine) as session:
        teacher = models.User(id="u1", username="teacher", display_name="老师", password_hash="hash-u1")
        session.add(teacher)
        session.flush()
        org = models.Organization(id="o1", name="学院", created_by="u1")
        session.add(org)
        session.flush()
        session.add_all([
            models.OrganizationMember(id="m1", organization_id="o1", user_id="u1", role="owner"),
            models.OrganizationInvitation(
                id="i1", organization_id="o1", email="new@example.edu", token="token-1",
                expires_at=datetime(2026, 11, 30), created_by="u1",
            ),
            models.PlatformLLMConfig(
                id="p1", provider_type="openai_compatible", base_url="https://example.invalid/v1",
                model_name="gemini-2.5-flash", api_key_ciphertext=CIPHERTEXT, api_key_nonce="n",
                api_key_tag="t", key_last4="abcd", configured_by="u1",
            ),
            models.AIConnection(
                id="c1", organization_id="o1", owner_id="u1", name="Vertex", provider_type="openai_compatible",
                base_url="https://example.invalid/v1", model_name="google/gemini-2.5-flash",
                api_key_ciphertext=CIPHERTEXT, api_key_nonce="n", api_key_tag="t", key_last4="abcd",
            ),
        ])
        session.flush()
        session.add_all([
            models.AssistantPreference(
                id="a1", organization_id="o1", user_id="u1", model_source="connection", ai_connection_id="c1",
            ),
            models.SiteLead(id="l1", kind="trial", email="lead@example.com", source_path="/"),
            # 不迁移的表：审计日志与其它业务数据。
            models.AuditLog(id="log1", event_type="login", event_metadata={}),
        ])
        session.commit()


def _environ(source: Path, target: Path) -> dict:
    return {"SOURCE_DATABASE_URL": "sqlite:///%s" % source, "MIGRATION_DATABASE_URL": "sqlite:///%s" % target}


SEED_USER = copy_fixed_config.SEED_USER_ID
SEED_ORG = copy_fixed_config.SEED_ORGANIZATION_ID


def _seed_like_0022(engine, *, member_ids=()):
    """迁移 0022 在空库上预置的三行；``member_ids`` 是同样加进预置组织的其他用户。"""

    with Session(engine) as session:
        session.add(models.User(id=SEED_USER, username="dev-user", display_name="Bootstrap Admin",
                                role="platform_admin", platform_role="platform_admin"))
        session.flush()
        session.add(models.Organization(id=SEED_ORG, name="Default Organization", created_by=SEED_USER))
        session.flush()
        for user_id in (SEED_USER, *member_ids):
            session.add(models.OrganizationMember(
                id="%s-%s" % (SEED_ORG[:18], user_id[-17:]), organization_id=SEED_ORG, user_id=user_id,
                role="org_admin" if user_id == SEED_USER else "teacher",
            ))
        session.commit()


def test_seed_ids_match_migration_0022():
    source = (ROOT / "alembic" / "versions" / "0022_legacy_tenant_backfill.py").read_text(encoding="utf-8")
    assert 'DEFAULT_USER_ID = "%s"' % SEED_USER in source
    assert 'DEFAULT_ORGANIZATION_ID = "%s"' % SEED_ORG in source


def test_copy_replaces_the_rows_migrations_seed_into_a_fresh_target(tmp_path, capsys):
    """线上就是这样：旧库与新库都由迁移建出，都有 0022 的三行；老师是预置组织的成员。"""

    head = current_migration_head()
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    source_engine = _database(source, head)
    with Session(source_engine) as session:
        session.add(models.User(id="u-teacher", username="liuzhen", display_name="老师", password_hash="hash-t"))
        session.commit()
    _seed_like_0022(source_engine, member_ids=("u-teacher",))
    with source_engine.begin() as connection:  # 旧库里预置组织改过名：复制后以旧库为准
        connection.execute(text("UPDATE organizations SET name = '学院' WHERE id = :id"), {"id": SEED_ORG})
    target_engine = _database(target, head)
    _seed_like_0022(target_engine)

    assert copy_fixed_config.main(["check"], environ=_environ(source, target)) == 0
    assert copy_fixed_config.main(["apply"], environ=_environ(source, target)) == 0

    with Session(target_engine) as session:
        assert session.query(models.User).count() == 2
        assert session.get(models.User, "u-teacher").password_hash == "hash-t"
        assert session.get(models.Organization, SEED_ORG).name == "学院"
        members = {member.user_id for member in session.query(models.OrganizationMember)}
        assert members == {SEED_USER, "u-teacher"}
    assert "已复制 organization_members       2 行" in capsys.readouterr().out


def test_copy_refuses_rows_beyond_the_seed_and_leaves_the_seed_alone(tmp_path, capsys):
    head = current_migration_head()
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _seed(_database(source, head))
    target_engine = _database(target, head)
    _seed_like_0022(target_engine)
    with Session(target_engine) as session:
        session.add(models.User(id="stray", username="stray", display_name="多出来的"))
        session.commit()

    assert copy_fixed_config.main(["apply"], environ=_environ(source, target)) == 1
    errors = capsys.readouterr().err
    assert "迁移预置以外的数据" in errors and "users" in errors
    with Session(target_engine) as session:
        assert session.get(models.User, SEED_USER) is not None
        assert session.query(models.User).count() == 2

    # 检查之后才冒出来的数据：复制事务里再核对一次，整体回滚，预置行也不删。
    engines = copy_fixed_config.make_engine("sqlite:///%s" % source), copy_fixed_config.make_engine("sqlite:///%s" % target)
    with pytest.raises(RuntimeError, match="迁移预置以外"):
        copy_fixed_config.copy_tables(*engines)
    with Session(target_engine) as session:
        assert session.get(models.User, SEED_USER) is not None


def test_copy_keeps_ids_and_password_hashes_and_skips_everything_else(tmp_path, capsys):
    head = current_migration_head()
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _seed(_database(source, head))
    target_engine = _database(target, head)

    assert copy_fixed_config.main(["apply"], environ=_environ(source, target)) == 0

    with Session(target_engine) as session:
        assert session.get(models.User, "u1").password_hash == "hash-u1"
        assert session.get(models.AIConnection, "c1").api_key_ciphertext == CIPHERTEXT
        assert session.get(models.AssistantPreference, "a1").ai_connection_id == "c1"
        assert session.get(models.OrganizationInvitation, "i1").token == "token-1"
        assert session.get(models.SiteLead, "l1").email == "lead@example.com"
        assert session.query(models.AuditLog).count() == 0
    printed = capsys.readouterr()
    assert "已复制 users" in printed.out and "已复制 site_leads" in printed.out
    # 只打印表名与行数：密文、密码哈希、邮箱与连接串都不出现。
    for value in (CIPHERTEXT, "hash-u1", "lead@example.com", str(source)):
        assert value not in printed.out + printed.err


def test_copy_refuses_a_second_run_and_mismatched_versions(tmp_path, capsys):
    head = current_migration_head()
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _seed(_database(source, head))
    _database(target, head)
    environ = _environ(source, target)
    assert copy_fixed_config.main(["apply"], environ=environ) == 0
    capsys.readouterr()

    assert copy_fixed_config.main(["apply"], environ=environ) == 1
    assert "新库这些表有迁移预置以外的数据，拒绝复制" in capsys.readouterr().err

    other = tmp_path / "other.db"
    _database(other, "0040_reasoning_tokens")
    assert copy_fixed_config.main(["check"], environ=_environ(source, other)) == 1
    assert "新库的迁移版本" in capsys.readouterr().err


def test_check_accepts_an_uninitialised_target_but_apply_does_not(tmp_path, capsys):
    source = tmp_path / "source.db"
    target = tmp_path / "empty.db"
    _seed(_database(source, current_migration_head()))
    create_engine("sqlite:///%s" % target).connect().close()  # 刚由 POSTGRES_DB 建出的空库
    environ = _environ(source, target)

    assert copy_fixed_config.main(["check"], environ=environ) == 0
    assert "检查通过（新库待初始化）" in capsys.readouterr().out
    assert copy_fixed_config.main(["apply"], environ=environ) == 1
    assert "先跑 init_database" in capsys.readouterr().err


def test_copy_failure_does_not_echo_the_connection_string(tmp_path, capsys):
    environ = {
        "SOURCE_DATABASE_URL": "postgresql://pgs_app:secret-pass-123@127.0.0.1:1/postgres",
        "MIGRATION_DATABASE_URL": "sqlite:///%s" % (tmp_path / "t.db"),
    }
    assert copy_fixed_config.main(["check"], environ=environ) == 1
    printed = capsys.readouterr()
    assert "secret-pass-123" not in printed.out + printed.err
    assert "未输出原始错误" in printed.err
    assert copy_fixed_config.main(["check"], environ={}) == 2


def test_copy_on_postgres_reads_the_source_read_only(tmp_path):
    """真实 PostgreSQL 上再走一遍（CI 的 postgres 任务设置 URL）：两边都像 init_database 那样跑完全部
    迁移（含 0022 的预置行），再验证只读事务、JSON 等列类型的反射与写入。"""

    import os
    import subprocess
    import sys

    from sqlalchemy.engine import make_url

    raw = os.getenv("PGS_TEST_POSTGRES_URL", "").strip()
    if not raw:
        pytest.skip("PGS_TEST_POSTGRES_URL is not set")
    base = make_url(raw)
    if (base.host or "") not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("PGS_TEST_POSTGRES_URL must point at a local, disposable database")
    names = ("pgs_copy_source", "pgs_copy_target")
    admin = create_engine(base, isolation_level="AUTOCOMMIT")

    def recreate(drop_only=False):
        with admin.connect() as connection:
            for name in names:
                connection.execute(text('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name))
                if not drop_only:
                    connection.execute(text('CREATE DATABASE "%s"' % name))

    recreate()
    urls = [base.set(database=name).render_as_string(hide_password=False) for name in names]
    for url in urls:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT, env=dict(os.environ, DATABASE_URL=url), check=True, capture_output=True,
        )
    engines = [create_engine(url) for url in urls]
    try:
        _seed(engines[0])
        with Session(engines[0]) as session:  # 线上老师就在 0022 预置的组织里
            session.add(models.OrganizationMember(id="m-seed", organization_id=SEED_ORG, user_id="u1", role="teacher"))
            session.commit()
        environ = {"SOURCE_DATABASE_URL": urls[0], "MIGRATION_DATABASE_URL": urls[1]}
        assert copy_fixed_config.main(["apply"], environ=environ) == 0
        with Session(engines[1]) as session:
            assert session.get(models.User, "u1").password_hash == "hash-u1"
            assert session.get(models.User, SEED_USER) is not None
            assert {m.user_id for m in session.query(models.OrganizationMember).filter_by(organization_id=SEED_ORG)} == {
                SEED_USER, "u1"}
            assert session.get(models.AIConnection, "c1").provider_options == {}
            assert session.query(models.AuditLog).count() == 0
        # 源库只在只读事务里读：同一套读法下写入会被拒绝。
        with engines[0].connect() as connection:
            copy_fixed_config._read_only(connection)
            with pytest.raises(Exception, match="read-only"):
                connection.execute(text("UPDATE users SET display_name = 'x'"))
    finally:
        for engine in engines:
            engine.dispose()
        recreate(drop_only=True)
        admin.dispose()


# ---- 每晚备份 ----


def _environ_local(**extra):
    values = {"PGS_DATA_PLANE": "local", "POSTGRES_PASSWORD": "R" * 28}
    values.update(extra)
    return values


def _fake_create(calls):
    def create(**kwargs):
        calls.append(kwargs)
        package = Path(kwargs["destination_root"]) / ("backup-20991231T190000Z-nightly-%04d" % len(calls))
        package.mkdir()
        (package / "manifest.json").write_text("{}", encoding="utf-8")
        return package

    return create


def test_backup_is_skipped_until_the_data_is_local(tmp_path):
    calls = []
    for environ in ({"PGS_DATA_PLANE": "supabase", "POSTGRES_PASSWORD": "x"}, {"PGS_DATA_PLANE": "local"}):
        assert nightly_backup.run_once(tmp_path / "b", tmp_path, environ, create=_fake_create(calls)) is None
    assert calls == []


def test_backup_refuses_when_the_disk_is_nearly_full(tmp_path):
    calls = []
    full = lambda _path: SimpleNamespace(free=1024**3)  # noqa: E731
    assert nightly_backup.run_once(
        tmp_path / "b", tmp_path, _environ_local(), create=_fake_create(calls), disk_usage=full
    ) is None
    assert calls == []


def test_backup_uses_the_superuser_verifies_and_keeps_seven(tmp_path):
    destination = tmp_path / "backups"
    destination.mkdir()
    for day in range(1, 9):
        (destination / ("backup-202610%02dT190000Z-nightly-old" % day)).mkdir()
    (destination / "backup-20261009T190000Z-nightly-x.partial").mkdir()  # 没做完的包不算、不删
    calls, verified = [], []

    package = nightly_backup.run_once(
        destination, tmp_path / "storage", _environ_local(), create=_fake_create(calls),
        verify=verified.append, disk_usage=lambda _path: SimpleNamespace(free=50 * 1024**3),
    )

    assert verified == [package]
    # pg_dump 默认关闭行级安全，pgs_app 绕不过去：用超级用户。
    assert calls[0]["database_url"] == "postgresql://postgres:%s@db:5432/pgs" % ("R" * 28)
    assert calls[0]["migration_head"] == current_migration_head()
    kept = [path.name for path in nightly_backup.packages(destination)]
    assert len(kept) == 7 and package.name in kept
    assert "backup-20261001T190000Z-nightly-old" not in kept and "backup-20261002T190000Z-nightly-old" not in kept
    assert (destination / "backup-20261009T190000Z-nightly-x.partial").is_dir()


def test_backup_runs_at_three_in_the_morning_utc_plus_eight():
    late = datetime(2026, 10, 27, 20, 0, tzinfo=timezone.utc)  # UTC+8 的 28 日 04:00
    early = datetime(2026, 10, 27, 18, 30, tzinfo=timezone.utc)  # UTC+8 的 28 日 02:30
    assert nightly_backup.next_run(late).isoformat() == "2026-10-29T03:00:00+08:00"
    assert nightly_backup.next_run(early).isoformat() == "2026-10-28T03:00:00+08:00"
    assert nightly_backup.next_run(early) - early < timedelta(hours=1)


def test_backup_once_fails_loudly_when_nothing_was_backed_up(tmp_path):
    # 切换工作流靠这个退出码确认新库确实备份过。
    assert nightly_backup.main(["--destination", str(tmp_path), "--once"], environ={"PGS_DATA_PLANE": "supabase"}) == 1


# ---- 迁移的实例侧脚本 ----


def test_migration_steps_each_get_only_their_own_connection(tmp_path):
    seen = []

    def runner(command, env, check):
        seen.append((command, env.get("DATABASE_URL"), {"MIGRATION_DATABASE_URL", "RUNTIME_DATABASE_URL"} & set(env)))
        return SimpleNamespace(returncode=0)

    runtime = tmp_path / "runtime.db"
    Base.metadata.create_all(create_engine("sqlite:///%s" % runtime))
    environ = {"MIGRATION_DATABASE_URL": "postgresql://postgres:owner@db:5432/pgs",
               "RUNTIME_DATABASE_URL": "sqlite:///%s" % runtime}

    assert production_migrate.main(["upgrade"], environ=environ, runner=runner) == 0
    assert production_migrate.main(["verify"], environ=environ, runner=runner) == 0

    upgrade, verify = seen
    assert upgrade[0] == ["alembic", "upgrade", "head"]
    assert upgrade[1] == "postgresql+psycopg://postgres:owner@db:5432/pgs"
    assert verify[0][-1] == "backend.app.scripts.verify_postgres_ops" and verify[1].startswith("sqlite:///")
    # 子进程只拿到 DATABASE_URL：另一个角色的连接串不跟着传下去。
    assert upgrade[2] == verify[2] == set()
    assert production_migrate.main(["survey"], environ={}, runner=runner) == 2


# ---- 连接池 ----


def test_direct_postgres_uses_a_bounded_pool_and_the_transaction_pooler_does_not(monkeypatch):
    from backend.app.core.config import settings
    from backend.app.db.session import engine_options

    monkeypatch.setattr(settings, "DB_POOL_SIZE", 10)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", 10)
    direct = engine_options("postgresql+psycopg://pgs_app:x@db:5432/pgs")
    assert (direct["pool_size"], direct["max_overflow"]) == (10, 10)
    pooler = engine_options("postgresql+psycopg://pgs_app:x@pooler.supabase.com:6543/postgres")
    assert pooler["poolclass"] is NullPool and "pool_size" not in pooler
    assert "pool_size" not in engine_options("sqlite:///x.db")


# ---- 切换工作流 ----


def _cutover():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "production-cutover.yml").read_text(encoding="utf-8"))


def test_cutover_is_manual_approved_and_defaults_to_checking_only():
    workflow = _cutover()
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["dry_run"]["default"] is True and inputs["expected_head"]["required"] is True
    job = workflow["jobs"]["cutover"]
    assert job["environment"] == "production"
    # 与部署任务同一组：切换期间不会有部署在重启服务。
    assert workflow["concurrency"] == {"group": "lightsail-production", "cancel-in-progress": False}
    assert "inputs.dry_run == false || inputs.dry_run == 'false'" in job["env"]["REAL_RUN"]
    guard = {step.get("name"): step for step in job["steps"]}["Refuse to run unless the requested head matches this commit"]
    assert '"$CONFIRM" != "cutover"' in guard["run"] and "${{ inputs.confirm }}" not in guard["run"]


def test_cutover_changes_nothing_before_its_checks_and_runs_in_order():
    steps = _cutover()["jobs"]["cutover"]["steps"]
    names = [step.get("name") for step in steps]
    by_name = {step.get("name"): step for step in steps}
    order = [
        "Check the instance",
        "Render the credentials",
        "Check the source and target databases",
        "Stop the app and worker",
        "Initialise the local database",
        "Copy the fixed configuration",
        "Switch the instance to the local database",
        "Smoke test through the public address",
        "Back up and verify the new database",
    ]
    assert [names.index(name) for name in order] == sorted(names.index(name) for name in order)
    for name in order[3:]:
        assert by_name[name]["if"] == "env.REAL_RUN == 'true'", name
    for name in order[:3]:
        assert "if" not in by_name[name], name
    instance = by_name["Check the instance"]["run"]
    assert 'cat /opt/pgs/RELEASE' in instance and '"$release" != "$GITHUB_SHA"' in instance
    assert "DATA_PLANE" in instance
    assert "--with-source" in by_name["Render the credentials"]["run"]
    switch = by_name["Switch the instance to the local database"]["run"]
    assert switch.index("before-cutover") < switch.index("DATA_PLANE.new") < switch.index("deploy.sh")
    assert "--once" in by_name["Back up and verify the new database"]["run"]
    fallback = by_name["Put the old services back on failure"]
    assert fallback["if"] == "failure() && steps.stop.outputs.stopped == 'true'"
    assert '"$plane" == "local"' in fallback["run"] and "before-cutover" in fallback["run"]
    cleanup = by_name["Remove the credentials and deploy key"]
    assert cleanup["if"] == "always()" and "ops.env" in cleanup["run"] and "pgs_deploy" in cleanup["run"]


def test_cutover_never_uploads_data_or_dumps_the_database():
    text_ = (ROOT / ".github" / "workflows" / "production-cutover.yml").read_text(encoding="utf-8")
    assert "upload-artifact" not in text_ and "pg_dump" not in text_
