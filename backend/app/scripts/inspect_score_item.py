"""只读查看一个评分项能不能改分、为什么（RUNBOOK「评分工作区里改分点保存没反应」）。

用法（生产在 app 容器里跑）::

    python -m backend.app.scripts.inspect_score_item <score_item_id>

评分项 ID 取自浏览器开发者工具里 ``PATCH /api/score-items/<id>`` 的地址，或 app 日志里
同一行。只读：查完即回滚、不写库；不打印论文正文、学生姓名与学号，只打印状态、分数、
改分会被哪道检查拒绝，以及下一步该怎么做。
"""

from __future__ import annotations

import json
import sys

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.db.models import BatchScoringItem
from backend.app.db.models import BatchScoringJob
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun


UNRESOLVED = ("invalid", "blocked")


def _num(value):
    return None if value is None else float(value)


def inspect_score_item(db, item_id: str) -> dict:
    # 与改分接口（engine.update_score_item）同一套加载与检查，结论才对得上。
    from backend.app.services.batches import state as batch_state
    from backend.app.services.scoring.engine import _require_recalculable_run
    from backend.app.services.scoring.profiles.thesis import ThesisProfile

    item = db.scalar(
        select(ScoreItem)
        .where(ScoreItem.id == item_id)
        .options(
            selectinload(ScoreItem.scoring_run).selectinload(ScoringRun.paper),
            selectinload(ScoreItem.scoring_run).selectinload(ScoringRun.rubric),
            selectinload(ScoreItem.scoring_run).selectinload(ScoringRun.items),
        )
    )
    if item is None:
        return {"found": False, "item_id": item_id, "next_step": "没有这个评分项：确认 ID，或材料已被重新评分。"}

    run = item.scoring_run
    paper = getattr(run, "paper", None)
    batch = getattr(paper, "batch", None)
    checks = {}
    for name, check in (
        ("frozen_policy", lambda: _require_recalculable_run(run)),
        ("ordinary_override", lambda: ThesisProfile().assert_ordinary_item_override_allowed(item=item)),
        ("batch_writable", lambda: batch_state.guard_writable(batch) if batch is not None else None),
    ):
        try:
            check()
            checks[name] = "ok"
        except Exception as exc:  # noqa: BLE001 —— 诊断要原样报出被哪道检查拒绝
            checks[name] = "rejected: %s" % exc

    jobs = []
    if getattr(run, "paper_id", None):
        rows = db.execute(
            select(BatchScoringItem, BatchScoringJob)
            .join(BatchScoringJob, BatchScoringJob.id == BatchScoringItem.job_id)
            .where(BatchScoringItem.paper_id == run.paper_id)
            .order_by(BatchScoringItem.created_at)
        ).all()
        jobs = [
            {
                "job_id": job.id,
                "job_status": job.status,
                "item_status": job_item.status,
                "error_code": job_item.error_code,
            }
            for job_item, job in rows
        ]

    siblings = [i.auto_score_status for i in (run.items or [])]
    if item.auto_score_status in UNRESOLVED:
        next_step = (
            "AI 没评出这一项的有效结果，不能普通改分：到评分进度页「重试失败项」；"
            "连接配置改过、重试被拒时，为这份材料新建评分任务。"
        )
    elif checks["batch_writable"] != "ok":
        next_step = "批次已归档：先在评分任务里重新打开批次，再改分。"
    elif checks["frozen_policy"] != "ok":
        next_step = "这份评分来自旧版本，不能重算：重新评分后再改。"
    else:
        next_step = (
            "规则上可以改分。界面仍保存失败时，查 app 日志里 PATCH /api/score-items/%s 的状态码。" % item.id
        )

    return {
        "found": True,
        "item": {
            "id": item.id,
            "criterion_code": item.criterion_code,
            "max_score": _num(item.max_score),
            "ai_score": _num(item.ai_score),
            "final_score": _num(item.final_score),
            "auto_score_status": item.auto_score_status,
            "need_manual_review": item.need_manual_review,
        },
        "run": {
            "id": run.id,
            "status": run.status,
            "created_at": run.created_at,
            "final_total_score": _num(getattr(run, "final_total_score", None)),
            "unresolved_items": sum(1 for status in siblings if status in UNRESOLVED),
            "total_items": len(siblings),
        },
        "batch_jobs": jobs,
        "checks": checks,
        "override_allowed": all(value == "ok" for value in checks.values()),
        "next_step": next_step,
    }


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("用法：python -m backend.app.scripts.inspect_score_item <score_item_id>", file=sys.stderr)
        return 2

    from backend.app.db.session import SessionLocal

    db = SessionLocal()
    try:
        report = inspect_score_item(db, args[0])
    finally:
        # 只读：不管查到什么都回滚。生产连的是事务池，不能用会话级 SET 设只读。
        db.rollback()
        db.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["found"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
