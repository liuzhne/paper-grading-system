"""全新 Supabase 项目的初始化：运行角色、表结构、授权与行级安全、私有存储桶、逐表核对。

方案见 docs/部署平台迁移到AWS方案.md 4.13。只在一个新建的空项目上运行一次；每一步都先
检查再做，中途失败可以直接重跑。

    uv run python -m backend.app.scripts.init_database

连接串与密码从环境变量读，没有就在终端里提示输入（不回显、不进 shell 历史）：

- ``MIGRATION_DATABASE_URL``：项目 owner（``postgres``）的连接串。用 Supabase「Connect →
  Session pooler」那一条（IPv4，端口 5432）；事务池（6543）不支持迁移里的 DDL。
- ``PGS_APP_PASSWORD``：运行角色 ``pgs_app`` 的密码，只在角色不存在时用来创建它。

为什么需要它：0023 之后的迁移会在 ``pgs_app`` 存在时给新表授权、建行级安全；更早的表
（截至 0022 共 38 张）当初是在 SQL 编辑器里手工授权的，迁移里没有。新库只跑迁移，迁移
会成功，应用却会 permission denied。本脚本把那次手工操作固定下来。

脚本只打印步骤与计数，从不打印连接串或密码。
"""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import re
import subprocess
import sys


ROLE = "pgs_app"
POLICY = "pgs_app_dml"
DEFAULT_BUCKET = "paper-grading-private"
# 只用字母和数字：新运行角色的密码可以原样放进连接串，避免手工 URL 编码出错。
# owner 的既有密码允许特殊字符；其连接串仍须按 PostgreSQL URI 规则编码。
_PASSWORD = re.compile(r"^[A-Za-z0-9]{24,}$")
ROOT = Path(__file__).resolve().parents[3]


def _secret(name: str, prompt: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if not sys.stdin.isatty():
        raise SystemExit("缺少环境变量 %s" % name)
    return getpass.getpass(prompt).strip()


def driver_url(url: str) -> str:
    """psycopg 认的 ``postgresql://``；Alembic 走 SQLAlchemy，要 ``postgresql+psycopg://``。"""

    for prefix in ("postgresql+psycopg://", "postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    raise SystemExit("MIGRATION_DATABASE_URL 不是 PostgreSQL 连接串")


def check_password(password: str) -> None:
    if not _PASSWORD.match(password):
        raise SystemExit("PGS_APP_PASSWORD 至少 24 位，只能是字母和数字")


def ensure_runtime_role(conn, password_source) -> bool:
    """角色不存在时创建；已存在时不改密码。返回是否新建。"""

    from psycopg import sql

    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,)).fetchone()
    if exists:
        return False
    password = password_source()
    check_password(password)
    # CREATE ROLE 不接受绑定参数：用 psycopg 在客户端转义成字面量。
    conn.execute(
        sql.SQL(
            "CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS PASSWORD {}"
        ).format(sql.Identifier(ROLE), sql.Literal(password))
    )
    return True


def run_migrations(owner_url: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = "postgresql+psycopg://" + owner_url[len("postgresql://"):]
    try:
        # 失败 traceback 可能包含连接串或 SQL 字面量，不向终端转发子进程输出。
        # 本脚本的步骤输出仍说明正在执行哪一步；详细迁移诊断走已有运维入口。
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "Alembic 迁移失败（退出码 %s）；请核对 owner 连接串、Session pooler（5432）"
            "与 DDL 权限后重试。为保护凭据，未输出原始错误。" % exc.returncode
        ) from None


def public_tables(conn) -> list[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    ).fetchall()
    return [row[0] for row in rows]


def grant_runtime_access(conn) -> int:
    """与 0023 起各迁移的做法一致：DML 授权 + 行级安全 + ``pgs_app_dml`` 策略。

    ``alembic_version`` 只给 SELECT：运行角色复验要读当前版本，但不该能改它。
    """

    from psycopg import sql

    role = sql.Identifier(ROLE)
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    tables = public_tables(conn)
    for table in tables:
        name = sql.Identifier("public", table)
        privileges = "SELECT" if table == "alembic_version" else "SELECT, INSERT, UPDATE, DELETE"
        conn.execute(
            sql.SQL("GRANT {} ON TABLE {} TO {}").format(sql.SQL(privileges), name, role)
        )
        conn.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(name))
        has_policy = conn.execute(
            "SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = %s "
            "AND policyname = %s",
            (table, POLICY),
        ).fetchone()
        if not has_policy:
            conn.execute(
                sql.SQL(
                    "CREATE POLICY {} ON {} FOR ALL TO {} USING (true) WITH CHECK (true)"
                ).format(sql.Identifier(POLICY), name, role)
            )
    conn.execute(
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role)
    )
    return len(tables)


