from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import Response
from fastapi import UploadFile
from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from backend.app.db.models import Rubric
from backend.app.db.models import RubricCompilation
from backend.app.db.models import ScoringRun
from backend.app.db.session import get_db
from backend.app.schemas.rubric import RubricCreate
from backend.app.schemas.rubric import RubricCloneRequest
from backend.app.schemas.rubric import AtomicRuleEditRequest
from backend.app.schemas.rubric import RubricImportResult
from backend.app.schemas.rubric import RubricLifecycleReason
from backend.app.schemas.rubric import RubricPublishRequest
from backend.app.schemas.rubric import RubricRead
from backend.app.schemas.rubric import RubricDraftRecompileRequest
from backend.app.schemas.rubric import RubricExecutionDraftRead
from backend.app.schemas.rubric import TemplateLinkReviewRequest
from backend.app.schemas.rubric import RubricUpdate
from backend.app.api.deps import CurrentPrincipal
from backend.app.api.deps import current_principal
from backend.app.api.deps import current_user_id
from backend.app.api.deps import require_organization_role
from backend.app.services.dev_user import ensure_dev_user
from backend.app.services.auth import auth_active
from backend.app.services.llm.factory import get_llm_scorer
from backend.app.eval.scores_template import build_scores_table_template
from backend.app.services.rubric_import.parser import parse_rubric_files
from backend.app.services.rubric_import.persist import build_criterion
from backend.app.services.rubric_import.persist import extra_criterion_fields
from backend.app.services.rubric_import.template import build_rubric_import_template
from backend.app.services.scoring.core.policy import validate_weight_configuration
from backend.app.services.rubric_import import pipeline as rubric_pipeline
from backend.app.services.rubrics import lifecycle as rubric_lifecycle
from backend.app.services.rubrics.draft_graph import read_execution_draft

router = APIRouter(prefix="/rubrics", tags=["rubrics"])


def _scoped_duplicate(
    db: Session,
    *,
    name: str,
    version: str,
    visibility: str,
    principal: CurrentPrincipal,
    exclude_id: str | None = None,
) -> Rubric | None:
    query = select(Rubric).where(
        Rubric.name == name,
        Rubric.version == version,
        Rubric.visibility == visibility,
    )
    if visibility == "system":
        pass
    elif visibility == "organization":
        query = query.where(Rubric.organization_id == principal.organization_id)
    else:
        query = query.where(Rubric.owner_id == principal.user_id)
    if exclude_id is not None:
        query = query.where(Rubric.id != exclude_id)
    return db.scalar(query)


def _visible_rubric(
    db: Session,
    rubric_id: str,
    principal: CurrentPrincipal,
) -> Rubric:
    rubric = _load_rubric(db, rubric_id)
    if rubric is None:
        raise HTTPException(status_code=404, detail="rubric not found")
    allowed = (
        not auth_active()
        or principal.platform_role == "platform_admin"
        or (
            rubric.visibility == "system"
            or rubric.visibility == "organization" and rubric.organization_id == principal.organization_id
            or rubric.visibility == "private" and rubric.owner_id == principal.user_id
        )
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="rubric not found")
    return rubric


