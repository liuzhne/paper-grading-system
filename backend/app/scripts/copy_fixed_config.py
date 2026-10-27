"""切换到本机数据库时，把固定配置与账号从旧库复制到新库（docs/迁出Supabase到单机Docker方案.md 第 6 节）。

    python -m backend.app.scripts.copy_fixed_config check   # 只读：两边的迁移版本与要复制的行数
    python -m backend.app.scripts.copy_fixed_config apply   # 复制；新库须已由 init_database 建好且这些表为空

连接串从环境变量读：

- ``SOURCE_DATABASE_URL``：旧库（切换时 GitHub 上的 ``RUNTIME_DATABASE_URL``，指向 Supabase）。
  只在只读事务里读取，最后回滚。
- ``MIGRATION_DATABASE_URL``：新库的 owner 连接串。所有表在一个事务里写入，任一步失败整体回滚。

只复制两边都有的列、保留原 ID：密码哈希原样复制，老师用原密码登录；模型连接的密钥是用
``BYOK_MASTER_KEY`` 加密的密文，这个密钥不换，新库就能解密；评测工作流的 ``--connection-id``
照常可用。这些表的主键都是字符串 UUID，没有序列需要重置。

只打印表名、列名与行数，从不打印内容、连接串或密钥。
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import MetaData
from sqlalchemy import create_engine
from sqlalchemy import func
from sqlalchemy import inspect
from sqlalchemy import select
from sqlalchemy import table
from sqlalchemy import text

from backend.app.db.session import engine_options
from backend.app.services.deployment.backup import current_migration_head


#: 按外键顺序：组织的 created_by 指向用户；成员、邀请与模型连接指向组织和用户；助手偏好再指向
#: 模型连接。外键只能指向排在前面的表（测试按模型元数据核对）。
#: 维护者 2026-10-27 定（方案 D3、D5）：审计日志与其它业务数据都不迁移。
TABLES = (
    "users",
    "organizations",
    "organization_members",
    "organization_invitations",
    "platform_llm_config",
    "ai_connections",
    "assistant_preferences",
    "site_leads",
)


def _url(raw: str) -> str:
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            return "postgresql+psycopg://" + raw[len(prefix) :]
    return raw


def make_engine(raw_url: str):
    url = _url(raw_url)
    return create_engine(url, **engine_options(url))


def _head(connection) -> str | None:
    if not inspect(connection).has_table("alembic_version"):
        return None
    return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _read_only(connection):
    # 事务级只读：连接池（6543）不会把它带到下一个借用这条连接的人（RUNBOOK 2026-10-16）。
    if connection.dialect.name == "postgresql":
        connection.execute(text("SET TRANSACTION READ ONLY"))


def _counts(connection, tables=TABLES) -> dict:
    present = set(inspect(connection).get_table_names())
    counts = {}
    for name in tables:
        if name in present:
            counts[name] = connection.execute(select(func.count()).select_from(table(name))).scalar_one()
        else:
            counts[name] = None
    return counts


def survey(source_engine, target_engine, *, image_head=None) -> dict:
    """两边的迁移版本与各表行数；不写任何东西。"""

    image_head = image_head or current_migration_head()
    with source_engine.connect() as source:
        _read_only(source)
        source_head = _head(source)
        source_counts = _counts(source)
        source.rollback()
    with target_engine.connect() as target:
        target_head = _head(target)
        target_counts = _counts(target)
        target.rollback()
    return {
        "image_head": image_head,
        "source_head": source_head,
        "target_head": target_head,
        "source": source_counts,
        "target": target_counts,
    }


def problems(report: dict, *, require_initialized: bool) -> list[str]:
    found = []
    if report["source_head"] != report["image_head"]:
        found.append("旧库的迁移版本 %s 与镜像 %s 不同：先把旧库迁移到同一版本" % (report["source_head"], report["image_head"]))
    missing = [name for name, count in report["source"].items() if count is None]
    if missing:
        found.append("旧库缺少这些表：%s" % ", ".join(missing))
    if report["target_head"] is None:
        if require_initialized:
            found.append("新库还没有初始化：先跑 init_database")
    elif report["target_head"] != report["image_head"]:
        found.append("新库的迁移版本 %s 与镜像 %s 不同" % (report["target_head"], report["image_head"]))
    occupied = [name for name, count in report["target"].items() if count]
    if occupied:
        found.append("新库这些表已有数据，拒绝复制（重跑前先清空新库）：%s" % ", ".join(occupied))
    return found


def copy_tables(source_engine, target_engine, tables=TABLES) -> dict:
    """在一个目标事务里按顺序复制；返回每张表复制的行数与没复制的列。"""

    copied = {}
    skipped_columns = {}
    with source_engine.connect() as source:
        _read_only(source)
        source_meta = MetaData()
        source_meta.reflect(bind=source, only=list(tables))
        with target_engine.begin() as target:
            target_meta = MetaData()
            target_meta.reflect(bind=target, only=list(tables))
            for name in tables:
                source_table = source_meta.tables[name]
                target_table = target_meta.tables[name]
                columns = [column.name for column in target_table.columns if column.name in source_table.columns]
                extra = sorted(set(source_table.columns.keys()) - set(columns))
                if extra:
                    skipped_columns[name] = extra
                rows = source.execute(select(*[source_table.c[column] for column in columns])).mappings().all()
                if rows:
                    target.execute(target_table.insert(), [dict(row) for row in rows])
                written = target.execute(select(func.count()).select_from(target_table)).scalar_one()
                if written != len(rows):
                    raise RuntimeError("%s 复制后行数不一致：读到 %d、写入后 %d" % (name, len(rows), written))
                copied[name] = len(rows)
        source.rollback()
    return {"copied": copied, "skipped_columns": skipped_columns}


def _print_report(report: dict):
    print("镜像迁移版本：%s" % report["image_head"])
    print("旧库迁移版本：%s" % report["source_head"])
    print("新库迁移版本：%s" % (report["target_head"] or "未初始化"))
    for name in TABLES:
        target = report["target"][name]
        print(
            "  %-26s 旧库 %s 行 / 新库 %s"
            % (
                name,
                "缺表" if report["source"][name] is None else report["source"][name],
                "未建表" if target is None else "%d 行" % target,
            )
        )


def main(argv=None, environ=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("check", "apply"))
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    missing = [name for name in ("SOURCE_DATABASE_URL", "MIGRATION_DATABASE_URL") if not environ.get(name)]
    if missing:
        print("缺少环境变量：%s" % ", ".join(missing), file=sys.stderr)
        return 2

    source_engine = make_engine(environ["SOURCE_DATABASE_URL"])
    target_engine = make_engine(environ["MIGRATION_DATABASE_URL"])
    try:
        report = survey(source_engine, target_engine)
        _print_report(report)
        found = problems(report, require_initialized=args.command == "apply")
        if found:
            for message in found:
                print("不能复制：" + message, file=sys.stderr)
            return 1
        if args.command == "check":
            print("检查通过%s" % ("" if report["target_head"] else "（新库待初始化）"))
            return 0
        result = copy_tables(source_engine, target_engine)
    except Exception as exc:
        # 驱动的错误文本可能回显连接串片段：只给类型。
        print("复制失败（%s）；新库没有写入任何行。为保护凭据，未输出原始错误。" % type(exc).__name__, file=sys.stderr)
        return 1
    finally:
        source_engine.dispose()
        target_engine.dispose()
    for name in TABLES:
        print("已复制 %-26s %d 行" % (name, result["copied"][name]))
    for name, columns in sorted(result["skipped_columns"].items()):
        print("新库没有、未复制的列：%s.%s" % (name, ", ".join(columns)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
