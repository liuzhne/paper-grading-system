"""导入结果落库 + 评分项构建：route 与 CLI 共用同一份，避免双写漂移。

`persist_imported_rubric` 把 `parser.parse_rubric_files` 的结果落库为 draft Rubric；
`build_criterion`/`extra_criterion_fields` 也供 rubrics 路由的创建/克隆/更新复用（鸭子类型，
既吃 Pydantic 评分项也吃导入的 ImportedCriterion）。
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from backend.app.core.config import settings
from backend.app.db.models import Rubric
from backend.app.db.models import RubricCriterion


def persist_imported_rubric(db: Session, name, version, description, imported):
    """把导入结果落库为 draft Rubric；name+version 重复抛 ValueError（调用方转 400/退出码）。"""
    exists = db.scalar(select(Rubric).where(Rubric.name == name, Rubric.version == version))
    if exists is not None:
        raise ValueError("rubric name and version already exist")

    rubric = Rubric(
        name=name,
        version=version,
        total_score=imported.total_score,
        description=import_description(description, imported.template_summary),
        format_spec=imported.format_spec,
        status="draft",
        created_by=settings.DEFAULT_DEV_USER_ID,
    )
    for index, criterion in enumerate(imported.criteria):
        rubric.criteria.append(build_criterion(criterion, index))
    db.add(rubric)
    try:
        db.commit()  # 依赖 uq_rubrics_name_version 兜底并发：预检查与提交之间若被插队，由唯一约束拦截
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("rubric name and version already exist") from exc
    db.refresh(rubric)
    return load_rubric(db, rubric.id)


def load_rubric(db, rubric_id):
    return db.scalar(select(Rubric).where(Rubric.id == rubric_id).options(selectinload(Rubric.criteria)))


def build_criterion(criterion, index):
    return RubricCriterion(
        code=criterion.code,
        name=criterion.name,
        max_score=criterion.max_score,
        weight=criterion.weight,
        description=criterion.description,
        evidence_hints=criterion.evidence_hints,
        deduction_rules=criterion.deduction_rules,
        display_order=getattr(criterion, "display_order", None) if getattr(criterion, "display_order", None) is not None else index,
        **extra_criterion_fields(criterion),
    )


def extra_criterion_fields(criterion):
    return {
        "criterion_type": getattr(criterion, "criterion_type", None) or "llm_judgment",
        "scoring_mode": getattr(criterion, "scoring_mode", None) or "llm_direct",
        "applies_to": getattr(criterion, "applies_to", None) or "global",
        "rubric_levels": list(getattr(criterion, "rubric_levels", None) or []),
        "sub_checks": list(getattr(criterion, "sub_checks", None) or []),
        "dimension": getattr(criterion, "dimension", None),
        "deduction_rules_structured": list(getattr(criterion, "deduction_rules_structured", None) or []),
    }


def import_description(description, template_summary):
    parts = [description.strip()] if description and description.strip() else []
    hints = template_summary.get("hints") or []
    if hints:
        parts.append("由 Word 模板解析到的结构提示：%s。" % "、".join(hints[:12]))
    return "\n".join(parts) if parts else None
