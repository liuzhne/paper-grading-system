"""旧导出日志补录的运维入口（前端 v2 计划 §7，阶段 6B）。

独立于迁移存在，因为 ``alembic upgrade head`` 不会重跑已完成的迁移：回退
窗口内旧应用只写 `spreadsheet_write_logs`，重新前进时要能再执行一次把这段
补上。可重入，重复执行不产生重复事件。

    python -m backend.app.scripts.backfill_export_events [--dry-run]
"""

import argparse
import json
import sys

from backend.app.db.session import SessionLocal
from backend.app.services.batches.export_backfill import backfill_legacy_export_logs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计将要补录的行数，不提交。",
    )
    args = parser.parse_args(argv)

    with SessionLocal() as session:
        try:
            result = backfill_legacy_export_logs(session)
        except ValueError as exc:
            # 未登记的通道：整批停下，不写入猜出来的映射。
            session.rollback()
            print(json.dumps({"status": "aborted", "reason": str(exc)}, ensure_ascii=False))
            return 1
        if args.dry_run:
            session.rollback()
            result["status"] = "dry_run"
        else:
            session.commit()
            result["status"] = "committed"
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
