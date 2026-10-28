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


def test_production_compose_runs_app_worker_caddy_and_a_private_database():
    services = _compose()["services"]
    assert set(services) == {"caddy", "app", "worker", "db", "backup"}
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
    # 文件存哪里跟着数据位置走，由 production_env.py 写进 .env；compose 里写死会盖掉它。
    assert "STORAGE_PROVIDER" not in app["environment"]
    assert app["environment"]["AUTH_ENABLED"] == "true"
    # 直连 db 用连接池：app 与 worker 各最多 20 条，小于 db 的 max_connections。
    pool = int(app["environment"]["DB_POOL_SIZE"]) + int(app["environment"]["DB_MAX_OVERFLOW"])
    assert 2 * pool < 50 and "max_connections=50" in services["db"]["command"]
    for service in (app, worker):
        assert service["depends_on"]["db"]["condition"] == "service_healthy"
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


def test_production_database_is_private_and_only_db_and_backup_read_its_password():
    services = _compose()["services"]
    db, backup = services["db"], services["backup"]

    # 大版本与镜像里的 postgresql-client、CI 的迁移门禁相同；Debian 镜像，排序规则与 CI 一致。
    assert db["image"] == "postgres:16-bookworm"
    assert "16" in (ROOT / "Dockerfile").read_text(encoding="utf-8").split("POSTGRES_MAJOR=")[1][:3]
    # 不对外开端口：GitHub runner 与公网都连不到，迁移与切换在实例上用一次性容器执行。
    assert "ports" not in db and "ports" not in backup
    assert db["environment"] == {"POSTGRES_USER": "postgres", "POSTGRES_DB": "pgs"}
    assert "pg_data:/var/lib/postgresql/data" in db["volumes"]
    assert "pg_isready" in " ".join(db["healthcheck"]["test"])
    # 超级用户密码只在 db.env：app 与 worker 读 .env，拿不到它；backup 只读 db.env，拿不到应用密钥。
    assert db["env_file"] == "db.env" and backup["env_file"] == "db.env"
    for name in ("app", "worker"):
        assert services[name]["env_file"] == ".env"
    # 备份：与线上同一镜像，存储卷只读，备份进单独的卷（宿主机目录归 deploy 用户，容器未必写得进）。
    assert backup["image"] == "paper-grading-system:current"
    assert backup["command"][:3] == ["python", "-m", "backend.app.scripts.nightly_backup"]
    assert "app_storage:/app/storage:ro" in backup["volumes"] and "backups:/backups" in backup["volumes"]
    assert {"pg_data", "backups", "app_storage"} <= set(_compose()["volumes"])


def test_deploy_brings_the_database_up_before_switching_the_app():
    script = (PRODUCTION / "deploy.sh").read_text(encoding="utf-8")
    db = script.index("--wait-timeout 120 db")
    app = script.index("--force-recreate --wait --wait-timeout 180 app")
    assert db < app < script.index("--force-recreate backup")
    assert 'for file in .env db.env' in script


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
    for name in ("bootstrap.sh", "deploy.sh", "ops.sh"):
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
        "STORAGE_PROVIDER",
    }
    assert "AUTH_SECRET='%s'" % _SECRET in written
    # 来源里的 STORAGE_PROVIDER 不照抄：按数据位置写（默认 supabase）。
    assert "STORAGE_PROVIDER='supabase'" in written
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


def test_production_env_passes_the_site_leads_switch_through(tmp_path, monkeypatch, capsys):
    """官网申请开关来自 GitHub 仓库变量；没设就不写，应用按默认（关闭）。"""
    script = _env_script()
    for name, value in {
        "DATABASE_URL": "postgresql://pgs_app.ref:%s@pooler.supabase.com:6543/postgres" % _SECRET,
        "AUTH_SECRET": _SECRET,
        "AUTH_PASSWORD": _SECRET,
        "BYOK_MASTER_KEY": _SECRET,
        "SUPABASE_URL": "https://new.supabase.co",
        "SUPABASE_SECRET_KEY": _SECRET,
    }.items():
        monkeypatch.setenv(name, value)
    output = tmp_path / "pgs.env"
    args = ["--from-environment", "--site-address", "pgs.duckdns.org", "--output", str(output)]

    assert script.main(args) == 0
    assert "SITE_LEADS_ENABLED" not in output.read_text(encoding="utf-8")

    output.unlink()
    monkeypatch.setenv("SITE_LEADS_ENABLED", "true")
    assert script.main(args) == 0
    assert "SITE_LEADS_ENABLED='true'" in output.read_text(encoding="utf-8")
    assert "ASSISTANT_AGENT_ENABLED" not in output.read_text(encoding="utf-8")

    output.unlink()
    monkeypatch.setenv("ASSISTANT_AGENT_ENABLED", "true")
    assert script.main(args) == 0
    assert "ASSISTANT_AGENT_ENABLED='true'" in output.read_text(encoding="utf-8")
    monkeypatch.delenv("ASSISTANT_AGENT_ENABLED")

    # yes / on 应用也认，但仓库变量一眼看不出开没开：只收 true / false。
    output.unlink()
    monkeypatch.setenv("SITE_LEADS_ENABLED", "yes")
    assert script.main(args) == 1
    assert "SITE_LEADS_ENABLED 只能是 true 或 false" in capsys.readouterr().err
    assert not output.exists()


