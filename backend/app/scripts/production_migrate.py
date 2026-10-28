"""生产数据库迁移的实例侧执行：.github/workflows/production-migrate.yml 经 SSH 调用。

数据库改成同机的 db 容器（不对外开端口）之后，GitHub 运行器连不到它。工作流把本提交的镜像
送到实例，用一次性容器（deploy/production/ops.sh）跑这里的子命令。切换前数据还在 Supabase 时，
走的也是这条路：实例连 Supabase 的 Session pooler。

连接串从环境变量读（``scripts/production_env.py --kind ops`` 生成，按实例上的 DATA_PLANE 选）：

- ``MIGRATION_DATABASE_URL``：owner，只用于 survey / pending / upgrade。
- ``RUNTIME_DATABASE_URL``：运行角色 ``pgs_app``，verify 只用它。owner 什么都读得到，证明不了应用
  能不能工作——0026/0027 的新表缺授权，就是只有用运行角色验证才暴露出来（见 0029）。

    python -m backend.app.scripts.production_migrate survey|pending|upgrade|verify

只打印版本、角色名、表名与行数，不打印行内容（生产库含学生 PII），也不打印连接串。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from sqlalchemy import create_engine
from sqlalchemy import func
from sqlalchemy import inspect
from sqlalchemy import select
from sqlalchemy import table
from sqlalchemy import text

from backend.app.db.session import engine_options
from backend.app.services.deployment.postgres_verifier import RUNTIME_ACCESS_TABLES


COMMANDS = ("survey", "pending", "upgrade", "verify")
#: 每个子命令用哪一个连接串。
CREDENTIAL = {
    "survey": "MIGRATION_DATABASE_URL",
    "pending": "MIGRATION_DATABASE_URL",
    "upgrade": "MIGRATION_DATABASE_URL",
    "verify": "RUNTIME_DATABASE_URL",
}


def _url(raw: str) -> str:
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            return "postgresql+psycopg://" + raw[len(prefix) :]
    return raw


def _engine(raw: str):
    url = _url(raw)
    return create_engine(url, **engine_options(url))


def _run(command, url, runner=subprocess.run) -> int:
    # 子进程只拿到这一步该用的连接串：alembic 与 verify_postgres_ops 都读 DATABASE_URL。
    env = dict(os.environ, DATABASE_URL=_url(url))
    for name in CREDENTIAL.values():
        env.pop(name, None)
    return runner(command, env=env, check=False).returncode


def survey(url) -> int:
    engine = _engine(url)
    try:
        with engine.connect() as connection:
            head = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            print("current alembic head:", head)
            print("current_user:", connection.execute(text("SELECT current_user")).scalar_one())
            for name in sorted(inspect(connection).get_table_names(schema="public")):
                count = connection.execute(select(func.count()).select_from(table(name))).scalar_one()
                if count:
                    print("  %-34s %s" % (name, count))
            connection.rollback()
    finally:
        engine.dispose()
    return 0


def verify(url, runner=subprocess.run) -> int:
    code = _run([sys.executable, "-m", "backend.app.scripts.verify_postgres_ops"], url, runner)
    if code:
        return code
    engine = _engine(url)
    try:
        # 运行角色真的能读每一张需要授权的表——迁移成功不等于应用可用。新建表要加进
        # postgres_verifier.RUNTIME_ACCESS_TABLES，否则这道验证会随时间失效。
        with engine.connect() as connection:
            for name in RUNTIME_ACCESS_TABLES:
                connection.execute(select(func.count()).select_from(table(name))).scalar_one()
                print("runtime role can read:", name)
            connection.rollback()
    finally:
        engine.dispose()
    return 0


def main(argv=None, environ=None, runner=subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=COMMANDS)
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    name = CREDENTIAL[args.command]
    url = environ.get(name)
    if not url:
        print("缺少环境变量 %s" % name, file=sys.stderr)
        return 2
    try:
        if args.command == "survey":
            return survey(url)
        if args.command == "pending":
            return _run(["alembic", "history", "--indicate-current"], url, runner)
        if args.command == "upgrade":
            return _run(["alembic", "upgrade", "head"], url, runner)
        return verify(url, runner)
    except Exception as exc:
        # 驱动的错误文本可能回显连接串片段：只给类型。
        print("%s 失败（%s）；为保护凭据，未输出原始错误。" % (args.command, type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
