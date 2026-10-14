"""Lightsail 生产部署：健康检查、worker 优雅退出，以及 deploy/production 的配置契约。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from threading import Event
from threading import Thread
import time

from sqlalchemy.exc import OperationalError

from backend.app.db.models import utcnow
from backend.app.services.deployment.readiness import build_health
from backend.app.services.work_queue import runner
from backend.app.services.work_queue import sweep
from backend.app.services.work_queue.limits import SWEEP_INTERVAL_SECONDS
from backend.app.services.work_queue.state import LAST_SWEEP
from backend.app.services.work_queue.state import record_state


ROOT = Path(__file__).resolve().parents[3]
PRODUCTION = ROOT / "deploy" / "production"


def test_health_fails_until_the_worker_has_swept(client):
    response = client.get("/api/system/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "fail",
        "checks": {"database": "pass", "worker": "fail"},
    }
    assert response.headers["cache-control"] == "no-store"


def test_health_passes_with_a_recent_sweep_and_fails_when_it_goes_stale(client):
    record_state(client.session_factory, LAST_SWEEP, utcnow())
    fresh = client.get("/api/system/health")
    assert fresh.status_code == 200
    assert fresh.json() == {"status": "pass", "checks": {"database": "pass", "worker": "pass"}}

    # 没有活动任务也要判失败：worker 空闲时同样每个周期巡检一次。
    stale_at = utcnow() - timedelta(seconds=2 * SWEEP_INTERVAL_SECONDS + 5)
    record_state(client.session_factory, LAST_SWEEP, stale_at)
    stale = client.get("/api/system/health")
    assert stale.status_code == 503
    assert stale.json()["checks"]["worker"] == "fail"


def test_health_reports_database_failure_without_details():
    class BrokenSession:
        def execute(self, *_args, **_kwargs):
            raise OperationalError("SELECT 1", {}, Exception("password=secret host=db"))

        def rollback(self):
            raise OperationalError("ROLLBACK", {}, Exception("connection closed"))

    healthy, payload = build_health(BrokenSession())

    assert healthy is False
    assert payload == {"status": "fail", "checks": {"database": "fail", "worker": "unknown"}}


def test_health_is_public_when_auth_is_enabled(client, monkeypatch):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "Strong-login-password-2026")
    record_state(client.session_factory, LAST_SWEEP, utcnow())

    assert client.get("/api/system/health").status_code == 200


def _patch_loop(monkeypatch, cycle):
    monkeypatch.setattr(runner, "run_worker_cycle", cycle)
    monkeypatch.setattr(sweep, "run_global_sweep", lambda _factory: None)


def test_worker_stops_claiming_and_waits_for_the_item_in_hand(monkeypatch):
    started, release, finished, stop = Event(), Event(), Event(), Event()
    calls = []

    def cycle(_factory, **_options):
        calls.append(time.monotonic())
        if len(calls) == 1:
            started.set()
            release.wait(5)
            finished.set()
            return True
        return False

    _patch_loop(monkeypatch, cycle)
    loop = Thread(
        target=runner.run_worker_loop,
        args=(None,),
        kwargs={
            "poll_seconds": 0.01,
            "stop_event": stop,
            "threads": 1,
            "sweep_seconds": 0.01,
            "drain_seconds": 5,
        },
    )
    loop.start()
    assert started.wait(2)

    stop.set()
    time.sleep(0.2)
    assert loop.is_alive(), "停止后应等在执行的条目做完再返回"
    release.set()
    loop.join(2)

    assert not loop.is_alive()
    assert finished.is_set()
    assert len(calls) == 1, "停止后不应再领取新条目"


def test_worker_gives_up_after_the_drain_window(monkeypatch):
    started, release, stop = Event(), Event(), Event()

    def cycle(_factory, **_options):
        started.set()
        release.wait(5)
        return True

    _patch_loop(monkeypatch, cycle)
    loop = Thread(
        target=runner.run_worker_loop,
        args=(None,),
        kwargs={
            "poll_seconds": 0.01,
            "stop_event": stop,
            "threads": 1,
            "sweep_seconds": 0.01,
            "drain_seconds": 0.1,
        },
    )
    loop.start()
    assert started.wait(2)
    began = time.monotonic()
    stop.set()
    loop.join(2)
    try:
        assert not loop.is_alive()
        assert time.monotonic() - began < 1.5
    finally:
        release.set()


def test_worker_cli_rejects_a_negative_drain_window():
    import pytest

    from backend.app.scripts import run_batch_worker

    with pytest.raises(SystemExit):
        run_batch_worker.main(["--drain-seconds", "-1"])


def _compose():
    import yaml

    return yaml.safe_load((PRODUCTION / "compose.yml").read_text(encoding="utf-8"))


def test_production_compose_runs_app_worker_and_caddy_without_a_database():
    services = _compose()["services"]
    assert set(services) == {"caddy", "app", "worker"}
    app, worker, caddy = services["app"], services["worker"], services["caddy"]

    # 同一镜像、同一份配置；deploy.sh 把要上线的版本打成 current。
    assert app["image"] == worker["image"] == "paper-grading-system:current"
    assert app["env_file"] == worker["env_file"]
    assert app["environment"] == worker["environment"]
    # 生产运行角色没有 DDL：不能沿用 Dockerfile 里先迁移的 CMD。
    assert "alembic" not in " ".join(app["command"])
    assert app["command"][:2] == ["uvicorn", "backend.app.main:app"]
    assert worker["command"] == [
        "python", "-m", "backend.app.scripts.run_batch_worker", "--drain-seconds", "100",
    ]
    assert worker["stop_grace_period"] == "110s"
    # 请求里同步跑整批评分会和 worker 抢条目。
    assert app["environment"]["INLINE_JOB_RUN_ENABLED"] == "false"
    assert app["environment"]["STORAGE_PROVIDER"] == "supabase"
    assert app["environment"]["AUTH_ENABLED"] == "true"
    # L0 缓存在卷里：发版换容器不丢，app 与 worker 共用。
    assert app["environment"]["STORAGE_ROOT"] == "/app/storage"
    assert "app_storage:/app/storage" in app["volumes"]
    assert "app_storage:/app/storage" in worker["volumes"]
    # Caddyfile 挂目录：部署替换文件后 reload 能读到新内容。
    assert "./caddy:/etc/caddy:ro" in caddy["volumes"]
    # app 端口只给 Caddy：信任转发头的前提。
    assert "ports" not in app and "ports" not in worker
    assert sorted(caddy["ports"]) == ["443:443", "443:443/udp", "80:80"]
    # Caddy 拿不到应用密钥。
    assert "env_file" not in caddy
    for service in services.values():
        assert service["restart"] == "unless-stopped"
        assert service["logging"]["options"]["max-size"]


def test_production_caddyfile_keeps_vercel_headers_and_cache_rules():
    caddyfile = (PRODUCTION / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    assert "tls internal" not in caddyfile
    for marker in (
        'Strict-Transport-Security "max-age=31536000; includeSubDomains"',
        'X-Content-Type-Options "nosniff"',
        'X-Frame-Options "SAMEORIGIN"',
        "Content-Security-Policy \"frame-ancestors 'self'\"",
        'Referrer-Policy "same-origin"',
        '"public, max-age=31536000, immutable"',
        '"public, max-age=0, must-revalidate"',
        "encode zstd gzip",
        "lb_try_duration",
        "reverse_proxy app:8000",
    ):
        assert marker in caddyfile, marker


def test_deploy_scripts_are_strict_and_never_print_the_env_file():
    for name in ("bootstrap.sh", "deploy.sh"):
        script = (PRODUCTION / name).read_text(encoding="utf-8")
        assert script.startswith("#!/usr/bin/env bash\n")
        assert "set -euo pipefail" in script
        assert "cat .env" not in script and "cat /opt/pgs/.env" not in script


def _env_script():
    import importlib.util

    spec = importlib.util.spec_from_file_location("production_env", ROOT / "scripts" / "production_env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SECRET = "s3cr3t-value-never-printed"


def _source(tmp_path, **extra):
    values = {
        "DATABASE_URL": "postgresql+psycopg://pgs_app:%s@pooler.supabase.com:6543/postgres" % _SECRET,
        "AUTH_SECRET": _SECRET,
        "AUTH_PASSWORD": _SECRET,
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SECRET_KEY": _SECRET,
        "BYOK_MASTER_KEY": _SECRET,
        "VERCEL_OIDC_TOKEN": "vercel-only",
        "BATCH_SCORING_DISPATCH": "vercel_queue",
        "STORAGE_PROVIDER": "supabase",
        "STORAGE_ROOT": "/tmp/storage",
    }
    values.update(extra)
    path = tmp_path / "source.env"
    path.write_text("".join('%s="%s"\n' % item for item in values.items()), encoding="utf-8")
    return path


def test_production_env_drops_platform_and_compose_managed_variables(tmp_path, capsys):
    script = _env_script()
    output = tmp_path / "pgs.env"

    code = script.main(
        ["--source", str(_source(tmp_path)), "--site-address", "pgs.duckdns.org", "--output", str(output)]
    )

    assert code == 0
    assert output.stat().st_mode & 0o777 == 0o600
    written = output.read_text(encoding="utf-8")
    names = [line.split("=", 1)[0] for line in written.splitlines() if "=" in line]
    assert names[0] == "PGS_SITE_ADDRESS"
    assert set(names) == {
        "PGS_SITE_ADDRESS",
        "DATABASE_URL",
        "AUTH_SECRET",
        "AUTH_PASSWORD",
        "SUPABASE_URL",
        "SUPABASE_SECRET_KEY",
        "BYOK_MASTER_KEY",
    }
    assert "AUTH_SECRET='%s'" % _SECRET in written
    printed = capsys.readouterr()
    assert _SECRET not in printed.out + printed.err
    assert "AUTH_SECRET" in printed.out


def test_production_env_refuses_session_pool_missing_secrets_and_overwrite(tmp_path, capsys):
    script = _env_script()
    output = tmp_path / "pgs.env"
    session_pool = _source(
        tmp_path,
        DATABASE_URL="postgresql+psycopg://pgs_app:%s@pooler.supabase.com:5432/postgres" % _SECRET,
        AUTH_SECRET="",
    )

    code = script.main(["--source", str(session_pool), "--site-address", "pgs.duckdns.org", "--output", str(output)])

    assert code == 1
    assert not output.exists()
    errors = capsys.readouterr().err
    assert "6543" in errors and "AUTH_SECRET" in errors
    assert _SECRET not in errors

    output.write_text("existing", encoding="utf-8")
    import pytest

    with pytest.raises(SystemExit):
        script.main(["--source", str(_source(tmp_path)), "--site-address", "pgs.duckdns.org", "--output", str(output)])
    assert output.read_text(encoding="utf-8") == "existing"


def test_production_env_rejects_values_compose_cannot_quote(tmp_path, capsys):
    script = _env_script()
    output = tmp_path / "pgs.env"
    quoted = _source(tmp_path, LANGFUSE_SECRET_KEY="it's-%s" % _SECRET)

    assert script.main(["--source", str(quoted), "--site-address", "pgs.duckdns.org", "--output", str(output)]) == 1
    assert not output.exists()
    errors = capsys.readouterr().err
    assert "LANGFUSE_SECRET_KEY" in errors and _SECRET not in errors


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _FakeConn:
    """记录执行过的 SQL；按语句开头返回预设结果。"""

    def __init__(self, *, role=False, tables=("alembic_version", "papers"), policies=(), storage=True, bucket=None):
        self.role, self.tables, self.policies = role, list(tables), set(policies)
        self.storage, self.bucket = storage, bucket
        self.statements = []

    def execute(self, query, params=None):
        text = query if isinstance(query, str) else query.as_string(None)
        self.statements.append(text)
        if text.startswith("SELECT 1 FROM pg_roles"):
            return _Rows([(1,)] if self.role else [])
        if text.startswith("SELECT tablename FROM pg_tables"):
            return _Rows([(name,) for name in self.tables])
        if text.startswith("SELECT 1 FROM pg_policies"):
            return _Rows([(1,)] if params[0] in self.policies else [])
        if text.startswith("SELECT to_regclass"):
            return _Rows([("storage.buckets" if self.storage else None,)])
        if text.startswith("SELECT public FROM storage.buckets"):
            return _Rows([] if self.bucket is None else [(self.bucket,)])
        return _Rows([])


def _init_script():
    from backend.app.scripts import init_database

    return init_database


def test_init_creates_the_runtime_role_once_without_printing_the_password(capsys):
    script = _init_script()
    password = "A1b2C3d4E5f6G7h8J9k0L1m2N3"
    conn = _FakeConn()

    assert script.ensure_runtime_role(conn, lambda: password) is True
    create = [s for s in conn.statements if s.startswith("CREATE ROLE")]
    assert create == [
        'CREATE ROLE "pgs_app" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
        "NOREPLICATION NOBYPASSRLS PASSWORD '%s'" % password
    ]
    assert password not in capsys.readouterr().out

    existing = _FakeConn(role=True)
    asked = []
    assert script.ensure_runtime_role(existing, lambda: asked.append(1) or password) is False
    assert asked == [], "角色已存在时不该再要密码，也不改密码"
    assert not any(s.startswith("CREATE ROLE") for s in existing.statements)


def test_init_rejects_passwords_that_break_connection_strings():
    import pytest

    script = _init_script()
    for weak in ("short1", "has%percent-and-dash-1234567890", "a" * 23):
        with pytest.raises(SystemExit):
            script.ensure_runtime_role(_FakeConn(), lambda weak=weak: weak)


def test_init_grants_every_table_like_the_0023_migrations():
    script = _init_script()
    conn = _FakeConn(tables=("alembic_version", "papers", "rubrics"), policies=("rubrics",))

    assert script.grant_runtime_access(conn) == 3
    statements = conn.statements
    assert 'GRANT USAGE ON SCHEMA public TO "pgs_app"' in statements
    assert 'GRANT SELECT ON TABLE "public"."alembic_version" TO "pgs_app"' in statements
    assert 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "public"."papers" TO "pgs_app"' in statements
    for table in ("alembic_version", "papers", "rubrics"):
        assert 'ALTER TABLE "public"."%s" ENABLE ROW LEVEL SECURITY' % table in statements
    policies = [s for s in statements if s.startswith("CREATE POLICY")]
    # rubrics 已有策略：重跑不报 “already exists”。
    assert policies == [
        'CREATE POLICY "pgs_app_dml" ON "public"."alembic_version" FOR ALL TO "pgs_app" USING (true) WITH CHECK (true)',
        'CREATE POLICY "pgs_app_dml" ON "public"."papers" FOR ALL TO "pgs_app" USING (true) WITH CHECK (true)',
    ]
    assert 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "pgs_app"' in statements


def test_init_bucket_is_private_created_once_and_skipped_outside_supabase():
    import pytest

    script = _init_script()
    assert script.ensure_private_bucket(_FakeConn(storage=False), "b") == "skipped"
    created = _FakeConn()
    assert script.ensure_private_bucket(created, "b") == "created"
    assert any(s.startswith("INSERT INTO storage.buckets") and "false" in s for s in created.statements)
    assert script.ensure_private_bucket(_FakeConn(bucket=False), "b") == "exists"
    with pytest.raises(SystemExit):
        script.ensure_private_bucket(_FakeConn(bucket=True), "b")


def test_init_normalizes_urls_for_psycopg():
    import pytest

    script = _init_script()
    assert script.driver_url("postgresql+psycopg://u:p@h:5432/d") == "postgresql://u:p@h:5432/d"
    assert script.driver_url("postgres://u:p@h:5432/d") == "postgresql://u:p@h:5432/d"
    with pytest.raises(SystemExit):
        script.driver_url("sqlite:///x.db")


def test_production_env_from_environment_takes_only_the_allowlist(tmp_path, monkeypatch, capsys):
    # 部署任务的环境里有 GITHUB_TOKEN 等无关变量：只取名单里的，写进实例的 .env。
    script = _env_script()
    for name, value in {
        "DATABASE_URL": "postgresql://pgs_app.ref:%s@pooler.supabase.com:6543/postgres" % _SECRET,
        "AUTH_SECRET": _SECRET,
        "AUTH_PASSWORD": _SECRET,
        "BYOK_MASTER_KEY": _SECRET,
        "SUPABASE_URL": "https://new.supabase.co",
        "SUPABASE_SECRET_KEY": _SECRET,
        "AUTH_USERNAME": "admin",
        "SUPABASE_STORAGE_BUCKET": "",
        "GITHUB_TOKEN": "ghs_never_copied",
    }.items():
        monkeypatch.setenv(name, value)
    output = tmp_path / "pgs.env"

    assert script.main(["--from-environment", "--site-address", "pgs.duckdns.org", "--output", str(output)]) == 0
    written = output.read_text(encoding="utf-8")
    assert "GITHUB_TOKEN" not in written and "ghs_never_copied" not in written
    # 空值（GitHub 上没设的变量）不写，应用用默认值。
    assert "SUPABASE_STORAGE_BUCKET" not in written
    assert "AUTH_USERNAME='admin'" in written
    printed = capsys.readouterr()
    assert _SECRET not in printed.out + printed.err

    monkeypatch.delenv("BYOK_MASTER_KEY")
    output.unlink()
    assert script.main(["--from-environment", "--site-address", "pgs.duckdns.org", "--output", str(output)]) == 1
    assert "BYOK_MASTER_KEY" in capsys.readouterr().err
    assert not output.exists()


def test_production_env_rejects_the_prisma_pgbouncer_flag(tmp_path, capsys):
    # 2026-10-14：从 Supabase 复制了给 Prisma 的事务池连接串，psycopg 报
    # invalid connection option "pgbouncer"。部署前就要拦下，并且不回显连接串。
    script = _env_script()
    output = tmp_path / "pgs.env"
    prisma = _source(
        tmp_path,
        DATABASE_URL="postgresql://pgs_app.ref:%s@pooler.supabase.com:6543/postgres?pgbouncer=true" % _SECRET,
    )

    assert script.main(["--source", str(prisma), "--site-address", "pgs.duckdns.org", "--output", str(output)]) == 1
    errors = capsys.readouterr().err
    assert "pgbouncer=true" in errors and _SECRET not in errors
    assert not output.exists()
