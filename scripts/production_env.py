"""生成 Lightsail 实例上的 /opt/pgs/.env。只输出变量名，从不打印任何值。

两种来源：

- 部署任务（默认路径）：GitHub production 环境的密钥与变量作为环境变量传进来，
  ``--from-environment`` 只取名单里的变量。见 .github/workflows/ci.yml 的
  deploy-lightsail-production。
- 手工：从一份本机变量文件生成，上传后删除本地文件。

      uv run python scripts/production_env.py --source <变量文件> --site-address <名字>.duckdns.org --output /tmp/pgs.env

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
    "STORAGE_PROVIDER",
    "STORAGE_ROOT",
    "AUTH_ENABLED",
    "INLINE_JOB_RUN_ENABLED",
    "LLM_DEBUG_LOG_ENABLED",
    # 本脚本自己写：
    "PGS_SITE_ADDRESS",
}
REQUIRED = (
    "DATABASE_URL",
    "AUTH_SECRET",
    "AUTH_PASSWORD",
    "BYOK_MASTER_KEY",
    "SUPABASE_URL",
    "SUPABASE_SECRET_KEY",
)
#: 可选开关：GitHub 仓库变量没设时不写进 .env，应用取默认值（关闭）。
#: ASSISTANT_AGENT_ENABLED：评分助手的 Agent 回合（docs/评分助手Agent化方案.md），A1 评测达标后才打开。
OPTIONAL_SWITCHES = ("SITE_LEADS_ENABLED", "ASSISTANT_AGENT_ENABLED")
# --from-environment 只取这些：部署任务的环境里还有 GITHUB_*、RUNNER_* 等无关变量。
ENVIRONMENT_NAMES = REQUIRED + ("AUTH_USERNAME", "SUPABASE_STORAGE_BUCKET") + OPTIONAL_SWITCHES
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


def problems(selected: dict) -> list[str]:
    found = [f"缺少必需变量 {name}" for name in REQUIRED if not selected.get(name)]
    for name in OPTIONAL_SWITCHES:
        # 写成 yes/on 应用也认，但仓库变量一眼看不出开没开；只收 true / false。
        value = selected.get(name)
        if value is not None and value not in ("true", "false"):
            found.append(f"{name} 只能是 true 或 false")
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

    return select_variables(dotenv_values(path))


def _quote(value: str) -> str:
    if "\n" in value:
        raise ValueError("值里不能有换行")
    # docker compose 的 env_file：单引号内原样保留，不做变量替换。
    if "'" in value:
        raise ValueError("值里不能有单引号")
    return "'%s'" % value


def render(selected: dict, site_address: str) -> str:
    lines = ["# 由 scripts/production_env.py 生成；不要提交到仓库。"]
    lines.append("PGS_SITE_ADDRESS=%s" % _quote(site_address))
    for name in sorted(selected):
        try:
            lines.append("%s=%s" % (name, _quote(selected[name])))
        except ValueError as exc:
            raise ValueError("%s：%s" % (name, exc)) from None
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", help="本机变量文件")
    source.add_argument("--from-environment", action="store_true", help="从环境变量读（部署任务用）")
    parser.add_argument("--site-address", required=True, help="站点域名，例如 <名字>.duckdns.org")
    parser.add_argument("--output", required=True, help="输出文件；上传到实例后删除")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    args = parser.parse_args(argv)

    if not _SITE.match(args.site_address):
        parser.error("--site-address 应为域名，不带 https:// 和路径")
    if args.source and not Path(args.source).is_file():
        parser.error("找不到 %s" % args.source)
    output = Path(args.output)
    if output.exists() and not args.force:
        parser.error("%s 已存在；确认要覆盖时加 --force" % output)

    selected = from_environment(os.environ) if args.from_environment else from_file(Path(args.source))
    found = problems(selected)
    if found:
        for message in found:
            print("错误：" + message, file=sys.stderr)
        return 1
    try:
        content = render(selected, args.site_address)
    except ValueError as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 1

    if output.exists():
        output.unlink()
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    print("已写入 %s（权限 0600），共 %d 个变量：" % (output, len(selected) + 1))
    print("  " + " ".join(["PGS_SITE_ADDRESS", *sorted(selected)]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
