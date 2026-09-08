"""旧导出日志补录的运维入口（前端 v2 计划 §7，阶段 6B）。

独立于迁移存在，因为 ``alembic upgrade head`` 不会重跑已完成的迁移：回退
窗口内旧应用只写 `spreadsheet_write_logs`，重新前进时要能再执行一次把这段
补上。可重入，重复执行不产生重复事件。

    DATABASE_URL=... python -m backend.app.scripts.backfill_export_events [--dry-run]

**必须显式给出 DATABASE_URL。** `settings` 会加载 `.env.local`，开发机上那通常指向
生产库；不带 URL 直接跑，等于按文档执行一次就连上生产。远端目标默认拒绝，需要
`--i-know-this-is-not-local` 显式确认——照搬备份恢复的那条约定。
"""

import argparse
import json
import sys

from sqlalchemy.engine import make_url

from backend.app.core.config import settings
from backend.app.db.session import SessionLocal
from backend.app.services.batches.export_backfill import backfill_legacy_export_logs


#: 视为本地的主机名。空 host 属于 sqlite 这类文件库。
_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})


def requires_confirmation(url: str) -> bool:
    """目标库是否需要显式确认。

    判据只看「是不是本地」，不试图识别「是不是生产」：认不出生产的那一次，
    恰恰就是出事的那一次。
    """
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        return False
    return (parsed.host or "") not in _LOCAL_HOSTS


def _run(dry_run: bool) -> int:
    with SessionLocal() as session:
        try:
            result = backfill_legacy_export_logs(session)
        except ValueError as exc:
            # 未登记的通道：整批停下，不写入猜出来的映射。
            session.rollback()
            print(json.dumps({"status": "aborted", "reason": str(exc)}, ensure_ascii=False))
            return 1
        if dry_run:
            session.rollback()
            result["status"] = "dry_run"
        else:
            session.commit()
            result["status"] = "committed"
        print(json.dumps(result, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计将要补录的行数，不提交。",
    )
    parser.add_argument(
        "--i-know-this-is-not-local",
        action="store_true",
        help="确认目标不是本地库。远端目标默认拒绝。",
    )
    args = parser.parse_args(argv)

    url = settings.DATABASE_URL
    if requires_confirmation(url) and not args.i_know_this_is_not_local:
        host = make_url(url).host
        # dry-run 也拦：它同样建立连接、同样按 .env.local 指向生产。
        # 「只读所以没关系」正是让人在生产上养成随手执行习惯的那句话。
        print(
            "拒绝执行：目标数据库不是本地库（host=%s）。\n"
            "这个脚本会写入 export_events。settings 会加载 .env.local，"
            "开发机上它通常指向生产。\n"
            "确认目标无误后重跑并加上 --i-know-this-is-not-local；"
            "或显式指定 DATABASE_URL 指向本地库。" % host
        )
        return 2

    return _run(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