@router.post("", response_model=RubricRead)
def create_rubric(
    payload: RubricCreate,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    if payload.visibility == "system":
        if principal.platform_role != "platform_admin":
            raise HTTPException(status_code=403, detail="only platform admins can create system rubrics")
    elif payload.visibility == "organization":
        require_organization_role(principal, "org_admin")
    _validate_criteria_total(payload.total_score, payload.criteria)
    exists = _scoped_duplicate(
        db,
        name=payload.name,
        version=payload.version,
        visibility=payload.visibility,
        principal=principal,
    )
    if exists is not None:
        raise HTTPException(status_code=400, detail="rubric name and version already exist")

    db.commit()  # pipeline owns a short, clean transaction
    try:
        prepared = rubric_pipeline.prepare_manual_json_import(
            command=_manual_import_command(payload)
        )
        identity = rubric_pipeline.persist_prepared_import(
            session=db,
            prepared=prepared,
            actor_id=user_id,
            organization_id=(None if payload.visibility == "system" else principal.organization_id),
            visibility=payload.visibility,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _load_rubric(db, identity.rubric_id)


@router.get("", response_model=list[RubricRead])
def list_rubrics(
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    query = select(Rubric).options(selectinload(Rubric.criteria))
    if auth_active() and principal.platform_role != "platform_admin":
        query = query.where(
            or_(
                Rubric.visibility == "system",
                and_(
                    Rubric.visibility == "organization",
                    Rubric.organization_id == principal.organization_id,
                ),
                and_(
                    Rubric.visibility == "private",
                    Rubric.owner_id == principal.user_id,
                ),
            )
        )
    return db.scalars(query.order_by(Rubric.created_at.desc())).all()


@router.get("/import-template.xlsx")
def download_rubric_import_template():
    return Response(
        build_rubric_import_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="rubric_import_template.xlsx"'},
    )


@router.get("/{rubric_id}/scores-template.xlsx")
def download_scores_template(
    rubric_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """按该 rubric 的评分项 code 生成"教师成绩表"模板，供 QWK 评估填写（见 scripts/run_qwk_eval）。"""
    rubric = _visible_rubric(db, rubric_id, principal)
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
    visibility: str = Form("private"),
    rules_file: UploadFile = File(...),
    template_file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    if visibility not in {"private", "organization", "system"}:
        raise HTTPException(status_code=422, detail="invalid rubric visibility")
    if visibility == "system":
        if principal.platform_role != "platform_admin":
            raise HTTPException(status_code=403, detail="only platform admins can create system rubrics")
    elif visibility == "organization":
        require_organization_role(principal, "org_admin")
    if not _is_excel_file(rules_file.filename or ""):
        raise HTTPException(status_code=400, detail="rules_file must be an .xlsx or .xlsm file")
    if template_file and not _is_docx_file(template_file.filename or ""):
        raise HTTPException(status_code=400, detail="template_file must be a .docx file")

    exists = _scoped_duplicate(db, name=name, version=version, visibility=visibility, principal=principal)
    if exists is not None:
        raise HTTPException(status_code=400, detail="rubric name and version already exist")

    rules_bytes = rules_file.file.read()
    template_bytes = template_file.file.read() if template_file else None
    try:
        # Keep the established response summary while persistence is delegated
        # to the two-phase M4 import pipeline below.
        imported = parse_rubric_files(
            rules_bytes=rules_bytes,
            template_bytes=template_bytes,
            scorer=None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        db.commit()
        prepared = rubric_pipeline.prepare_file_import(
            command=_file_import_command(
                name=name,
                version=version,
                description=description,
                rules_file_name=rules_file.filename or "rules.xlsx",
                template_file_name=(template_file.filename if template_file else None),
            ),
            rules_bytes=rules_bytes,
            template_bytes=template_bytes,
            scorer=None,
        )
        identity = rubric_pipeline.persist_prepared_import(
            session=db,
            prepared=prepared,
            actor_id=user_id,
            organization_id=(None if visibility == "system" else principal.organization_id),
            visibility=visibility,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "rubric": _load_rubric(db, identity.rubric_id),
        "warnings": imported.warnings,
        "template_summary": imported.template_summary,
    }


@router.post("/{rubric_id}/clone", response_model=RubricRead)
def clone_rubric(
    rubric_id: str,
    payload: RubricCloneRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    original = _visible_rubric(db, rubric_id, principal)

    new_name = payload.name or original.name
    exists = _scoped_duplicate(
        db,
        name=new_name,
        version=payload.new_version,
        visibility=original.visibility,
        principal=principal,
    )
    if exists is not None:
        raise HTTPException(status_code=400, detail="rubric name and version already exist")

    try:
        cloned = rubric_lifecycle.clone_published_rubric(
            db,
            rubric_id,
            payload.new_version,
            user_id,
            name=payload.name,
            description=payload.description,
        )
        db.commit()
    except rubric_lifecycle.RubricLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _load_rubric(db, cloned.id)


@router.get("/{rubric_id}", response_model=RubricRead)
def get_rubric(
    rubric_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    return _visible_rubric(db, rubric_id, principal)


@router.get(
    "/{rubric_id}/execution-draft",
    response_model=RubricExecutionDraftRead,
)
def get_rubric_execution_draft(
    rubric_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    try:
        _visible_rubric(db, rubric_id, principal)
        return read_execution_draft(session=db, rubric_id=rubric_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{rubric_id}/submit-review", response_model=RubricRead)
def submit_rubric_review(
    rubric_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    try:
        _visible_rubric(db, rubric_id, principal)
        rubric_lifecycle.submit_for_review(db, rubric_id)
        db.commit()
    except rubric_lifecycle.RubricLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _load_rubric(db, rubric_id)


@router.post("/{rubric_id}/return-to-draft", response_model=RubricRead)
def return_rubric_to_draft(
    rubric_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    try:
        _visible_rubric(db, rubric_id, principal)
        rubric_lifecycle.return_to_draft(db, rubric_id)
        db.commit()
    except rubric_lifecycle.RubricLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _load_rubric(db, rubric_id)


@router.post("/{rubric_id}/recompile", response_model=RubricRead)
def recompile_rubric_draft(
    rubric_id: str,
    payload: RubricDraftRecompileRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """Create a complete successor graph for a blocked/unpublished draft."""

    ensure_dev_user(db)
    rubric = _visible_rubric(db, rubric_id, principal)
    if rubric.status != "draft":
        raise HTTPException(
            status_code=400,
            detail="only a draft rubric can be recompiled",
        )
    _validate_criteria_total(rubric.total_score, payload.criteria)
    command = _manual_recompile_command(rubric, payload)
    try:
        # Preparation is DB-free.  End the read transaction before the
        # persistence service takes its short lock/commit transaction.
        db.commit()
        prepared = rubric_pipeline.prepare_manual_json_recompile(command=command)
        identity = rubric_pipeline.persist_prepared_import(
            session=db,
            prepared=prepared,
            actor_id=user_id,
            target_rubric_id=rubric_id,
            reason=payload.reason,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _load_rubric(db, identity.rubric_id)


@router.post("/{rubric_id}/rules/{rule_code}/submit-review")
def submit_atomic_rule_review(
    rubric_id: str,
    rule_code: str,
    payload: RubricLifecycleReason,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    try:
        _visible_rubric(db, rubric_id, principal)
        rule = rubric_lifecycle.submit_atomic_rule_for_review(
            db, rubric_id, rule_code, user_id, payload.reason
        )
        db.commit()
    except rubric_lifecycle.RubricLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _rule_response(rule)


@router.patch("/{rubric_id}/rules/{rule_code}")
def patch_atomic_rule(
    rubric_id: str,
    rule_code: str,
    payload: AtomicRuleEditRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    try:
        _visible_rubric(db, rubric_id, principal)
        rule = rubric_lifecycle.edit_atomic_rule(
            db,
            rubric_id,
            rule_code,
            payload.changes,
            user_id,
            payload.reason,
        )
        db.commit()
    except rubric_lifecycle.RubricLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _rule_response(rule)


@router.post("/{rubric_id}/rules/{rule_code}/approve")
def approve_atomic_rule_review(
    rubric_id: str,
    rule_code: str,
    payload: RubricLifecycleReason,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    try:
        _visible_rubric(db, rubric_id, principal)
        rule = rubric_lifecycle.approve_atomic_rule(
            db, rubric_id, rule_code, user_id, payload.reason
        )
        db.commit()
    except rubric_lifecycle.RubricLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _rule_response(rule)


@router.post("/{rubric_id}/rules/{rule_code}/reject")
def reject_atomic_rule_review(
    rubric_id: str,
    rule_code: str,
    payload: RubricLifecycleReason,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    try:
        _visible_rubric(db, rubric_id, principal)
        rule = rubric_lifecycle.reject_atomic_rule(
            db, rubric_id, rule_code, user_id, payload.reason
        )
        db.commit()
    except rubric_lifecycle.RubricLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _rule_response(rule)


@router.post("/{rubric_id}/rules/{rule_code}/reopen")
def reopen_atomic_rule_review(
    rubric_id: str,
    rule_code: str,
    payload: RubricLifecycleReason,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    try:
        _visible_rubric(db, rubric_id, principal)
        rule = rubric_lifecycle.reopen_atomic_rule(
            db, rubric_id, rule_code, user_id, payload.reason
        )
        db.commit()
    except rubric_lifecycle.RubricLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _rule_response(rule)


@router.post("/{rubric_id}/template-links/{link_id}/review")
def review_atomic_rule_template_link(
    rubric_id: str,
    link_id: str,
    payload: TemplateLinkReviewRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    try:
        _visible_rubric(db, rubric_id, principal)
        link = rubric_lifecycle.review_template_link(
            db,
            rubric_id,
            link_id,
            user_id,
            payload.decision,
            payload.reason,
        )
        db.commit()
    except rubric_lifecycle.RubricLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": link.id,
        "rule_id": link.rule_id,
        "review_status": link.review_status,
        "reviewed_by": link.reviewed_by,
        "reviewed_at": link.reviewed_at,
    }


@router.patch("/{rubric_id}", response_model=RubricRead)
def update_rubric(
    rubric_id: str,
    payload: RubricUpdate,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    rubric = _visible_rubric(db, rubric_id, principal)
    if rubric.status != "draft":
        raise HTTPException(status_code=400, detail="only draft rubrics can be edited; clone published rubrics first")

    updates = payload.model_dump(exclude_unset=True)
    versioned = db.scalar(
        select(RubricCompilation.id)
        .where(RubricCompilation.rubric_id == rubric.id)
        .limit(1)
    )
    if versioned is not None and updates:
        raise HTTPException(
            status_code=400,
            detail=(
                "versioned rubric content must be changed through AtomicRule "
                "editing/recompilation or clone-for-edit"
            ),
        )
    scored_run_id = db.scalar(select(ScoringRun.id).where(ScoringRun.rubric_id == rubric.id).limit(1))
    if scored_run_id and {"name", "version", "total_score", "criteria"}.intersection(updates):
        raise HTTPException(status_code=400, detail="cannot change scoring rubric fields after scoring runs exist; clone first")

    next_name = updates.get("name", rubric.name)
    next_version = updates.get("version", rubric.version)
    exists = _scoped_duplicate(
        db,
        name=next_name,
        version=next_version,
        visibility=rubric.visibility,
        principal=principal,
        exclude_id=rubric.id,
    )
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
            rubric.criteria.append(build_criterion(criterion, index))
    elif "total_score" in updates:
        _validate_criteria_total(rubric.total_score, rubric.criteria)

    db.commit()
    db.refresh(rubric)
    return _load_rubric(db, rubric_id)


@router.post("/{rubric_id}/publish", response_model=RubricRead)
def publish_rubric(
    rubric_id: str,
    payload: RubricPublishRequest | None = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    _visible_rubric(db, rubric_id, principal)
    compilation_id = payload.compilation_id if payload else None
    if not compilation_id:
        has_provenance = db.scalar(
            select(RubricCompilation.id)
            .where(RubricCompilation.rubric_id == rubric_id)
            .limit(1)
        )
        if has_provenance is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "legacy draft must be explicitly upgraded to provenance "
                    "before publication"
                ),
            )
        raise HTTPException(
            status_code=400,
            detail="compilation_id is required for provenance publication",
        )
    try:
        rubric_lifecycle.publish_rubric(
            db,
            rubric_id,
            compilation_id,
            user_id,
        )
        db.commit()
    except rubric_lifecycle.RubricLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _load_rubric(db, rubric_id)


def _load_rubric(db, rubric_id):
    return db.scalar(select(Rubric).where(Rubric.id == rubric_id).options(selectinload(Rubric.criteria)))


def _compiler_identity(parser_version: str) -> dict:
    return {
        "parser_version": parser_version,
        "compiler_version": "atomic-rule-compiler@1",
        "prompt_version": "m4-import-no-llm@1",
        "model_provider": None,
        "model_name": None,
        "sampling_params": {},
    }


def _manual_import_command(payload: RubricCreate) -> dict:
    return {
        "schema_version": rubric_pipeline.IMPORT_SCHEMA_VERSION,
        "source_kind": "manual_json",
        "rubric": payload.model_dump(mode="json"),
        "compiler": _compiler_identity("manual-json-parser@1"),
        "version": {"hash_scheme": "rubric-content-v2"},
    }


def _manual_recompile_command(rubric: Rubric, payload: RubricDraftRecompileRequest) -> dict:
    return {
        "schema_version": rubric_pipeline.IMPORT_SCHEMA_VERSION,
        "source_kind": "manual_json",
        "rubric": {
            "name": rubric.name,
            "version": rubric.version,
            "description": rubric.description,
            "total_score": float(rubric.total_score),
            "format_spec": dict(rubric.format_spec or {}),
            "criteria": [
                item.model_dump(mode="json") for item in payload.criteria
            ],
            "business_profile_key": payload.business_profile_key,
            "workflow_profile": payload.workflow_profile,
            "global_policy": dict(payload.global_policy),
        },
        "compiler": _compiler_identity("manual-json-parser@1"),
        "version": {
            "hash_scheme": "rubric-content-v2",
            "version": payload.version,
        },
        "draft_recompile": {
            "mode": "supersede_unpublished",
            "supersedes_compilation_id": payload.supersedes_compilation_id,
        },
    }


def _file_import_command(
    *,
    name: str,
    version: str,
    description: str | None,
    rules_file_name: str,
    template_file_name: str | None,
) -> dict:
    return {
        "schema_version": rubric_pipeline.IMPORT_SCHEMA_VERSION,
        "source_kind": "file_import",
        "rubric": {
            "name": name,
            "version": version,
            "description": description,
            "business_profile_key": "thesis",
            "workflow_profile": (
                "template_driven" if template_file_name else "manual_json"
            ),
        },
        "files": {
            "rules_file_name": rules_file_name,
            "template_file_name": template_file_name,
        },
        "compiler": _compiler_identity("excel-word-parser@1"),
        "version": {"hash_scheme": "rubric-content-v2"},
    }


def _rule_response(rule) -> dict:
    return {
        "id": rule.id,
        "rule_code": rule.rule_code,
        "status": rule.status,
        "reviewed_by": rule.reviewed_by,
        "reviewed_at": rule.reviewed_at,
    }


def _safe_scorer():
    # §5 规则归一化用的小模型；构造失败（如真实 LLM 未配好）则返回 None，导入不受阻、退化为仅 Excel 显式规则。
    try:
        return get_llm_scorer()
    except Exception:
        return None


def _validate_criteria_total(total_score, criteria):
    try:
        return validate_weight_configuration(criteria, total_score=total_score)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _is_excel_file(filename):
    return filename.lower().endswith((".xlsx", ".xlsm"))


def _is_docx_file(filename):
    return filename.lower().endswith(".docx")
