"""生成 Lightsail 实例上的密钥文件。只输出变量名，从不打印任何值。

三种文件（``--kind``）：

- ``app``（默认）：/opt/pgs/.env，给 app 与 worker。
- ``db``：/opt/pgs/db.env，只给 db 与 backup 服务：超级用户密码与数据位置。app 与 worker 拿不到它。
- ``ops``：一次性运维容器（迁移、切换）用的连接串，经 SSH 标准输入交给 deploy/production/ops.sh，
  用完即删。``docker run --env-file`` 不处理引号，这一种不加引号。

数据位置（``--data-plane``）：部署任务读实例上的 /opt/pgs/DATA_PLANE，没有这个文件即 ``supabase``
（docs/迁出Supabase到单机Docker方案.md 8.1）。

- ``supabase``：数据库与文件在 Supabase（切换前），连接串来自 GitHub 上的 secret。
- ``local``：数据库是同机的 db 容器，文件在卷里；连接串由 ``PGS_APP_PASSWORD`` /
  ``POSTGRES_PASSWORD`` 拼成 ``…@db:5432/pgs``，不必另存连接串。

两种来源：

- 部署任务（默认路径）：GitHub production 环境的密钥与变量作为环境变量传进来，
  ``--from-environment`` 只取名单里的变量。见 .github/workflows/ci.yml 的
  deploy-lightsail-production。
- 手工：从一份本机变量文件生成，上传后删除本地文件。

      uv run python scripts/production_env.py --source <变量文件> --site-address <名字>.duckdns.org --output /tmp/pgs.env
      uv run python scripts/production_env.py --source <变量文件> --kind db --output /tmp/db.env

输出文件权限 0600，已存在时不覆盖（除非 --force）。不依赖项目环境：部署任务用系统
自带的 python3 运行，只有 ``--source`` 才需要 python-dotenv。

去掉的变量：Vercel 平台注入的（``VERCEL*`` 等）、只在 Vercel 上有意义的
``BATCH_SCORING_DISPATCH``，以及 deploy/production/compose.yml 里已经写死的生产配置
——留着也会被 compose 覆盖，但两处写同一个变量，以后改一处就会让人以为生效了。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs
from urllib.parse import urlsplit


DROPPED_PREFIXES = ("VERCEL", "TURBO_", "NX_")
DROPPED = {
    "BATCH_SCORING_DISPATCH",
    # compose.yml 的 environment 里写死的：
    "STORAGE_ROOT",
    "AUTH_ENABLED",
    "INLINE_JOB_RUN_ENABLED",
    "LLM_DEBUG_LOG_ENABLED",
    # 本脚本按数据位置自己写：
    "PGS_SITE_ADDRESS",
    "STORAGE_PROVIDER",
    # 数据库密码与 owner 连接串只进 db.env 或一次性运维容器，不给 app 与 worker：
    "POSTGRES_PASSWORD",
    "PGS_APP_PASSWORD",
    "MIGRATION_DATABASE_URL",
    "RUNTIME_DATABASE_URL",
    "PGS_DATA_PLANE",
}
DATA_PLANES = ("supabase", "local")
KINDS = ("app", "db", "ops")
#: 本机数据库：compose 里的 db 服务（不对外开端口）。
LOCAL_HOST = "db"
LOCAL_PORT = 5432
LOCAL_DATABASE = "pgs"
SUPABASE_ONLY = ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_STORAGE_BUCKET")
APP_REQUIRED = {
    "supabase": (
        "DATABASE_URL",
        "AUTH_SECRET",
        "AUTH_PASSWORD",
        "BYOK_MASTER_KEY",
        "SUPABASE_URL",
        "SUPABASE_SECRET_KEY",
    ),
    "local": ("PGS_APP_PASSWORD", "AUTH_SECRET", "AUTH_PASSWORD", "BYOK_MASTER_KEY"),
}
REQUIRED = APP_REQUIRED["supabase"]
#: 与 backend/app/scripts/init_database.py 相同：只用字母和数字，原样放进连接串不用编码。
DATABASE_PASSWORDS = ("POSTGRES_PASSWORD", "PGS_APP_PASSWORD")
_DATABASE_PASSWORD = re.compile(r"^[A-Za-z0-9]{24,}$")
#: 可选开关：GitHub 仓库变量没设时不写进 .env，应用取默认值（关闭）。
#: ASSISTANT_AGENT_ENABLED：评分助手的 Agent 回合（docs/评分助手Agent化方案.md）。设为 true 时，部署流水线先用
#: 新镜像跑 assistant_eval_gate：当前提示词指纹上没有达标的评测报告就不部署（§12.5）。
OPTIONAL_SWITCHES = ("SITE_LEADS_ENABLED", "ASSISTANT_AGENT_ENABLED")
# --from-environment 只取这些：部署任务的环境里还有 GITHUB_*、RUNNER_* 等无关变量。
ENVIRONMENT_NAMES = (
    REQUIRED
    + ("AUTH_USERNAME", "SUPABASE_STORAGE_BUCKET")
    + OPTIONAL_SWITCHES
    + DATABASE_PASSWORDS
    + ("MIGRATION_DATABASE_URL", "RUNTIME_DATABASE_URL")
)
_SITE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def select_variables(values: dict) -> dict:
    selected = {}
    for name, value in values.items():
        if value is None or not _NAME.match(name):
            continue
        if name in DROPPED or name.startswith(DROPPED_PREFIXES):
            continue
        selected[name] = value
    return selected


def local_database_url(user: str, password: str) -> str:
    """本机 db 容器的连接串；密码只有字母和数字（见 _DATABASE_PASSWORD），不用编码。"""

    return "postgresql+psycopg://%s:%s@%s:%d/%s" % (user, password, LOCAL_HOST, LOCAL_PORT, LOCAL_DATABASE)


def _password_problems(values: dict, names) -> list[str]:
    found = []
    for name in names:
        value = values.get(name)
        if not value:
            found.append(f"缺少必需变量 {name}")
        elif not _DATABASE_PASSWORD.match(value):
            found.append(f"{name} 只能是字母和数字，至少 24 位")
    return found


def app_variables(values: dict, data_plane: str) -> dict:
    """.env 的内容：本机模式下连接串由 PGS_APP_PASSWORD 拼出，不再要 Supabase 的变量。"""

    selected = select_variables(values)
    if data_plane == "local":
        for name in SUPABASE_ONLY:
            selected.pop(name, None)
        selected.pop("DATABASE_URL", None)
        if values.get("PGS_APP_PASSWORD"):
            selected["DATABASE_URL"] = local_database_url("pgs_app", values["PGS_APP_PASSWORD"])
    selected["STORAGE_PROVIDER"] = data_plane
    return selected


def app_problems(values: dict, data_plane: str) -> list[str]:
    if data_plane == "local":
        found = _password_problems(values, ("PGS_APP_PASSWORD",))
        found += [f"缺少必需变量 {name}" for name in APP_REQUIRED["local"][1:] if not values.get(name)]
        return found + _switch_problems(values)
    return problems(select_variables(values))


def db_variables(values: dict, data_plane: str) -> dict:
    return {"POSTGRES_PASSWORD": values.get("POSTGRES_PASSWORD") or "", "PGS_DATA_PLANE": data_plane}


def db_problems(values: dict) -> list[str]:
    return _password_problems(values, ("POSTGRES_PASSWORD",))


def ops_variables(values: dict, data_plane: str, with_source: bool) -> dict:
    """一次性运维容器的连接串：MIGRATION_*（owner）、RUNTIME_*（pgs_app），切换时多一个 SOURCE_*。"""

    if data_plane == "local":
        selected = {
            "MIGRATION_DATABASE_URL": local_database_url("postgres", values.get("POSTGRES_PASSWORD") or ""),
            "RUNTIME_DATABASE_URL": local_database_url("pgs_app", values.get("PGS_APP_PASSWORD") or ""),
            # init_database 只在角色不存在时用它建 pgs_app。
            "PGS_APP_PASSWORD": values.get("PGS_APP_PASSWORD") or "",
        }
        if with_source:
            # 切换时 GitHub 上的 RUNTIME_DATABASE_URL 还指向 Supabase：它就是复制的源库。
            selected["SOURCE_DATABASE_URL"] = values.get("RUNTIME_DATABASE_URL") or ""
        return selected
    return {name: values.get(name) or "" for name in ("MIGRATION_DATABASE_URL", "RUNTIME_DATABASE_URL")}


def ops_problems(values: dict, data_plane: str, with_source: bool) -> list[str]:
    if data_plane == "local":
        found = _password_problems(values, DATABASE_PASSWORDS)
        if with_source and not values.get("RUNTIME_DATABASE_URL"):
            found.append("缺少必需变量 RUNTIME_DATABASE_URL（切换时它是复制的源库）")
        return found
    if with_source:
        return ["--with-source 只用于切换到本机数据库（--data-plane local）"]
    return [f"缺少必需变量 {name}" for name in ("MIGRATION_DATABASE_URL", "RUNTIME_DATABASE_URL") if not values.get(name)]


def _switch_problems(selected: dict) -> list[str]:
    found = []
    for name in OPTIONAL_SWITCHES:
        # 写成 yes/on 应用也认，但仓库变量一眼看不出开没开；只收 true / false。
        value = selected.get(name)
        if value is not None and value not in ("true", "false"):
            found.append(f"{name} 只能是 true 或 false")
    return found


def problems(selected: dict) -> list[str]:
    """Supabase 模式（切换前）的 .env 检查。"""

    found = [f"缺少必需变量 {name}" for name in REQUIRED if not selected.get(name)]
    found += _switch_problems(selected)
    url = selected.get("DATABASE_URL")
    if url:
        try:
            port = urlsplit(url).port
        except ValueError:
            found.append("DATABASE_URL 无法解析")
        else:
            if port != 6543:
                found.append("DATABASE_URL 不是 Supabase 事务池（端口 6543）")
            # Supabase 给 Prisma 的连接串带 ?pgbouncer=true；psycopg 不认这个参数，
            # 应用会在第一次连库时报 invalid connection option "pgbouncer"。
            if "pgbouncer" in parse_qs(urlsplit(url).query):
                found.append("DATABASE_URL 带有 pgbouncer=true（Prisma 专用参数），去掉它")
    return found


def from_environment(environ) -> dict:
    return {name: environ[name] for name in ENVIRONMENT_NAMES if environ.get(name)}


def from_file(path: Path) -> dict:
    from dotenv import dotenv_values

    return {name: value for name, value in dotenv_values(path).items() if value is not None}


def _quote(value: str) -> str:
    if "\n" in value:
        raise ValueError("值里不能有换行")
    # docker compose 的 env_file：单引号内原样保留，不做变量替换。
    if "'" in value:
        raise ValueError("值里不能有单引号")
    return "'%s'" % value


def render(selected: dict, site_address: str | None = None, *, quoted: bool = True) -> str:
    lines = ["# 由 scripts/production_env.py 生成；不要提交到仓库。"]
    if site_address is not None:
        lines.append("PGS_SITE_ADDRESS=%s" % _quote(site_address))
    for name in sorted(selected):
        value = selected[name]
        try:
            if quoted:
                value = _quote(value)
            elif "\n" in value:
                raise ValueError("值里不能有换行")
        except ValueError as exc:
            raise ValueError("%s：%s" % (name, exc)) from None
        lines.append("%s=%s" % (name, value))
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", help="本机变量文件")
    source.add_argument("--from-environment", action="store_true", help="从环境变量读（部署任务用）")
    parser.add_argument("--kind", choices=KINDS, default="app", help="app：.env；db：db.env；ops：一次性运维容器")
    parser.add_argument("--data-plane", choices=DATA_PLANES, default="supabase", help="实例上 /opt/pgs/DATA_PLANE 的值")
    parser.add_argument("--with-source", action="store_true", help="ops：加上切换时的源库 SOURCE_DATABASE_URL")
    parser.add_argument("--site-address", help="站点域名，例如 <名字>.duckdns.org（--kind app 必填）")
    parser.add_argument("--output", required=True, help="输出文件；上传到实例后删除")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    args = parser.parse_args(argv)

    if args.kind == "app":
        if not args.site_address or not _SITE.match(args.site_address):
            parser.error("--site-address 应为域名，不带 https:// 和路径")
    elif args.site_address:
        parser.error("--site-address 只用于 --kind app")
    if args.with_source and args.kind != "ops":
        parser.error("--with-source 只用于 --kind ops")
    if args.source and not Path(args.source).is_file():
        parser.error("找不到 %s" % args.source)
    output = Path(args.output)
    if output.exists() and not args.force:
        parser.error("%s 已存在；确认要覆盖时加 --force" % output)

    values = from_environment(os.environ) if args.from_environment else from_file(Path(args.source))
    if args.kind == "app":
        found = app_problems(values, args.data_plane)
        selected = app_variables(values, args.data_plane)
    elif args.kind == "db":
        found = db_problems(values)
        selected = db_variables(values, args.data_plane)
    else:
        found = ops_problems(values, args.data_plane, args.with_source)
        selected = ops_variables(values, args.data_plane, args.with_source)
    if found:
        for message in found:
            print("错误：" + message, file=sys.stderr)
        return 1
    try:
        content = render(selected, args.site_address, quoted=args.kind != "ops")
    except ValueError as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 1

    if output.exists():
        output.unlink()
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    names = (["PGS_SITE_ADDRESS"] if args.site_address else []) + sorted(selected)
    print("已写入 %s（权限 0600，数据位置 %s），共 %d 个变量：" % (output, args.data_plane, len(names)))
    print("  " + " ".join(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