def ensure_private_bucket(conn, bucket: str) -> str:
    """建私有存储桶；不是 Supabase（没有 storage schema）时跳过。"""

    if conn.execute("SELECT to_regclass('storage.buckets')").fetchone()[0] is None:
        return "skipped"
    row = conn.execute("SELECT public FROM storage.buckets WHERE id = %s", (bucket,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO storage.buckets (id, name, public) VALUES (%s, %s, false)",
            (bucket, bucket),
        )
        return "created"
    if row[0]:
        raise SystemExit("存储桶 %s 是公开的：论文文件必须放在私有桶里" % bucket)
    return "exists"


def verify(conn, expected_head: str) -> list[str]:
    problems = []
    version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    if version is None or version[0] != expected_head:
        problems.append("迁移版本不是 %s" % expected_head)
    for table in public_tables(conn):
        privileges = ["SELECT"] if table == "alembic_version" else ["SELECT", "INSERT", "UPDATE", "DELETE"]
        # 逐项检查：has_table_privilege 传逗号分隔的多项时，任一项有就返回 true。
        rls, granted, policy = conn.execute(
            "SELECT c.relrowsecurity, "
            "(SELECT bool_and(has_table_privilege(%s::name, c.oid, p)) FROM unnest(%s::text[]) AS p), "
            "EXISTS (SELECT 1 FROM pg_policies p WHERE p.schemaname = 'public' "
            "AND p.tablename = c.relname AND p.policyname = %s) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = %s",
            (ROLE, privileges, POLICY, table),
        ).fetchone()
        if not (rls and granted and policy):
            problems.append("%s 的授权或行级安全不完整" % table)
    return problems


def migration_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help="私有存储桶名（默认 %(default)s）")
    args = parser.parse_args(argv)

    import psycopg

    owner_url = driver_url(_secret("MIGRATION_DATABASE_URL", "owner 连接串（Session pooler）："))
    head = migration_head()

    with psycopg.connect(owner_url, autocommit=True) as conn:
        created = ensure_runtime_role(
            conn, lambda: _secret("PGS_APP_PASSWORD", "pgs_app 的新密码（24 位以上字母数字）：")
        )
        print("1/5 运行角色 %s：%s" % (ROLE, "已创建" if created else "已存在，未改动密码"))

    print("2/5 迁移到 %s" % head, flush=True)
    run_migrations(owner_url)

    with psycopg.connect(owner_url, autocommit=True) as conn:
        count = grant_runtime_access(conn)
        print("3/5 授权与行级安全：%d 张表" % count)
        bucket = ensure_private_bucket(conn, args.bucket)
        print(
            "4/5 存储桶 %s：%s"
            % (args.bucket, {"created": "已创建（私有）", "exists": "已存在（私有）", "skipped": "不是 Supabase，跳过"}[bucket])
        )
        problems = verify(conn, head)
    if problems:
        for problem in problems:
            print("核对失败：" + problem, file=sys.stderr)
        return 1
    print("5/5 核对通过：%d 张表都已授权、开启行级安全并有 %s 策略" % (count, POLICY))
    return 0


def main(argv=None) -> int:
    import psycopg

    try:
        return _main(argv)
    except psycopg.Error as exc:
        # libpq 的解析错误会回显无效的密码片段；SQL 错误也可能含角色密码。
        # 仅保留固定异常类型和标准 SQLSTATE，不输出原始错误或 traceback。
        detail = type(exc).__name__
        if exc.sqlstate and re.fullmatch(r"[0-9A-Z]{5}", exc.sqlstate):
            detail += "；SQLSTATE " + exc.sqlstate
        print(
            "数据库初始化失败（%s）；请核对 owner 连接串、URL 编码、项目状态和权限后重试。"
            "为保护凭据，未输出原始错误。" % detail,
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