def test_lightsail_deploy_job_renders_the_site_leads_switch():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    lightsail = workflow[workflow.index("  deploy-lightsail-production:"):]

    assert "SITE_LEADS_ENABLED: ${{ vars.SITE_LEADS_ENABLED }}" in lightsail


def test_lightsail_deploy_checks_the_assistant_eval_gate_only_when_the_agent_is_on():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    lightsail = workflow[workflow.index("  deploy-lightsail-production:"):]
    build = lightsail.index("- name: Build the application image")
    gate = lightsail.index("- name: Check the assistant eval gate")
    ship = lightsail.index("- name: Ship the image and deployment files")
    assert build < gate < ship  # 用刚构建的镜像算指纹；不过就不往实例上送
    step = lightsail[gate:ship]
    assert "if: vars.ASSISTANT_AGENT_ENABLED == 'true'" in step
    assert "python -m backend.app.scripts.assistant_eval_gate /reports" in step and "--network none" in step


def test_the_assistant_eval_can_run_on_a_candidate_image_without_deploying_it():
    workflow = (ROOT / ".github" / "workflows" / "assistant-eval.yml").read_text(encoding="utf-8")
    build = workflow[workflow.index("- name: Build and ship the candidate image"):workflow.index("- name: Run on the instance")]
    assert "if: inputs.candidate" in build and "paper-grading-system:candidate-${{ github.sha }}" in build
    assert "docker tag" not in build and "deploy.sh" not in build  # 不切 current，不部署
    run = workflow[workflow.index("- name: Run on the instance"):workflow.index("- name: Remove the candidate image")]
    assert "--file compose.yml --file $override" in run and 'commit="$GITHUB_SHA"' in run
    cleanup = workflow[workflow.index("- name: Remove the candidate image"):workflow.index("- name: Render the report")]
    assert "if: always() && inputs.candidate" in cleanup and "docker image rm" in cleanup


_APP_PASSWORD = "A" * 12 + "b" * 12 + "123"
_ROOT_PASSWORD = "R" * 20 + "9" * 8


def _local_source(tmp_path, **extra):
    """切换后：没有 Supabase 的变量也能生成；GitHub 上旧的 RUNTIME_DATABASE_URL 还在也不会写进去。"""

    values = {
        "AUTH_SECRET": _SECRET,
        "AUTH_PASSWORD": _SECRET,
        "BYOK_MASTER_KEY": _SECRET,
        "PGS_APP_PASSWORD": _APP_PASSWORD,
        "POSTGRES_PASSWORD": _ROOT_PASSWORD,
        "RUNTIME_DATABASE_URL": "postgresql://pgs_app.ref:%s@pooler.supabase.com:6543/postgres" % _SECRET,
        "SUPABASE_URL": "https://old.supabase.co",
        "SUPABASE_SECRET_KEY": _SECRET,
    }
    values.update(extra)
    path = tmp_path / "local.env"
    path.write_text("".join('%s="%s"\n' % item for item in values.items()), encoding="utf-8")
    return path


def _written(path):
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)


def test_local_app_env_points_at_the_db_container_and_carries_no_database_passwords(tmp_path, capsys):
    script = _env_script()
    output = tmp_path / "pgs.env"

    code = script.main([
        "--source", str(_local_source(tmp_path)), "--data-plane", "local",
        "--site-address", "pgs.duckdns.org", "--output", str(output),
    ])

    assert code == 0
    written = _written(output)
    assert written["DATABASE_URL"] == "'postgresql+psycopg://pgs_app:%s@db:5432/pgs'" % _APP_PASSWORD
    assert written["STORAGE_PROVIDER"] == "'local'"
    # 超级用户密码、运行角色密码本身、旧库连接串与 Supabase 的变量都不进 app 的 .env。
    for name in ("POSTGRES_PASSWORD", "PGS_APP_PASSWORD", "RUNTIME_DATABASE_URL", "SUPABASE_URL", "SUPABASE_SECRET_KEY"):
        assert name not in written, name
    assert _ROOT_PASSWORD not in output.read_text(encoding="utf-8")
    printed = capsys.readouterr()
    assert _SECRET not in printed.out + printed.err and _APP_PASSWORD not in printed.out
    assert "数据位置 local" in printed.out


