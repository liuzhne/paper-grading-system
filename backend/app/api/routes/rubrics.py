from datetime import datetime
from datetime import timezone
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import Response
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from backend.app.core.config import settings
from backend.app.db.models import Rubric
from backend.app.db.models import RubricCriterion
from backend.app.db.models import ScoringRun
from backend.app.db.session import get_db
from backend.app.schemas.rubric import RubricCreate
from backend.app.schemas.rubric import RubricCloneRequest
from backend.app.schemas.rubric import RubricImportResult
from backend.app.schemas.rubric import RubricRead
from backend.app.schemas.rubric import RubricUpdate
from backend.app.services.dev_user import ensure_dev_user
from backend.app.services.llm.factory import get_llm_scorer
from backend.app.eval.scores_template import build_scores_table_template
from backend.app.services.rubric_import.parser import parse_rubric_files
from backend.app.services.rubric_import.template import build_rubric_import_template

router = APIRouter(prefix="/rubrics", tags=["rubrics"])


@router.post("", response_model=RubricRead)
def create_rubric(payload: RubricCreate, db: Session = Depends(get_db)):
    ensure_dev_user(db)
    exists = db.scalar(select(Rubric).where(Rubric.name == payload.name, Rubric.version == payload.version))
    if exists is not None:
        raise HTTPException(status_code=400, detail="rubric name and version already exist")

    rubric = Rubric(
        name=payload.name,
        version=payload.version,
        total_score=payload.total_score,
        description=payload.description,
        status="draft",
        created_by=settings.DEFAULT_DEV_USER_ID,
    )
    for index, criterion in enumerate(payload.criteria):
        rubric.criteria.append(
            RubricCriterion(
                code=criterion.code,
                name=criterion.name,
                max_score=criterion.max_score,
                weight=criterion.weight,
                description=criterion.description,
                evidence_hints=criterion.evidence_hints,
                deduction_rules=criterion.deduction_rules,
                display_order=criterion.display_order or index,
                **_extra_criterion_fields(criterion),
            )
        )
    db.add(rubric)
    db.commit()
    db.refresh(rubric)
    return _load_rubric(db, rubric.id)


@router.get("", response_model=list[RubricRead])
def list_rubrics(db: Session = Depends(get_db)):
    return db.scalars(select(Rubric).options(selectinload(Rubric.criteria)).order_by(Rubric.created_at.desc())).all()


@router.get("/import-template.xlsx")
def download_rubric_import_template():
    return Response(
        build_rubric_import_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="rubric_import_template.xlsx"'},
    )


@router.get("/{rubric_id}/scores-template.xlsx")
def download_scores_template(rubric_id: str, db: Session = Depends(get_db)):
    """按该 rubric 的评分项 code 生成"教师成绩表"模板，供 QWK 评估填写（见 scripts/run_qwk_eval）。"""
    rubric = _load_rubric(db, rubric_id)
    if rubric is None:
        raise HTTPException(status_code=404, detail="rubric not found")
    return Response(
        build_scores_table_template(rubric.criteria),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="scores_template.xlsx"'},
    )


