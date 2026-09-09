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
from backend.app.db.models import RubricVersion
from backend.app.db.models import ScoringRun
from backend.app.db.session import get_db
from backend.app.services.rubrics.coverage import build_rule_coverage
from backend.app.schemas.rubric import RubricCreate
from backend.app.schemas.rubric import RubricCloneRequest
from backend.app.schemas.rubric import AtomicRuleEditRequest
from backend.app.schemas.rubric import RubricImportResult
from backend.app.schemas.rubric import RubricLifecycleReason
from backend.app.schemas.rubric import RubricPublishRequest
from backend.app.schemas.rubric import RubricRead
from backend.app.schemas.rubric import RubricDraftRecompileRequest
from backend.app.schemas.rubric import RubricAIRuleDraftRequest
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
from backend.app.services.rubric_import.ai_rule_drafter import (
    AIRuleDraftValidationError,
    draft_deduction_rules,
)
from backend.app.services.rubric_import.compiler import analyze_rule_input
from backend.app.services.rubrics import lifecycle as rubric_lifecycle
from backend.app.services.rubrics.draft_graph import read_execution_draft
from backend.app.services.ai_connections import resolve_connection_runtime

router = APIRouter(prefix="/rubrics", tags=["rubrics"])


def _rubric_problem(
    *,
    code: str,
    message: str,
    user_action: str,
    severity: str = "error",
    retryable: bool = False,
    criterion_code: str | None = None,
    field_path: str | None = None,
    context: dict | None = None,
) -> dict:
    return {
        "code": code,
        "message": message,
        "user_action": user_action,
        "severity": severity,
        "retryable": retryable,
        "criterion_code": criterion_code,
        "field_path": field_path,
        "context": context or {},
    }


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
    # `_visible_rubric` 只查组织归属，不查角色。评分标准决定全组织的论文
    # 怎么被打分，写它必须过角色门控（v3 §4.2）。
    require_organization_role(principal, "org_admin", "teacher")
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