def test_local_env_requires_a_plain_database_password(tmp_path, capsys):
    script = _env_script()
    output = tmp_path / "pgs.env"
    args = ["--data-plane", "local", "--site-address", "pgs.duckdns.org", "--output", str(output)]

    assert script.main(["--source", str(_local_source(tmp_path, PGS_APP_PASSWORD="short-and:odd")), *args]) == 1
    errors = capsys.readouterr().err
    assert "PGS_APP_PASSWORD 只能是字母和数字，至少 24 位" in errors and "short-and:odd" not in errors
    assert not output.exists()

    assert script.main(["--source", str(_local_source(tmp_path, PGS_APP_PASSWORD="")), *args]) == 1
    assert "缺少必需变量 PGS_APP_PASSWORD" in capsys.readouterr().err


def test_db_env_has_only_the_superuser_password_and_the_data_plane(tmp_path, capsys):
    script = _env_script()
    output = tmp_path / "db.env"

    assert script.main(["--source", str(_local_source(tmp_path)), "--kind", "db", "--output", str(output)]) == 0
    assert _written(output) == {"POSTGRES_PASSWORD": "'%s'" % _ROOT_PASSWORD, "PGS_DATA_PLANE": "'supabase'"}
    assert output.stat().st_mode & 0o777 == 0o600
    assert _ROOT_PASSWORD not in capsys.readouterr().out

    output.unlink()
    assert script.main([
        "--source", str(_local_source(tmp_path, POSTGRES_PASSWORD="")), "--kind", "db", "--output", str(output),
    ]) == 1
    assert "缺少必需变量 POSTGRES_PASSWORD" in capsys.readouterr().err

    import pytest

    with pytest.raises(SystemExit):  # 只有 app 有站点地址
        script.main(["--source", str(_local_source(tmp_path)), "--kind", "db", "--site-address", "pgs.duckdns.org",
                     "--output", str(output)])


def test_ops_env_is_unquoted_and_switches_connections_with_the_data_plane(tmp_path):
    script = _env_script()
    output = tmp_path / "ops.env"

    assert script.main([
        "--source", str(_local_source(tmp_path)), "--kind", "ops", "--data-plane", "local", "--with-source",
        "--output", str(output),
    ]) == 0
    written = _written(output)
    # docker run --env-file 不处理引号：引号会变成值的一部分。
    assert written == {
        "MIGRATION_DATABASE_URL": "postgresql+psycopg://postgres:%s@db:5432/pgs" % _ROOT_PASSWORD,
        "RUNTIME_DATABASE_URL": "postgresql+psycopg://pgs_app:%s@db:5432/pgs" % _APP_PASSWORD,
        "PGS_APP_PASSWORD": _APP_PASSWORD,
        # 切换时 GitHub 上的 RUNTIME_DATABASE_URL 还指向 Supabase：复制的源库。
        "SOURCE_DATABASE_URL": "postgresql://pgs_app.ref:%s@pooler.supabase.com:6543/postgres" % _SECRET,
    }

    output.unlink()
    supabase = _local_source(tmp_path, MIGRATION_DATABASE_URL="postgresql://postgres.ref:x@pooler.supabase.com:5432/postgres")
    assert script.main(["--source", str(supabase), "--kind", "ops", "--output", str(output)]) == 0
    assert set(_written(output)) == {"MIGRATION_DATABASE_URL", "RUNTIME_DATABASE_URL"}


def test_ops_env_refuses_a_source_outside_the_cutover(tmp_path, capsys):
    script = _env_script()
    output = tmp_path / "ops.env"

    assert script.main([
        "--source", str(_local_source(tmp_path)), "--kind", "ops", "--with-source", "--output", str(output),
    ]) == 1
    assert "--with-source 只用于切换到本机数据库" in capsys.readouterr().err
    assert script.main([
        "--source", str(_local_source(tmp_path, RUNTIME_DATABASE_URL="")), "--kind", "ops", "--data-plane", "local",
        "--with-source", "--output", str(output),
    ]) == 1
    assert "RUNTIME_DATABASE_URL" in capsys.readouterr().err
    assert not output.exists()


def test_lightsail_deploy_renders_both_env_files_for_the_instance_data_plane():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    lightsail = workflow[workflow.index("  deploy-lightsail-production:"):]
    read = lightsail.index("- name: Read where the data lives")
    write = lightsail.index("- name: Write the runtime environment")
    switch = lightsail.index("- name: Switch to this version")
    assert read < write < switch
    assert "cat /opt/pgs/DATA_PLANE" in lightsail[read:write]
    step = lightsail[write:switch]
    assert step.count('--data-plane "$DATA_PLANE"') == 2 and "--kind db" in step
    assert "/opt/pgs/db.env" in step
    assert "POSTGRES_PASSWORD: ${{ secrets.POSTGRES_PASSWORD }}" in step
    assert "ops.sh" in lightsail[lightsail.index("- name: Ship the image and deployment files"):write]
