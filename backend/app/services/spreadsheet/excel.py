from pathlib import Path

from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from backend.app.core.config import settings
from backend.app.db.models import GradingBatch
from backend.app.db.models import Paper
from backend.app.db.models import ReviewLog
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun
from backend.app.db.models import SpreadsheetWriteLog
from backend.app.services.scoring.rules import as_float


SUMMARY_HEADERS = [
    "批次名称",
    "学号",
    "姓名",
    "学院",
    "专业",
    "论文题目",
    "评分标准版本",
    "AI 总分",
    "最终总分",
    "等级",
    "主要扣分点",
    "是否人工修改",
    "是否需要复核",
    "评分时间",
    "复核教师",
    "复核意见",
    "报告链接",
]

DETAIL_HEADERS = [
    "学号",
    "姓名",
    "论文题目",
    "评分项",
    "满分",
    "AI 得分",
    "最终得分",
    "扣分原因",
    "原文依据",
    "依据位置",
    "修改建议",
    "置信度",
]


def export_batch_excel(db: Session, batch_id: str):
    batch = db.scalar(select(GradingBatch).where(GradingBatch.id == batch_id).options(selectinload(GradingBatch.rubric)))
    if batch is None:
        raise ValueError("batch not found")

    runs = db.scalars(
        select(ScoringRun)
        .join(Paper)
        .where(Paper.batch_id == batch_id)
        .options(
            selectinload(ScoringRun.paper),
            selectinload(ScoringRun.rubric),
            selectinload(ScoringRun.items),
            selectinload(ScoringRun.items).selectinload(ScoreItem.criterion),
        )
    ).all()
    review_logs_by_run = _review_logs_by_run(db, runs)

    workbook = Workbook()
    summary = workbook.active
    summary.title = "总分表"
    summary.append(SUMMARY_HEADERS)

    detail = workbook.create_sheet("评分明细表")
    detail.append(DETAIL_HEADERS)

    for run in runs:
        paper = run.paper
        changed = any(as_float(item.final_score) != as_float(item.ai_score) for item in run.items)
        summary.append(
            [
                batch.name,
                paper.student_id,
                paper.student_name,
                paper.department or batch.department,
                paper.major or batch.major,
                paper.title,
                run.rubric.version,
                as_float(run.ai_total_score),
                as_float(run.final_total_score),
                run.grade,
                _main_deductions(run),
                "是" if changed else "否",
                "是" if run.need_manual_review else "否",
                run.finished_at.isoformat(sep=" ") if run.finished_at else "",
                "dev-user" if run.status == "reviewed" else "",
                _review_notes(review_logs_by_run.get(run.id, [])),
                "/api/scoring-runs/%s/report" % run.id,
            ]
        )
        for item in run.items:
            detail.append(
                [
                    paper.student_id,
                    paper.student_name,
                    paper.title,
                    item.criterion.name,
                    as_float(item.max_score),
                    as_float(item.ai_score),
                    as_float(item.final_score),
                    "；".join(item.deductions or []),
                    "；".join(evidence.get("quote", "") for evidence in item.evidence or []),
                    "；".join(evidence.get("location", "") for evidence in item.evidence or []),
                    item.suggestion,
                    as_float(item.confidence),
                ]
            )

    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    path = settings.exports_dir / ("batch_%s_scores.xlsx" % batch_id)
    # 先写临时文件 → 落库提交 → 原子改名为最终文件：commit 失败不会留下孤儿导出文件。
    tmp_path = path.with_name(path.name + ".tmp")
    workbook.save(tmp_path)
    try:
        for run in runs:
            db.add(
                SpreadsheetWriteLog(
                    scoring_run_id=run.id,
                    target_type="excel",
                    target_id=str(path),
                    status="success",
                    response={"path": str(path)},
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.replace(path)
    return path


def _main_deductions(run):
    deductions = []
    for item in run.items:
        deductions.extend(item.deductions or [])
    return "；".join(deductions[:3])


def _review_logs_by_run(db, runs):
    if not runs:
        return {}
    logs = db.scalars(
        select(ReviewLog)
        .where(ReviewLog.scoring_run_id.in_([run.id for run in runs]))
        .order_by(ReviewLog.created_at)
    ).all()
    result = {}
    for log in logs:
        result.setdefault(log.scoring_run_id, []).append(log)
    return result


def _review_notes(review_logs):
    if not review_logs:
        return ""
    overall_notes = [log.reason for log in review_logs if log.score_item_id is None and log.reason]
    if overall_notes:
        return "；".join(overall_notes)
    return "；".join(log.reason for log in review_logs if log.reason)