@router.get("/{rubric_id}/rule-coverage")
def get_rule_coverage(
    rubric_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """扣分细则完整度（计划 §6）。

    没有扣分规则的评分项是阻断项——评分到该项时没有判据可用。用户原文
    与 AI 起草分开计数：未经确认的 AI 规则不是用户认可的判据。
    """
    rubric = _visible_rubric(db, rubric_id, principal)
    return build_rule_coverage(db, rubric)


@router.post("/{rubric_id}/draft-deduction-rules")
def draft_rubric_deduction_rules(
    rubric_id: str,
    payload: RubricAIRuleDraftRequest,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """Return non-persistent, human-confirmable AI rule suggestions."""

    _visible_rubric(db, rubric_id, principal)
    # `_visible_rubric` 只查组织归属，不查角色。评分标准决定全组织的论文
    # 怎么被打分，写它必须过角色门控（v3 §4.2）。
    require_organization_role(principal, "org_admin", "teacher")
    execution = read_execution_draft(session=db, rubric_id=rubric_id)
    active = execution.get("active_compilation") or {}
    version = active.get("version") or {}
    try:
        if payload.ai_connection_id:
            if not principal.organization_id:
                raise AIRuleDraftValidationError(
                    "AI_DRAFT_CONNECTION_MISSING",
                    "当前上下文不能使用私有 AI 连接。",
                    "请选择组织后重试，或使用平台已授权的真实模型。",
                )
            runtime = resolve_connection_runtime(
                db,
                connection_id=payload.ai_connection_id,
                owner_id=principal.user_id,
                organization_id=principal.organization_id,
            )
            scorer = get_llm_scorer(runtime)
        else:
            scorer = get_llm_scorer(session=db)

        items = []
        for criterion in payload.criteria:
            criterion_value = criterion.model_dump(mode="json")
            analysis = analyze_rule_input(
                criterion_value.get("deduction_rules") or [],
                criterion_code=criterion.code,
            )
            if not (
                analysis["needs_ai_draft"]
                or analysis["needs_severity_expansion"]
            ):
                items.append(
                    {
                        "criterion_code": criterion.code,
                        "input_analysis": analysis,
                        "status": "already_structured",
                        "draft": None,
                    }
                )
                continue
            items.append(
                {
                    "criterion_code": criterion.code,
                    "input_analysis": analysis,
                    "status": "pending_confirmation",
                    "draft": draft_deduction_rules(
                        criterion=criterion_value,
                        input_analysis=analysis,
                        scorer=scorer,
                        business_profile_key=(
                            version.get("business_profile_key") or "thesis"
                        ),
                    ),
                }
            )
        return {"rubric_id": rubric_id, "items": items}
    except AIRuleDraftValidationError as exc:
        if exc.code == "AI_DRAFT_PROVIDER_REJECTED":
            status_code = 502
        elif exc.code in {
            "AI_DRAFT_CONNECTION_MISSING",
            "AI_DRAFT_PROVIDER_ERROR",
        }:
            status_code = 503
        else:
            status_code = 422
        raise HTTPException(
            status_code=status_code,
            detail=_rubric_problem(
                code=exc.code,
                message=exc.message,
                user_action=exc.user_action,
                retryable=exc.code == "AI_DRAFT_PROVIDER_ERROR",
            ),
        ) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail=_rubric_problem(
                code="AI_DRAFT_CONNECTION_MISSING",
                message="当前没有可用于起草扣分细则的真实 AI 连接。",
                user_action="请配置真实 AI 连接，或将评分项改为仅人工复核。",
                retryable=False,
            ),
        ) from exc


@router.post("/{rubric_id}/submit-review", response_model=RubricRead)
def submit_rubric_review(
    rubric_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    try:
        _visible_rubric(db, rubric_id, principal)
        # `_visible_rubric` 只查组织归属，不查角色。评分标准决定全组织的论文
        # 怎么被打分，写它必须过角色门控（v3 §4.2）。
        require_organization_role(principal, "org_admin", "teacher")
        execution = read_execution_draft(session=db, rubric_id=rubric_id)
        active = execution.get("active_compilation")
        blocker_count = len((active or {}).get("blockers") or [])
        if (
            active is None
            or active.get("status") != "validated"
            or blocker_count
        ):
            raise HTTPException(
                status_code=409,
                detail=_rubric_problem(
                    code="RUBRIC_REVIEW_BLOCKED",
                    message=(
                        f"当前执行草稿仍有 {blocker_count} 个阻断项，不能提交审核。"
                        if blocker_count
                        else "当前模板还没有校验通过的执行草稿，不能提交审核。"
                    ),
                    user_action="请返回第 2 步处理阻断项并保存重新校验。",
                    retryable=False,
                    context={"blocker_count": blocker_count},
                ),
            )
        rubric_lifecycle.submit_for_review(db, rubric_id)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
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
        # `_visible_rubric` 只查组织归属，不查角色。评分标准决定全组织的论文
        # 怎么被打分，写它必须过角色门控（v3 §4.2）。
        require_organization_role(principal, "org_admin", "teacher")
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
    # `_visible_rubric` 只查组织归属，不查角色。评分标准决定全组织的论文
    # 怎么被打分，写它必须过角色门控（v3 §4.2）。
    require_organization_role(principal, "org_admin", "teacher")
    if rubric.status != "draft":
        raise HTTPException(
            status_code=409,
            detail=_rubric_problem(
                code="RUBRIC_NOT_EDITABLE",
                message="当前评分模板不是可编辑草稿。",
                user_action="已发布模板请先复制为新版本后再编辑。",
            ),
        )
    predecessor_version = db.scalar(
        select(RubricVersion).where(
            RubricVersion.compilation_id == payload.supersedes_compilation_id,
            RubricVersion.rubric_id == rubric_id,
        )
    )
    if predecessor_version is None:
        raise HTTPException(
            status_code=409,
            detail=_rubric_problem(
                code="RUBRIC_RECOMPILE_STALE",
                message="当前编辑基于的执行草稿已经失效。",
                user_action="请保留当前修改并刷新最新执行草稿后重试。",
                retryable=True,
            ),
        )
    next_total = payload.total_score or float(rubric.total_score)
    _validate_criteria_total(next_total, payload.criteria)
    next_name = payload.name or rubric.name
    duplicate = _scoped_duplicate(
        db,
        name=next_name,
        version=payload.version,
        visibility=rubric.visibility,
        principal=principal,
        exclude_id=rubric.id,
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=_rubric_problem(
                code="RUBRIC_NAME_VERSION_CONFLICT",
                message="同一范围内已经存在相同名称和版本的模板。",
                user_action="请修改模板名称或版本后重新保存。",
                retryable=False,
                field_path="/version",
            ),
        )
    used_versions = set(
        db.scalars(
            select(RubricVersion.version).where(
                RubricVersion.rubric_id == rubric.id
            )
        ).all()
    )
    compiled_version = payload.version
    if compiled_version in used_versions:
        ordinal = 2
        while f"{payload.version}-draft.{ordinal}" in used_versions:
            ordinal += 1
        compiled_version = f"{payload.version}-draft.{ordinal}"
    command = _manual_recompile_command(
        rubric,
        payload,
        predecessor_version=predecessor_version,
        compiled_version=compiled_version,
    )
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
        raise HTTPException(
            status_code=422,
            detail=_rubric_problem(
                code="RUBRIC_RECOMPILE_BLOCKED",
                message="当前修改无法生成新的执行草稿。",
                user_action="请根据第 2 步字段提示修正规则后重试。",
                retryable=False,
                context={"reason": str(exc)},
            ),
        ) from exc
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
        # `_visible_rubric` 只查组织归属，不查角色。评分标准决定全组织的论文
        # 怎么被打分，写它必须过角色门控（v3 §4.2）。
        require_organization_role(principal, "org_admin", "teacher")
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
        # `_visible_rubric` 只查组织归属，不查角色。评分标准决定全组织的论文
        # 怎么被打分，写它必须过角色门控（v3 §4.2）。
        require_organization_role(principal, "org_admin", "teacher")
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
        # `_visible_rubric` 只查组织归属，不查角色。评分标准决定全组织的论文
        # 怎么被打分，写它必须过角色门控（v3 §4.2）。
        require_organization_role(principal, "org_admin", "teacher")
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
        # `_visible_rubric` 只查组织归属，不查角色。评分标准决定全组织的论文
        # 怎么被打分，写它必须过角色门控（v3 §4.2）。
        require_organization_role(principal, "org_admin", "teacher")
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
        # `_visible_rubric` 只查组织归属，不查角色。评分标准决定全组织的论文
        # 怎么被打分，写它必须过角色门控（v3 §4.2）。
        require_organization_role(principal, "org_admin", "teacher")
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
        # `_visible_rubric` 只查组织归属，不查角色。评分标准决定全组织的论文
        # 怎么被打分，写它必须过角色门控（v3 §4.2）。
        require_organization_role(principal, "org_admin", "teacher")
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
    # `_visible_rubric` 只查组织归属，不查角色。评分标准决定全组织的论文
    # 怎么被打分，写它必须过角色门控（v3 §4.2）。
    require_organization_role(principal, "org_admin", "teacher")
    if rubric.status != "draft":
        raise HTTPException(
            status_code=409,
            detail=_rubric_problem(
                code="RUBRIC_NOT_EDITABLE",
                message="当前评分模板不是可编辑草稿。",
                user_action="已发布模板请先复制为新版本后再编辑。",
            ),
        )

    updates = payload.model_dump(exclude_unset=True)
    versioned = db.scalar(
        select(RubricCompilation.id)
        .where(RubricCompilation.rubric_id == rubric.id)
        .limit(1)
    )
    if versioned is not None and updates:
        raise HTTPException(
            status_code=409,
            detail=_rubric_problem(
                code="RUBRIC_RECOMPILE_REQUIRED",
                message="该模板已经有执行草稿，不能直接覆盖历史内容。",
                user_action="请使用第 2 步的“保存并重新校验”生成新的执行草稿。",
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
    # `_visible_rubric` 只查组织归属，不查角色。评分标准决定全组织的论文
    # 怎么被打分，写它必须过角色门控（v3 §4.2）。
    require_organization_role(principal, "org_admin", "teacher")

    # 分享范围（D-029）。不传就沿用当前范围——扩大范围必须是显式动作。
    #
    # 「本组织」此前需要 `org_admin`，这里放宽为创建者本人即可：导入默认仅自己
    # 可见，若共享仍需审批，每份标准都要走一次管理员才能给同事用。
    # 「所有人」跨组织生效，影响面不同，维持 `platform_admin`。
    requested_visibility = payload.visibility if payload else None
    if requested_visibility == "system":
        if principal.platform_role != "platform_admin":
            raise HTTPException(
                status_code=403,
                detail="只有平台管理员可以把评分标准分享给所有组织。",
            )
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
        # 范围必须**先于** publish 写入：有 ORM 守卫拦住「已发布评分标准不可修改」，
        # 发布后再改会直接抛错。两者仍在同一个事务里，要么一起生效要么一起回滚
        # （D-029）——分两次提交会留下「已发布但范围还是旧的」这个没有补救入口的
        # 中间态，因为发布后范围就冻结了。
        if requested_visibility is not None:
            target = db.get(Rubric, rubric_id)
            if target is not None:
                target.visibility = requested_visibility
                db.flush()
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


def _manual_recompile_command(
    rubric: Rubric,
    payload: RubricDraftRecompileRequest,
    *,
    predecessor_version: RubricVersion,
    compiled_version: str,
) -> dict:
    return {
        "schema_version": rubric_pipeline.IMPORT_SCHEMA_VERSION,
        "source_kind": "manual_json",
        "rubric": {
            "name": payload.name or rubric.name,
            "version": payload.version,
            "description": (
                payload.description
                if "description" in payload.model_fields_set
                else rubric.description
            ),
            "total_score": payload.total_score or float(rubric.total_score),
            "format_spec": dict(rubric.format_spec or {}),
            "criteria": [
                item.model_dump(mode="json") for item in payload.criteria
            ],
            "business_profile_key": predecessor_version.business_profile_key,
            "workflow_profile": predecessor_version.workflow_profile,
            "global_policy": dict(predecessor_version.global_policy or {}),
        },
        "compiler": _compiler_identity("manual-json-parser@1"),
        "version": {
            "hash_scheme": "rubric-content-v2",
            "version": compiled_version,
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