@router.post("/import-files", response_model=RubricImportResult)
def import_rubric_from_files(
    name: str = Form(...),
    version: str = Form("v1.0"),
    description: Optional[str] = Form(None),
    rules_file: UploadFile = File(...),
    template_file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    ensure_dev_user(db)
    if not _is_excel_file(rules_file.filename or ""):
        raise HTTPException(status_code=400, detail="rules_file must be an .xlsx or .xlsm file")
    if template_file and not _is_docx_file(template_file.filename or ""):
        raise HTTPException(status_code=400, detail="template_file must be a .docx file")

    exists = db.scalar(select(Rubric).where(Rubric.name == name, Rubric.version == version))
    if exists is not None:
        raise HTTPException(status_code=400, detail="rubric name and version already exist")

    try:
        imported = parse_rubric_files(
            rules_bytes=rules_file.file.read(),
            template_bytes=template_file.file.read() if template_file else None,
            scorer=_safe_scorer(),  # §5 扣分规则归一化（无显式规则时）；构造失败则不调小模型
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    rubric = Rubric(
        name=name,
        version=version,
        total_score=imported.total_score,
        description=_import_description(description, imported.template_summary),
        format_spec=imported.format_spec,
        status="draft",
        created_by=settings.DEFAULT_DEV_USER_ID,
    )
    for index, criterion in enumerate(imported.criteria):
        rubric.criteria.append(_build_criterion(criterion, index))
    db.add(rubric)
    db.commit()
    db.refresh(rubric)
    return {
        "rubric": _load_rubric(db, rubric.id),
        "warnings": imported.warnings,
        "template_summary": imported.template_summary,
    }


@router.post("/{rubric_id}/clone", response_model=RubricRead)
def clone_rubric(rubric_id: str, payload: RubricCloneRequest, db: Session = Depends(get_db)):
    ensure_dev_user(db)
    original = _load_rubric(db, rubric_id)
    if original is None:
        raise HTTPException(status_code=404, detail="rubric not found")

    new_name = payload.name or original.name
    exists = db.scalar(select(Rubric).where(Rubric.name == new_name, Rubric.version == payload.new_version))
    if exists is not None:
        raise HTTPException(status_code=400, detail="rubric name and version already exist")

    cloned = Rubric(
        name=new_name,
        version=payload.new_version,
        total_score=original.total_score,
        status="draft",
        description=payload.description if payload.description is not None else original.description,
        created_by=settings.DEFAULT_DEV_USER_ID,
    )
    for criterion in original.criteria:
        cloned.criteria.append(
            RubricCriterion(
                code=criterion.code,
                name=criterion.name,
                max_score=criterion.max_score,
                weight=criterion.weight,
                description=criterion.description,
                evidence_hints=list(criterion.evidence_hints or []),
                deduction_rules=list(criterion.deduction_rules or []),
                display_order=criterion.display_order,
                **_extra_criterion_fields(criterion),
            )
        )

    db.add(cloned)
    db.commit()
    db.refresh(cloned)
    return _load_rubric(db, cloned.id)


@router.get("/{rubric_id}", response_model=RubricRead)
def get_rubric(rubric_id: str, db: Session = Depends(get_db)):
    rubric = _load_rubric(db, rubric_id)
    if rubric is None:
        raise HTTPException(status_code=404, detail="rubric not found")
    return rubric


@router.patch("/{rubric_id}", response_model=RubricRead)
def update_rubric(rubric_id: str, payload: RubricUpdate, db: Session = Depends(get_db)):
    ensure_dev_user(db)
    rubric = _load_rubric(db, rubric_id)
    if rubric is None:
        raise HTTPException(status_code=404, detail="rubric not found")
    if rubric.status != "draft":
        raise HTTPException(status_code=400, detail="only draft rubrics can be edited; clone published rubrics first")

    updates = payload.model_dump(exclude_unset=True)
    scored_run_id = db.scalar(select(ScoringRun.id).where(ScoringRun.rubric_id == rubric.id).limit(1))
    if scored_run_id and {"name", "version", "total_score", "criteria"}.intersection(updates):
        raise HTTPException(status_code=400, detail="cannot change scoring rubric fields after scoring runs exist; clone first")

    next_name = updates.get("name", rubric.name)
    next_version = updates.get("version", rubric.version)
    exists = db.scalar(select(Rubric).where(Rubric.name == next_name, Rubric.version == next_version, Rubric.id != rubric.id))
    if exists is not None:
        raise HTTPException(status_code=400, detail="rubric name and version already exist")

    if "name" in updates:
        if payload.name is None:
            raise HTTPException(status_code=400, detail="name cannot be null")
        rubric.name = payload.name
    if "version" in updates:
        if payload.version is None:
            raise HTTPException(status_code=400, detail="version cannot be null")
        rubric.version = payload.version
    if "description" in updates:
        rubric.description = payload.description
    if "total_score" in updates:
        if payload.total_score is None:
            raise HTTPException(status_code=400, detail="total_score cannot be null")
        rubric.total_score = payload.total_score
    if "criteria" in updates:
        if payload.criteria is None:
            raise HTTPException(status_code=400, detail="criteria cannot be null")
        _validate_criteria_total(rubric.total_score, payload.criteria)
        rubric.criteria.clear()
        db.flush()
        for index, criterion in enumerate(payload.criteria):
            rubric.criteria.append(_build_criterion(criterion, index))
    elif "total_score" in updates:
        _validate_criteria_total(rubric.total_score, rubric.criteria)

    db.commit()
    db.refresh(rubric)
    return _load_rubric(db, rubric_id)


@router.post("/{rubric_id}/publish", response_model=RubricRead)
def publish_rubric(rubric_id: str, db: Session = Depends(get_db)):
    rubric = _load_rubric(db, rubric_id)
    if rubric is None:
        raise HTTPException(status_code=404, detail="rubric not found")
    rubric.status = "published"
    rubric.published_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    db.refresh(rubric)
    return _load_rubric(db, rubric_id)


def _load_rubric(db, rubric_id):
    return db.scalar(select(Rubric).where(Rubric.id == rubric_id).options(selectinload(Rubric.criteria)))


def _build_criterion(criterion, index):
    return RubricCriterion(
        code=criterion.code,
        name=criterion.name,
        max_score=criterion.max_score,
        weight=criterion.weight,
        description=criterion.description,
        evidence_hints=criterion.evidence_hints,
        deduction_rules=criterion.deduction_rules,
        display_order=criterion.display_order if criterion.display_order is not None else index,
        **_extra_criterion_fields(criterion),
    )


def _safe_scorer():
    # §5 规则归一化用的小模型；构造失败（如真实 LLM 未配好）则返回 None，导入不受阻、退化为仅 Excel 显式规则。
    try:
        return get_llm_scorer()
    except Exception:
        return None


def _extra_criterion_fields(criterion):
    return {
        "criterion_type": getattr(criterion, "criterion_type", None) or "llm_judgment",
        "scoring_mode": getattr(criterion, "scoring_mode", None) or "llm_direct",
        "applies_to": getattr(criterion, "applies_to", None) or "global",
        "rubric_levels": list(getattr(criterion, "rubric_levels", None) or []),
        "sub_checks": list(getattr(criterion, "sub_checks", None) or []),
        "dimension": getattr(criterion, "dimension", None),
        "deduction_rules_structured": list(getattr(criterion, "deduction_rules_structured", None) or []),
    }


def _validate_criteria_total(total_score, criteria):
    max_sum = round(sum(item.max_score for item in criteria), 2)
    target_total = round(float(total_score), 2)
    if max_sum == target_total:
        return
    weighted_sum = round(sum(item.weight or 0 for item in criteria), 2)
    if weighted_sum != target_total:
        raise HTTPException(status_code=400, detail="criterion max_score sum or weight sum must equal total_score")


def _is_excel_file(filename):
    return filename.lower().endswith((".xlsx", ".xlsm"))


def _is_docx_file(filename):
    return filename.lower().endswith(".docx")


def _import_description(description, template_summary):
    parts = [description.strip()] if description and description.strip() else []
    hints = template_summary.get("hints") or []
    if hints:
        parts.append("由 Word 模板解析到的结构提示：%s。" % "、".join(hints[:12]))
    return "\n".join(parts) if parts else None
