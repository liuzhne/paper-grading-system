"""只读的规则体检报表（docs/论文评分第二阶段改造方案.md 第 7 节）。

用法（生产在 app 容器里跑）::

    python -m backend.app.scripts.rule_health_report <batch_id>

按批次统计每条规则的触发、未触发、证据不足、判失败次数，列出“全部触发或全部不触发”的规则
（可能没在看论文）、经常一起触发的规则对（可能重复扣分），以及“评分项里只有它触发、人工改分后
分数变高”的次数（可能误扣）。只读：查完即回滚、不写库；不打印论文正文与学生信息。
"""

from __future__ import annotations

import json
import sys


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("用法：python -m backend.app.scripts.rule_health_report <batch_id>", file=sys.stderr)
        return 2

    from backend.app.db.session import SessionLocal
    from backend.app.services.calibration.analytics import rule_health

    db = SessionLocal()
    try:
        report = rule_health(db, args[0])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        # 只读：不管查到什么都回滚。生产连的是事务池，不能用会话级 SET 设只读。
        db.rollback()
        db.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
