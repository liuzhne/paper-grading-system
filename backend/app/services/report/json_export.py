"""结构化 JSON 导出（设计§11）：把一次评分运行序列化为下游友好的单一 JSON 文档。

复用 ScoringRunRead / ScoreItemRead / ReviewLogRead schema（与 API 返回同口径），
汇总 运行/论文/评分标准/逐项(扣分·证据·选档·子结果)/篇章·格式发现/复核日志。Web 与 CLI 共用。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from backend.app.db.models import ReviewLog
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun
from backend.app.schemas.scoring import ReviewLogRead
from backend.app.schemas.scoring import ScoreItemRead
from backend.app.schemas.scoring import ScoringRunRead
from backend.app.services.scoring.profiles.thesis import ThesisProfile


def build_run_export(db: Session, run_id: str) -> dict:
    run = db.scalar(
        select(ScoringRun)
        .where(ScoringRun.id == run_id)
        .options(
            selectinload(ScoringRun.paper),
            selectinload(ScoringRun.rubric),
            selectinload(ScoringRun.items).selectinload(ScoreItem.criterion),
        )
    )
    if run is None:
        raise ValueError("scoring run not found")
    if run.submission_id is not None:
        raise ValueError("submission scoring runs require run-export@2")

    review_logs = db.scalars(
        select(ReviewLog).where(ReviewLog.scoring_run_id == run.id).order_by(ReviewLog.created_at)
    ).all()
    artifact = ThesisProfile().build_artifact_projection(
        run=run,
        review_logs=review_logs,
    )
    paper = artifact["paper"]
    rubric = artifact["rubric"]
    return {
        "schema": "paper-grading/run-export@1",
        "run": ScoringRunRead.model_validate(run).model_dump(mode="json"),
        "paper": {
            "id": paper["id"],
            "title": paper["title"],
            "student_id": paper["student_id"],
            "student_name": paper["student_name"],
        }
        if paper
        else None,
        "rubric": {"id": rubric["id"], "name": rubric["name"], "version": rubric["version"]} if rubric else None,
        "items": [ScoreItemRead.model_validate(item).model_dump(mode="json") for item in run.items],
        "review_logs": [ReviewLogRead.model_validate(log).model_dump(mode="json") for log in review_logs],
    }
