"""L2 跨文档校准库（设计§7）。

存"脱敏范文 + 已知分数 + 理由"为锚点，按 (rubric, 评分项) 归档；评分时取同项锚点作 few-shot，
统一模型在不同论文间的宽严尺度。锚点集合随评分输入一并进 L0 缓存哈希（保可复现，见 cache.build_request）。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import CalibrationAnchor

# 单次注入的锚点上限与摘录长度（控成本/上下文）。
MAX_ANCHORS = 4
EXCERPT_CHARS = 600


def create_anchor(db: Session, payload):
    anchor = CalibrationAnchor(
        rubric_id=payload.rubric_id,
        criterion_code=payload.criterion_code,
        score=payload.score,
        max_score=payload.max_score,
        label=payload.label,
        excerpt=payload.excerpt,
        rationale=payload.rationale,
        source=payload.source,
    )
    db.add(anchor)
    db.commit()
    db.refresh(anchor)
    return anchor


def list_anchors(db: Session, rubric_id: str, criterion_code: str = None):
    query = select(CalibrationAnchor).where(CalibrationAnchor.rubric_id == rubric_id)
    if criterion_code:
        query = query.where(CalibrationAnchor.criterion_code == criterion_code)
    return list(db.scalars(query.order_by(CalibrationAnchor.score.desc())))


def get_anchors(db: Session, rubric_id: str, criterion_code: str):
    """取某评分项的锚点（已脱敏范文），转为可注入提示且可哈希的纯 dict。"""
    anchors = list_anchors(db, rubric_id, criterion_code)[:MAX_ANCHORS]
    return [
        {
            "label": anchor.label,
            "score": float(anchor.score),
            "max_score": float(anchor.max_score),
            "excerpt": (anchor.excerpt or "")[:EXCERPT_CHARS],
            "rationale": anchor.rationale or "",
        }
        for anchor in anchors
    ]
