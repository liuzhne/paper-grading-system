from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment
from openpyxl.styles import Border
from openpyxl.styles import Font
from openpyxl.styles import PatternFill
from openpyxl.styles import Side
from openpyxl.utils import get_column_letter
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
from backend.app.services.scoring.profiles.thesis import ThesisProfile


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

_HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
_HEADER_FONT = Font(color="FFFFFF", bold=True)
_ALT_ROW_FILL = PatternFill("solid", fgColor="F3F8FC")
_THIN_BORDER = Border(bottom=Side(style="thin", color="D9E2F3"))

_SUMMARY_WIDTHS = [22, 16, 14, 18, 20, 38, 18, 12, 12, 10, 48, 14, 14, 20, 18, 40, 34]
_DETAIL_WIDTHS = [16, 14, 38, 28, 11, 11, 11, 44, 64, 38, 48, 12]
_SUMMARY_WRAP_COLUMNS = {1, 4, 5, 6, 11, 16, 17}
_DETAIL_WRAP_COLUMNS = {3, 4, 8, 9, 10, 11}
_SUMMARY_SCORE_COLUMNS = {8, 9}
_DETAIL_SCORE_COLUMNS = {5, 6, 7}


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
        projection = ThesisProfile().build_spreadsheet_projection(
            batch=batch,
            run=run,
            review_logs=review_logs_by_run.get(run.id, []),
        )
        row = projection["summary"]
        summary.append(
            [
                row["batch_name"],
                row["student_id"],
                row["student_name"],
                row["department"],
                row["major"],
                row["title"],
                row["rubric_version"],
                row["ai_total"],
                row["final_total"],
                row["grade"],
                row["main_deductions"],
                row["changed"],
                row["need_manual_review"],
                row["finished_at"] or "",
                row["reviewer"],
                row["review_notes"],
                row["report_link"],
            ]
        )
        for item in projection["details"]:
            detail.append(
                [
                    item["student_id"],
                    item["student_name"],
                    item["title"],
                    item["criterion_name"],
                    item["max_score"],
                    item["ai_score"],
                    item["final_score"],
                    item["deductions"],
                    item["evidence_quotes"],
                    item["evidence_locations"],
                    item["suggestion"],
                    item["confidence"],
                ]
            )

    _format_export_sheet(
        summary,
        widths=_SUMMARY_WIDTHS,
        wrap_columns=_SUMMARY_WRAP_COLUMNS,
        score_columns=_SUMMARY_SCORE_COLUMNS,
        date_columns={14},
    )
    _format_export_sheet(
        detail,
        widths=_DETAIL_WIDTHS,
        wrap_columns=_DETAIL_WRAP_COLUMNS,
        score_columns=_DETAIL_SCORE_COLUMNS,
        confidence_columns={12},
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


def _format_export_sheet(
    sheet,
    *,
    widths,
    wrap_columns,
    score_columns,
    date_columns=frozenset(),
    confidence_columns=frozenset(),
):
    """Apply a readable, print-friendly presentation to an export sheet."""

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False
    sheet.print_title_rows = "1:1"
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.oddFooter.center.text = "第 &P 页 / 共 &N 页"
    sheet.oddFooter.center.size = 9
    sheet.oddFooter.center.color = "666666"

    sheet.row_dimensions[1].height = 30
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width

    for cell in sheet[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=True,
        )

    for row_number in range(2, sheet.max_row + 1):
        row = sheet[row_number]
        if row_number % 2 == 0:
            for cell in row:
                cell.fill = _ALT_ROW_FILL
        for cell in row:
            cell.border = _THIN_BORDER
            cell.alignment = Alignment(
                horizontal=(
                    "right"
                    if cell.column in score_columns | confidence_columns
                    else "left"
                ),
                vertical="top",
                wrap_text=cell.column in wrap_columns,
            )
            if cell.column in score_columns:
                cell.number_format = "0.00"
            elif cell.column in confidence_columns:
                cell.number_format = "0.000"
            elif cell.column in date_columns and cell.value:
                cell.number_format = "yyyy-mm-dd hh:mm:ss"
        sheet.row_dimensions[row_number].height = _row_height(
            row,
            widths,
            wrap_columns,
        )


def _row_height(row, widths, wrap_columns):
    lines = 1
    for cell in row:
        if cell.column not in wrap_columns or cell.value in (None, ""):
            continue
        width = widths[cell.column - 1]
        text = str(cell.value)
        estimated = sum(
            max(1, (len(part) + max(int(width) - 1, 1)) // max(int(width), 1))
            for part in text.splitlines() or [text]
        )
        lines = max(lines, estimated)
    # Keep enough height for long evidence/recommendation cells. A generous
    # cap prevents pathological exports from becoming unbounded while avoiding
    # the visible clipping that occurred with the previous fixed-height rows.
    return min(240, max(22, 16 * lines))


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
