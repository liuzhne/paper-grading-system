from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from backend.app.db.models import GradingBatch
from backend.app.db.models import Paper
from backend.app.services.scoring.rules import as_float


def get_batch_summary(db: Session, batch_id: str):
    batch = db.scalar(
        select(GradingBatch)
        .where(GradingBatch.id == batch_id)
        .options(
            selectinload(GradingBatch.rubric),
            selectinload(GradingBatch.papers).selectinload(Paper.scoring_runs),
        )
    )
    if batch is None:
        raise ValueError("batch not found")

    paper_stats = Counter()
    run_stats = Counter()
    paper_summaries = []

    for paper in sorted(batch.papers, key=lambda item: item.created_at, reverse=True):
        paper_stats[paper.status] += 1
        latest_run = _latest_run(paper.scoring_runs)
        if latest_run is not None:
            run_stats[latest_run.status] += 1
            if latest_run.need_manual_review:
                run_stats["need_manual_review"] += 1
        else:
            run_stats["not_scored"] += 1

        paper_summaries.append(
            {
                "paper_id": paper.id,
                "title": paper.title,
                "student_id": paper.student_id,
                "student_name": paper.student_name,
                "file_name": paper.file_name,
                "paper_status": paper.status,
                "parse_quality": as_float(paper.parse_quality) if paper.parse_quality is not None else None,
                "latest_run_id": latest_run.id if latest_run else None,
                "latest_run_status": latest_run.status if latest_run else None,
                "latest_final_score": as_float(latest_run.final_total_score) if latest_run else None,
                "latest_grade": latest_run.grade if latest_run else None,
                "latest_need_manual_review": latest_run.need_manual_review if latest_run else None,
            }
        )

    return {
        "batch": batch,
        "rubric_name": batch.rubric.name,
        "rubric_version": batch.rubric.version,
        "paper_stats": _with_defaults(paper_stats, ["uploaded", "parsed", "scored", "pending_review", "reviewed", "failed"]),
        "run_stats": _with_defaults(run_stats, ["not_scored", "scored", "reviewing", "reviewed", "need_manual_review"]),
        "papers": paper_summaries,
    }


def _latest_run(runs):
    if not runs:
        return None
    return sorted(runs, key=lambda item: item.created_at, reverse=True)[0]


def _with_defaults(counter, keys):
    result = {key: int(counter.get(key, 0)) for key in keys}
    for key, value in counter.items():
        result.setdefault(key, int(value))
    return result
