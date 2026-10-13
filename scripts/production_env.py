"""从本机的生产变量副本生成 Lightsail 实例上的 /opt/pgs/.env。

    uv run python scripts/production_env.py --site-address <名字>.duckdns.org --output /tmp/pgs.env
    scp /tmp/pgs.env deploy@<实例 IP>:/opt/pgs/.env && rm /tmp/pgs.env

只输出变量名，从不打印任何值。输出文件权限 0600，已存在时不覆盖（除非 --force）。

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

from dotenv import dotenv_values
from sqlalchemy.engine import make_url


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
REQUIRED = ("DATABASE_URL", "AUTH_SECRET", "SUPABASE_URL", "SUPABASE_SECRET_KEY")
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
    url = selected.get("DATABASE_URL")
    if url:
        try:
            port = make_url(url).port
        except Exception:
            found.append("DATABASE_URL 无法解析")
        else:
            if port != 6543:
                found.append("DATABASE_URL 不是 Supabase 事务池（端口 6543）")
    return found


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
    parser.add_argument("--source", default=".env.local", help="本机生产变量副本（默认 .env.local）")
    parser.add_argument("--site-address", required=True, help="站点域名，例如 <名字>.duckdns.org")
    parser.add_argument("--output", required=True, help="输出文件；上传到实例后删除")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    args = parser.parse_args(argv)

    if not _SITE.match(args.site_address):
        parser.error("--site-address 应为域名，不带 https:// 和路径")
    source = Path(args.source)
    if not source.is_file():
        parser.error("找不到 %s" % source)
    output = Path(args.output)
    if output.exists() and not args.force:
        parser.error("%s 已存在；确认要覆盖时加 --force" % output)

    selected = select_variables(dotenv_values(source))
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
