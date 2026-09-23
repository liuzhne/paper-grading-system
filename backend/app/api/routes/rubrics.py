from typing import Optional
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from decimal import Decimal
from decimal import ROUND_HALF_UP
from types import SimpleNamespace

import json

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import Response
from fastapi import UploadFile
from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from backend.app.db.models import Rubric
from backend.app.db.models import AtomicRule
from backend.app.db.models import RubricCompilation
from backend.app.db.models import RubricVersion
from backend.app.db.models import RubricImportSession
from backend.app.db.models import ScoringRun
from backend.app.db.session import get_db
from backend.app.services.rubrics.coverage import build_rule_coverage
from backend.app.schemas.rubric import RubricCreate
from backend.app.schemas.rubric import SourceUnitBatchResolveRequest
from backend.app.schemas.rubric import FindingDismissRequest
from backend.app.schemas.rubric import RuleReviewRequest
from backend.app.schemas.rubric import StructureMergeRequest
from backend.app.schemas.rubric import StructureSuggestionRequest
from backend.app.schemas.rubric import StructureUndoRequest
from backend.app.schemas.rubric import UnitClassificationRequest
from backend.app.schemas.rubric import SourceUnitResolveRequest
from backend.app.schemas.rubric import RubricCloneRequest
from backend.app.schemas.rubric import AtomicRuleEditRequest
from backend.app.schemas.rubric import AtomicRuleConfirmRequest
from backend.app.schemas.rubric import RubricImportResult
from backend.app.schemas.rubric import RubricImportSessionConfirm
from backend.app.schemas.rubric import RubricImportSessionConfirmResult
from backend.app.schemas.rubric import RubricImportConflictResolve
from backend.app.schemas.rubric import RubricImportSessionRead
from backend.app.schemas.rubric import RubricImportSessionUpdate
from backend.app.schemas.rubric import RubricImportSessionStateRequest
from backend.app.schemas.rubric import RubricImportReuploadPreviewResponse
from backend.app.schemas.rubric import RubricImportSourcePreview
from backend.app.schemas.rubric import RubricSourceWorkspaceRead
from backend.app.schemas.rubric import RubricReuploadPreviewResponse
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
from backend.app.services.rubric_import import parse_state
from backend.app.services.rubric_import import review_state
from backend.app.services.rubric_import import structure_state
from backend.app.services.rubric_import.extraction.structure_override import StructureOverrideError
from backend.app.services.rubric_import.persist import build_criterion
from backend.app.services.rubric_import.persist import extra_criterion_fields
from backend.app.services.rubric_import.template import build_rubric_import_template
from backend.app.services.scoring.core.policy import validate_weight_configuration
from backend.app.services.scoring.core.policy import compile_scoring_policy
from backend.app.services.rubric_import import pipeline as rubric_pipeline
from backend.app.services.rubric_import import import_sessions
from backend.app.services.rubric_import.source_workspace import read_source_workspace
from backend.app.services.rubric_import.ai_rule_drafter import (
    AIRuleDraftValidationError,
    draft_deduction_rules,
)
from backend.app.services.rubric_import.compiler import analyze_rule_input
from backend.app.services.rubrics import lifecycle as rubric_lifecycle
from backend.app.services.rubrics.draft_graph import read_execution_draft
from backend.app.services.rubrics.review_workspace import read_review_workspace, confirm_rule
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


def _visible_import_session(
    db: Session,
    session_id: str,
    principal: CurrentPrincipal,
    *,
    for_update: bool = False,
) -> RubricImportSession:
    query = select(RubricImportSession).where(RubricImportSession.id == session_id)
    if for_update:
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None:
        raise HTTPException(status_code=404, detail="rubric import session not found")
    allowed = (
        not auth_active()
        or principal.platform_role == "platform_admin"
        or row.owner_id == principal.user_id
        or row.visibility == "organization"
        and row.organization_id == principal.organization_id
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="rubric import session not found")
    if row.status == "draft" and row.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
        row.status = "expired"
        row.state_version += 1
        db.commit()
        raise HTTPException(status_code=410, detail="rubric import session has expired")
    return row


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


@router.post(
    "/import-sessions",
    response_model=RubricImportSessionRead,
    status_code=201,
)
def create_rubric_import_session(
    name: str = Form(...),
    version: str = Form("v1.0"),
    description: Optional[str] = Form(None),
    visibility: str = Form("private"),
    rules_file: Optional[UploadFile] = File(None),
    template_file: Optional[UploadFile] = File(None),
    structure_override: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """确定性解析文件并保存临时草稿；确认前不创建 Rubric。"""

    ensure_dev_user(db)
    require_organization_role(principal, "org_admin", "teacher")
    if visibility not in {"private", "organization", "system"}:
        raise HTTPException(status_code=422, detail="invalid rubric visibility")
    if visibility == "system" and principal.platform_role != "platform_admin":
        raise HTTPException(status_code=403, detail="only platform admins can create system rubrics")
    if visibility == "organization":
        require_organization_role(principal, "org_admin")
    if rules_file is None and template_file is None:
        raise HTTPException(status_code=400, detail="请至少上传一份评分标准文件（Word 或 Excel）")
    if rules_file is not None and not _is_excel_file(rules_file.filename or ""):
        raise HTTPException(status_code=400, detail="rules_file must be an .xlsx or .xlsm file")
    if template_file is not None and not _is_docx_file(template_file.filename or ""):
        raise HTTPException(status_code=400, detail="template_file must be a .docx file")

    override = None
    if structure_override:
        try:
            override = json.loads(structure_override)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="structure_override must be valid JSON") from exc
    rules_bytes = rules_file.file.read() if rules_file else None
    template_bytes = template_file.file.read() if template_file else None
    try:
        prepared = rubric_pipeline.prepare_file_import(
            command=_file_import_command(
                name=name,
                version=version,
                description=description,
                rules_file_name=(rules_file.filename if rules_file else None),
                template_file_name=(template_file.filename if template_file else None),
            ),
            rules_bytes=rules_bytes,
            template_bytes=template_bytes,
            scorer=None,
            structure_override=override,
        )
        row = import_sessions.create(
            db,
            prepared=prepared.to_mapping(),
            actor_id=user_id,
            organization_id=(None if visibility == "system" else principal.organization_id),
            visibility=visibility,
            rules_file_name=(rules_file.filename if rules_file else None),
            rules_file_bytes=rules_bytes,
            template_file_name=(template_file.filename if template_file else None),
            template_file_bytes=template_bytes,
        )
    except StructureOverrideError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return import_sessions.serialize(row)


@router.get("/import-sessions/{session_id}", response_model=RubricImportSessionRead)
def get_rubric_import_session(
    session_id: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    return import_sessions.serialize(_visible_import_session(db, session_id, principal))


@router.get(
    "/import-sessions/{session_id}/source-preview",
    response_model=RubricImportSourcePreview,
)
def preview_rubric_import_source(
    session_id: str,
    document: str,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    if document not in {"word", "excel"}:
        raise HTTPException(status_code=422, detail="document must be word or excel")
    row = _visible_import_session(db, session_id, principal)
    raw = (row.prepared_graph.get("compilation") or {}).get("raw_parse_output") or {}
    ledger = raw.get("source_ledger") or {}
    items = [
        {
            "unit_id": item.get("unit_id"),
            "kind": item.get("kind"),
            "locator": item.get("context") or {},
            "text": item.get("text") or "",
        }
        for item in ledger.get("units") or []
        if item.get("doc_id") == document
    ]
    return {"document": document, "items": items}


@router.patch("/import-sessions/{session_id}", response_model=RubricImportSessionRead)
def update_rubric_import_session(
    session_id: str,
    payload: RubricImportSessionUpdate,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    require_organization_role(principal, "org_admin", "teacher")
    row = _visible_import_session(db, session_id, principal, for_update=True)
    try:
        import_sessions.update(row, payload.model_dump(exclude_unset=True, mode="json"))
        db.commit()
        db.refresh(row)
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return import_sessions.serialize(row)


@router.post(
    "/import-sessions/{session_id}/conflicts/{conflict_id:path}/resolve",
    response_model=RubricImportSessionRead,
)
def resolve_rubric_import_session_conflict(
    session_id: str,
    conflict_id: str,
    payload: RubricImportConflictResolve,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    require_organization_role(principal, "org_admin", "teacher")
    row = _visible_import_session(db, session_id, principal, for_update=True)
    try:
        import_sessions.resolve_conflict(
            row,
            conflict_id,
            expected_state_version=payload.expected_state_version,
            decision=payload.decision,
            reason=payload.reason,
        )
        db.commit()
        db.refresh(row)
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return import_sessions.serialize(row)


@router.post(
    "/import-sessions/{session_id}/cancel",
    response_model=RubricImportSessionRead,
)
def cancel_rubric_import_session(
    session_id: str,
    payload: RubricImportSessionStateRequest,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    require_organization_role(principal, "org_admin", "teacher")
    row = _visible_import_session(db, session_id, principal, for_update=True)
    try:
        import_sessions.cancel(row, expected_state_version=payload.expected_state_version)
        db.commit()
        db.refresh(row)
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return import_sessions.serialize(row)


def _prepare_import_session_reupload(
    row: RubricImportSession,
    *,
    rules_file: UploadFile | None,
    template_file: UploadFile | None,
) -> tuple[dict, dict, list[dict], list[dict], dict]:
    """用新文件与会话中未替换的旧文件生成确定性差异，不写数据库。"""

    if rules_file is not None and not _is_excel_file(rules_file.filename or ""):
        raise HTTPException(status_code=400, detail="rules_file must be an .xlsx or .xlsm file")
    if template_file is not None and not _is_docx_file(template_file.filename or ""):
        raise HTTPException(status_code=400, detail="template_file must be a .docx file")
    if rules_file is None and template_file is None:
        raise HTTPException(status_code=400, detail="请至少重新上传一份评分标准文件")

    rules_bytes = rules_file.file.read() if rules_file is not None else row.rules_file_bytes
    template_bytes = (
        template_file.file.read() if template_file is not None else row.template_file_bytes
    )
    rules_name = rules_file.filename if rules_file is not None else row.rules_file_name
    template_name = (
        template_file.filename if template_file is not None else row.template_file_name
    )
    try:
        prepared = rubric_pipeline.prepare_file_import(
            command=_file_import_command(
                name=row.name,
                version=row.version,
                description=row.description,
                rules_file_name=rules_name,
                template_file_name=template_name,
            ),
            rules_bytes=rules_bytes,
            template_bytes=template_bytes,
            scorer=None,
        ).to_mapping()
        draft, adjustments, differences = import_sessions.reupload_diff(row, prepared)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    files = {
        "rules_file_name": rules_name,
        "rules_file_bytes": rules_bytes,
        "template_file_name": template_name,
        "template_file_bytes": template_bytes,
    }
    return prepared, draft, adjustments, differences, files


@router.post(
    "/import-sessions/{session_id}/reupload-preview",
    response_model=RubricImportReuploadPreviewResponse,
)
def preview_rubric_import_session_reupload(
    session_id: str,
    expected_state_version: int = Form(...),
    rules_file: Optional[UploadFile] = File(None),
    template_file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    require_organization_role(principal, "org_admin", "teacher")
    row = _visible_import_session(db, session_id, principal)
    if row.status != "draft":
        raise HTTPException(status_code=409, detail="只有待确认的导入会话可以重新上传")
    if row.state_version != expected_state_version:
        raise HTTPException(status_code=409, detail="导入草稿已更新，请刷新后重试")
    prepared, _, adjustments, differences, files = _prepare_import_session_reupload(
        row,
        rules_file=rules_file,
        template_file=template_file,
    )
    fingerprint = import_sessions.reupload_fingerprint(
        row,
        rules_bytes=files["rules_file_bytes"],
        template_bytes=files["template_file_bytes"],
    )
    counts = {
        kind: sum(item["change_type"] == kind for item in differences)
        for kind in ("added", "removed", "modified", "unchanged")
    }
    return {
        "fingerprint": fingerprint,
        "criteria_diff": differences,
        "added_count": counts["added"],
        "removed_count": counts["removed"],
        "modified_count": counts["modified"],
        "unchanged_count": counts["unchanged"],
        "score_adjustments": adjustments,
        "warnings": _prepared_warning_messages(prepared),
    }


@router.post(
    "/import-sessions/{session_id}/reupload-confirm",
    response_model=RubricImportSessionRead,
)
def confirm_rubric_import_session_reupload(
    session_id: str,
    expected_state_version: int = Form(...),
    fingerprint: str = Form(...),
    rules_file: Optional[UploadFile] = File(None),
    template_file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    require_organization_role(principal, "org_admin", "teacher")
    row = _visible_import_session(db, session_id, principal, for_update=True)
    if row.status != "draft":
        raise HTTPException(status_code=409, detail="只有待确认的导入会话可以重新上传")
    if row.state_version != expected_state_version:
        raise HTTPException(status_code=409, detail="导入草稿已更新，请重新预览差异")
    prepared, draft, adjustments, _, files = _prepare_import_session_reupload(
        row,
        rules_file=rules_file,
        template_file=template_file,
    )
    current_fingerprint = import_sessions.reupload_fingerprint(
        row,
        rules_bytes=files["rules_file_bytes"],
        template_bytes=files["template_file_bytes"],
    )
    if fingerprint != current_fingerprint:
        raise HTTPException(status_code=409, detail="重新上传文件已变化，请重新预览差异")
    import_sessions.apply_reupload(
        row,
        prepared=prepared,
        draft=draft,
        adjustments=adjustments,
        **files,
    )
    db.commit()
    db.refresh(row)
    return import_sessions.serialize(row)


@router.post(
    "/import-sessions/{session_id}/confirm",
    response_model=RubricImportSessionConfirmResult,
)
def confirm_rubric_import_session(
    session_id: str,
    payload: RubricImportSessionConfirm,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    require_organization_role(principal, "org_admin", "teacher")
    row = _visible_import_session(db, session_id, principal)
    if row.status == "confirmed":
        if row.confirmation_key != payload.idempotency_key:
            raise HTTPException(status_code=409, detail="导入会话已经使用其他幂等键确认")
        rubric = _load_rubric(db, row.rubric_id)
        return {
            "status": "confirmed",
            "import_session_id": row.id,
            "state_version": row.state_version,
            "rubric": rubric,
        }
    if row.status != "draft":
        raise HTTPException(status_code=409, detail="只有待确认的导入会话可以确认")
    if row.state_version != payload.expected_state_version:
        raise HTTPException(status_code=409, detail="导入草稿已更新，请刷新后重试")
    try:
        prepared = import_sessions.prepared_for_confirmation(row)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    exists = _scoped_duplicate(
        db,
        name=row.name,
        version=row.version,
        visibility=row.visibility,
        principal=principal,
    )
    if exists is not None:
        raise HTTPException(status_code=400, detail="rubric name and version already exist")
    visibility = row.visibility
    organization_id = row.organization_id
    state_version = row.state_version
    db.commit()
    try:
        identity = rubric_pipeline.persist_prepared_import(
            session=db,
            prepared=prepared,
            actor_id=user_id,
            organization_id=organization_id,
            visibility=visibility,
            import_session_id=session_id,
            import_session_state_version=state_version,
            confirmation_key=payload.idempotency_key,
        )
    except ValueError as exc:
        message = str(exc)
        status = 409 if "stale" in message or "already confirmed" in message else 422
        raise HTTPException(status_code=status, detail=message) from exc
    confirmed = db.get(RubricImportSession, session_id)
    return {
        "status": "confirmed",
        "import_session_id": session_id,
        "state_version": confirmed.state_version,
        "rubric": _load_rubric(db, identity.rubric_id),
    }


@router.get("/{rubric_id}/source-workspace", response_model=RubricSourceWorkspaceRead)
def get_rubric_source_workspace(
    rubric_id: str,
    response: Response,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    rubric = _visible_rubric(db, rubric_id, principal)
    response.headers["Cache-Control"] = "private, no-store"
    return read_source_workspace(session=db, rubric=rubric)


def _prepare_confirmed_rubric_reupload(
    db: Session,
    rubric: Rubric,
    *,
    rules_file: UploadFile | None,
    template_file: UploadFile | None,
) -> dict:
    if rules_file is not None and not _is_excel_file(rules_file.filename or ""):
        raise HTTPException(status_code=400, detail="rules_file must be an .xlsx or .xlsm file")
    if template_file is not None and not _is_docx_file(template_file.filename or ""):
        raise HTTPException(status_code=400, detail="template_file must be a .docx file")
    if rules_file is None and template_file is None:
        raise HTTPException(status_code=400, detail="请至少重新上传一份评分标准文件")
    if rubric.status != "draft":
        raise HTTPException(status_code=409, detail="已发布或审核中的评分标准不能被重新上传覆盖，请先复制为新版本")

    active = db.scalar(
        select(RubricCompilation)
        .where(
            RubricCompilation.rubric_id == rubric.id,
            RubricCompilation.published_at.is_(None),
            RubricCompilation.status != "superseded",
        )
        .order_by(RubricCompilation.created_at.desc())
    )
    if active is None:
        raise HTTPException(status_code=409, detail="当前没有可替换的活动执行草稿")
    source_session = db.scalar(
        select(RubricImportSession)
        .where(
            RubricImportSession.rubric_id == rubric.id,
            RubricImportSession.status == "confirmed",
        )
        .order_by(RubricImportSession.created_at.desc())
    )
    rules_bytes = rules_file.file.read() if rules_file is not None else (
        source_session.rules_file_bytes if source_session is not None else None
    )
    template_bytes = template_file.file.read() if template_file is not None else (
        source_session.template_file_bytes if source_session is not None else None
    )
    rules_name = rules_file.filename if rules_file is not None else (
        source_session.rules_file_name if source_session is not None else None
    )
    template_name = template_file.filename if template_file is not None else (
        source_session.template_file_name if source_session is not None else None
    )
    try:
        prepared = rubric_pipeline.prepare_file_import(
            command=_file_import_command(
                name=rubric.name,
                version=rubric.version,
                description=rubric.description,
                rules_file_name=rules_name,
                template_file_name=template_name,
            ),
            rules_bytes=rules_bytes,
            template_bytes=template_bytes,
            scorer=None,
        ).to_mapping()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    baseline = (
        deepcopy(source_session.prepared_graph)
        if source_session is not None
        else {
            "legacy_projection": {
                "criteria": [
                    {
                        "code": item.code,
                        "name": item.name,
                        "max_score": str(item.max_score),
                        "description": item.description,
                        "display_order": item.display_order,
                    }
                    for item in rubric.criteria
                ]
            },
            "source_rules": [],
            "compilation": {"raw_parse_output": {}},
        }
    )
    previous_by_code = {
        item.get("code"): item
        for item in ((source_session.draft_data or {}).get("criteria") if source_session else [])
    }
    current_criteria = []
    for item in rubric.criteria:
        rounded = int(
            Decimal(str(item.max_score)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        prior = previous_by_code.get(item.code) or {}
        current_criteria.append(
            {
                "code": item.code,
                "name": item.name,
                "max_score": rounded,
                "description": item.description,
                "display_order": item.display_order,
                "source_refs": deepcopy(prior.get("source_refs") or []),
                "parse_status": prior.get("parse_status") or "confirmed",
                "deleted": False,
            }
        )
    comparison = SimpleNamespace(
        prepared_graph=baseline,
        draft_data={"criteria": current_criteria},
    )
    try:
        draft, adjustments, differences = import_sessions.reupload_diff(comparison, prepared)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    fingerprint_row = SimpleNamespace(id=rubric.id, state_version=active.id)
    fingerprint = import_sessions.reupload_fingerprint(
        fingerprint_row,
        rules_bytes=rules_bytes,
        template_bytes=template_bytes,
    )
    return {
        "active": active,
        "source_session": source_session,
        "prepared": prepared,
        "draft": draft,
        "adjustments": adjustments,
        "differences": differences,
        "fingerprint": fingerprint,
        "rules_file_name": rules_name,
        "rules_file_bytes": rules_bytes,
        "template_file_name": template_name,
        "template_file_bytes": template_bytes,
    }


def _prepared_warning_messages(prepared: dict) -> list[str]:
    return [
        item if isinstance(item, str) else str(item.get("message") or item.get("code"))
        for item in prepared["compilation"].get("warnings") or []
    ]


def _carry_unchanged_reupload_content(
    db: Session,
    rubric: Rubric,
    context: dict,
    graph: dict,
) -> None:
    """按已确认策略保留未变化评分项的人工上下文与原子规则。"""

    unchanged = {
        item["code"]
        for item in context["differences"]
        if item["change_type"] == "unchanged"
    }
    if not unchanged:
        return
    current = {item.code: item for item in rubric.criteria if item.code in unchanged}
    preserved_fields = (
        "description",
        "weight",
        "evidence_hints",
        "criterion_type",
        "scoring_mode",
        "applies_to",
        "rubric_levels",
        "sub_checks",
        "dimension",
        "deduction_rules_structured",
    )
    for collection_name in ("criteria",):
        for item in graph.get(collection_name) or []:
            source = current.get(item["code"])
            if source is not None:
                item.update({field: deepcopy(getattr(source, field)) for field in preserved_fields})
    for item in graph.get("legacy_projection", {}).get("criteria") or []:
        source = current.get(item["code"])
        if source is not None:
            item.update({field: deepcopy(getattr(source, field)) for field in preserved_fields})

    version = db.scalar(
        select(RubricVersion).where(
            RubricVersion.compilation_id == context["active"].id,
            RubricVersion.rubric_id == rubric.id,
        )
    )
    if version is None:
        return
    rules = db.scalars(
        select(AtomicRule).where(AtomicRule.rubric_version_id == version.id)
    ).all()
    if not rules:
        return
    from backend.app.services.rubrics.atomic_recompile import prepare_atomic_recompile
    from backend.app.services.rubrics.review_workspace import content_token

    snapshot_command = {
        "rubric": {
            **deepcopy(graph["rubric"]),
            "criteria": deepcopy(graph["criteria"]),
            "global_policy": deepcopy(version.global_policy or {}),
            "business_profile_key": version.business_profile_key,
            "workflow_profile": version.workflow_profile,
        },
        "compiler": _compiler_identity("excel-word-parser@1"),
        "version": deepcopy(graph["version"]),
        "draft_recompile": deepcopy(graph["draft_recompile"]),
    }
    edits = [
        {"id": rule.id, "content_token": content_token(rule), "changes": {}}
        for rule in rules
    ]
    snapshot = prepare_atomic_recompile(db, version, snapshot_command, edits).to_mapping()
    carried_rules = [
        item for item in snapshot["atomic_rules"]
        if item["criterion_code"] in unchanged
    ]
    carried_codes = {item["rule_code"] for item in carried_rules}
    graph["atomic_rules"] = [
        item for item in graph["atomic_rules"]
        if item["criterion_code"] not in unchanged
    ] + carried_rules
    graph["artifacts"].extend(snapshot["artifacts"])
    graph["source_rules"].extend(snapshot["source_rules"])
    graph["template_items"].extend(snapshot["template_items"])
    graph["template_links"] = [
        item for item in graph["template_links"]
        if item["rule_code"] not in carried_codes
    ] + [
        item for item in snapshot["template_links"]
        if item["rule_code"] in carried_codes
    ]


@router.post(
    "/{rubric_id}/reupload-preview",
    response_model=RubricReuploadPreviewResponse,
)
def preview_confirmed_rubric_reupload(
    rubric_id: str,
    rules_file: Optional[UploadFile] = File(None),
    template_file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    rubric = _visible_rubric(db, rubric_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    context = _prepare_confirmed_rubric_reupload(
        db, rubric, rules_file=rules_file, template_file=template_file
    )
    from backend.app.services.rubrics.review_carry import calculate_carry_diff

    active_criteria = [item for item in context["draft"]["criteria"] if not item.get("deleted")]
    retained, invalidated = calculate_carry_diff(
        db,
        context["active"],
        active_criteria,
    )
    counts = {
        kind: sum(item["change_type"] == kind for item in context["differences"])
        for kind in ("added", "removed", "modified", "unchanged")
    }
    is_rules = rules_file is not None
    return {
        "fingerprint": context["fingerprint"],
        "file_type": "rules" if is_rules else "template",
        "filename": (
            context["rules_file_name"] if is_rules else context["template_file_name"]
        ),
        "criteria_diff": context["differences"],
        "added_count": counts["added"],
        "removed_count": counts["removed"],
        "modified_count": counts["modified"],
        "unchanged_count": counts["unchanged"],
        "invalidated_rules_count": invalidated,
        "retained_rules_count": retained,
        "warnings": _prepared_warning_messages(context["prepared"]),
    }


@router.post("/{rubric_id}/reupload-confirm", response_model=RubricRead)
def confirm_confirmed_rubric_reupload(
    rubric_id: str,
    fingerprint: str = Form(...),
    rules_file: Optional[UploadFile] = File(None),
    template_file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    rubric = _visible_rubric(db, rubric_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    context = _prepare_confirmed_rubric_reupload(
        db, rubric, rules_file=rules_file, template_file=template_file
    )
    if fingerprint != context["fingerprint"]:
        raise HTTPException(status_code=409, detail="重新上传文件或当前执行草稿已变化，请重新预览差异")
    total_score = sum(
        int(item["max_score"])
        for item in context["draft"]["criteria"]
        if not item.get("deleted")
    )
    projection = SimpleNamespace(
        status="draft",
        state_version=1,
        name=rubric.name,
        version=rubric.version,
        description=rubric.description,
        total_score=total_score,
        prepared_graph=context["prepared"],
        draft_data=context["draft"],
    )
    try:
        graph = import_sessions.prepared_for_confirmation(projection)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    graph["draft_recompile"] = {
        "mode": rubric_pipeline.RUBRIC_REUPLOAD_MODE,
        "supersedes_compilation_id": context["active"].id,
    }
    _carry_unchanged_reupload_content(db, rubric, context, graph)
    source_session = context["source_session"]
    db.commit()
    try:
        identity = rubric_pipeline.persist_prepared_import(
            session=db,
            prepared=graph,
            actor_id=user_id,
            target_rubric_id=rubric_id,
            reason="用户确认重新上传评分模板差异",
            file_payloads={
                "excel": context["rules_file_bytes"],
                "word": context["template_file_bytes"],
            },
            reupload_session_id=(source_session.id if source_session is not None else None),
            reupload_session_state_version=(
                source_session.state_version if source_session is not None else None
            ),
            reupload_session_update=(
                {
                    "prepared_graph": context["prepared"],
                    "draft_data": context["draft"],
                    "warnings": _prepared_warning_messages(context["prepared"]),
                    "score_adjustments": context["adjustments"],
                    "rules_file_name": context["rules_file_name"],
                    "rules_file_bytes": context["rules_file_bytes"],
                    "template_file_name": context["template_file_name"],
                    "template_file_bytes": context["template_file_bytes"],
                    "total_score": total_score,
                }
                if source_session is not None
                else None
            ),
        )
    except ValueError as exc:
        status = 409 if "stale" in str(exc) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _load_rubric(db, identity.rubric_id)


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
    rules_file: Optional[UploadFile] = File(None),
    template_file: Optional[UploadFile] = File(None),
    structure_override: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    override = None
    if structure_override:
        try:
            override = json.loads(structure_override)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="structure_override must be valid JSON") from exc
    if visibility not in {"private", "organization", "system"}:
        raise HTTPException(status_code=422, detail="invalid rubric visibility")
    if visibility == "system":
        if principal.platform_role != "platform_admin":
            raise HTTPException(status_code=403, detail="only platform admins can create system rubrics")
    elif visibility == "organization":
        require_organization_role(principal, "org_admin")
    if rules_file is None and template_file is None:
        raise HTTPException(status_code=400, detail="请至少上传一份评分标准文件（Word 或 Excel）")
    if rules_file is not None and not _is_excel_file(rules_file.filename or ""):
        raise HTTPException(status_code=400, detail="rules_file must be an .xlsx or .xlsm file")
    if template_file and not _is_docx_file(template_file.filename or ""):
        raise HTTPException(status_code=400, detail="template_file must be a .docx file")

    exists = _scoped_duplicate(db, name=name, version=version, visibility=visibility, principal=principal)
    if exists is not None:
        raise HTTPException(status_code=400, detail="rubric name and version already exist")

    rules_bytes = rules_file.file.read() if rules_file else None
    template_bytes = template_file.file.read() if template_file else None
    try:
        # 只解析一次：响应里的告警、模板摘要与覆盖率都来自实际落库的编译结果。
        db.commit()
        prepared = rubric_pipeline.prepare_file_import(
            command=_file_import_command(
                name=name,
                version=version,
                description=description,
                rules_file_name=(rules_file.filename or "rules.xlsx") if rules_file else None,
                template_file_name=(template_file.filename if template_file else None),
            ),
            rules_bytes=rules_bytes,
            template_bytes=template_bytes,
            scorer=None,
            structure_override=override,
        )
        identity = rubric_pipeline.persist_prepared_import(
            session=db,
            prepared=prepared,
            actor_id=user_id,
            organization_id=(None if visibility == "system" else principal.organization_id),
            visibility=visibility,
        )
    except StructureOverrideError as exc:
        raise HTTPException(
            status_code=422,
            detail=_rubric_problem(code=exc.code, message=exc.message, user_action="请核对确认的表格结构后重试。"),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    compilation = prepared.to_mapping()["compilation"]
    raw = compilation["raw_parse_output"]
    coverage = raw.get("coverage") or {}
    return {
        "rubric": _load_rubric(db, identity.rubric_id),
        "warnings": [
            item if isinstance(item, str) else str(item.get("message") or item.get("code"))
            for item in compilation["warnings"]
        ],
        "template_summary": raw.get("template_summary") or {},
        "coverage": coverage,
        "triggers": list(raw.get("triggers") or []),
        "unclaimed_summary": parse_state.unclaimed_summary(coverage),
        "conflicts": list(raw.get("source_conflicts") or []),
    }


@router.post("/import-files/structure-suggestions")
def preview_import_structure(
    rules_file: Optional[UploadFile] = File(None),
    template_file: Optional[UploadFile] = File(None),
    ai_connection_id: Optional[str] = Form(None),
    dry_run: bool = Form(False),
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """导入前结构预检：识别失败（E1/E7）时用 LLM 建议表格结构，不落库；
    ``dry_run`` 只返回将发送的规模估算，供用户确认后再调用模型。"""

    ensure_dev_user(db)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        scorer = None if dry_run else _rubric_ai_scorer(db, principal, ai_connection_id)
        result = structure_state.preview_structure(
            rules_bytes=rules_file.file.read() if rules_file else None,
            template_bytes=template_file.file.read() if template_file else None,
            scorer=scorer,
            dry_run=dry_run,
        )
    except parse_state.ParseStateError as exc:
        raise _parse_state_problem(exc) from exc
    return jsonable_encoder(result)


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


@router.get("/{rubric_id}/review-workspace")
def get_rubric_review_workspace(
    rubric_id: str,
    response: Response,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _visible_rubric(db, rubric_id, principal)
    response.headers["Cache-Control"] = "private, no-store"
    return read_review_workspace(db, rubric_id)


@router.post("/{rubric_id}/rules/{rule_code}/confirm")
def confirm_rubric_rule(
    rubric_id: str,
    rule_code: str,
    payload: AtomicRuleConfirmRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    ensure_dev_user(db)
    _visible_rubric(db, rubric_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        rule = confirm_rule(db, rubric_id, rule_code, user_id, payload)
        db.commit()
    except rubric_lifecycle.RubricLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _rule_response(rule)


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


@router.get("/{rubric_id}/parse-coverage")
def get_parse_coverage(
    rubric_id: str,
    response: Response,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """解析台账覆盖率：哪些原文内容未被识别（解析重构方案 §4.3）。"""

    _visible_rubric(db, rubric_id, principal)
    response.headers["Cache-Control"] = "private, no-store"
    return jsonable_encoder(parse_state.read_parse_coverage(db, rubric_id))


def _parse_state_problem(exc) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail=_rubric_problem(code=exc.code, message=exc.message, user_action="请刷新后重试。"),
    )


def _rubric_ai_scorer(db, principal, ai_connection_id: str | None):
    """与 AI 起草共用的连接选择：显式连接走用户/组织的私有连接，否则用平台默认模型。"""

    if ai_connection_id:
        if not principal.organization_id:
            raise parse_state.ParseStateError(503, "AI_CONNECTION_MISSING", "当前上下文不能使用私有 AI 连接。")
        runtime = resolve_connection_runtime(
            db,
            connection_id=ai_connection_id,
            owner_id=principal.user_id,
            organization_id=principal.organization_id,
        )
        return get_llm_scorer(runtime)
    return get_llm_scorer(session=db)


@router.post("/{rubric_id}/unit-classifications")
def classify_source_units(
    rubric_id: str,
    payload: UnitClassificationRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """用户确认后运行兜底分类器；结果只是建议，持久化到当前编译记录并带指纹。"""

    ensure_dev_user(db)
    _visible_rubric(db, rubric_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        scorer = _rubric_ai_scorer(db, principal, payload.ai_connection_id)
        result = parse_state.run_unit_classification(
            db, rubric_id, scorer, unit_ids=payload.unit_ids, actor_id=user_id
        )
        db.commit()
    except parse_state.ParseStateError as exc:
        db.rollback()
        raise _parse_state_problem(exc) from exc
    return jsonable_encoder(result)


@router.post("/{rubric_id}/rule-review")
def run_rubric_rule_review(
    rubric_id: str,
    payload: RuleReviewRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """第二部分结束后的规则审查：代码前置检查 + LLM 审查（只报告，不修改规则）。"""

    ensure_dev_user(db)
    _visible_rubric(db, rubric_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        scorer = None if payload.dry_run else _rubric_ai_scorer(db, principal, payload.ai_connection_id)
        result = review_state.run_rule_review(
            db, rubric_id, scorer, scope=payload.scope, dry_run=payload.dry_run, actor_id=user_id
        )
        db.commit()
    except parse_state.ParseStateError as exc:
        db.rollback()
        raise _parse_state_problem(exc) from exc
    return jsonable_encoder(result)


@router.get("/{rubric_id}/rule-review")
def get_rubric_rule_review(
    rubric_id: str,
    response: Response,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    _visible_rubric(db, rubric_id, principal)
    response.headers["Cache-Control"] = "private, no-store"
    return jsonable_encoder(review_state.read_rule_review(db, rubric_id))


@router.post("/{rubric_id}/rule-review/findings/{finding_id}/dismiss")
def dismiss_rubric_rule_review_finding(
    rubric_id: str,
    finding_id: str,
    payload: FindingDismissRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """豁免一条审查问题（写明原因并留痕）。"""

    ensure_dev_user(db)
    _visible_rubric(db, rubric_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        finding = review_state.dismiss_finding(db, rubric_id, finding_id, reason=payload.reason, actor_id=user_id)
        db.commit()
    except parse_state.ParseStateError as exc:
        db.rollback()
        raise _parse_state_problem(exc) from exc
    return jsonable_encoder(finding)


@router.post("/{rubric_id}/structure-suggestions")
def suggest_rubric_structure(
    rubric_id: str,
    payload: StructureSuggestionRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """用户确认后运行 LLM 结构识别，返回与当前草稿的差异；建议带指纹持久化。"""

    ensure_dev_user(db)
    _visible_rubric(db, rubric_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        scorer = None if payload.dry_run else _rubric_ai_scorer(db, principal, payload.ai_connection_id)
        result = structure_state.suggest_structure(db, rubric_id, scorer, dry_run=payload.dry_run, actor_id=user_id)
        db.commit()
    except parse_state.ParseStateError as exc:
        db.rollback()
        raise _parse_state_problem(exc) from exc
    return jsonable_encoder(result)


def _persist_structure(db, rubric_id, prepared, *, user_id, reason, action, status, details):
    try:
        db.commit()
        rubric_pipeline.persist_prepared_import(
            session=db, prepared=prepared, actor_id=user_id, target_rubric_id=rubric_id, reason=reason
        )
        structure_state.record_structure_event(
            db, rubric_id, action=action, status=status, actor_id=user_id, reason=reason, details=details
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail=_rubric_problem(code="STRUCTURE_REPARSE_BLOCKED", message="按确认的结构生成新草稿失败。",
                                   user_action="请核对结构后重试。", context={"reason": str(exc)}),
        ) from exc
    return {
        "rubric": RubricRead.model_validate(_load_rubric(db, rubric_id)).model_dump(mode="json"),
        "parse_coverage": jsonable_encoder(parse_state.read_parse_coverage(db, rubric_id)),
    }


@router.post("/{rubric_id}/suggestions/merge")
def merge_structure_suggestion(
    rubric_id: str,
    payload: StructureMergeRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """按确认后的结构重新解析并取代当前草稿：新增/补全直接合入，修改须在 confirm 中逐条列出。"""

    ensure_dev_user(db)
    _visible_rubric(db, rubric_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        prepared, details = structure_state.prepare_merge(
            db, rubric_id, fingerprint=payload.fingerprint, confirm=payload.confirm, exclude=payload.exclude
        )
    except parse_state.ParseStateError as exc:
        db.rollback()
        raise _parse_state_problem(exc) from exc
    return _persist_structure(
        db, rubric_id, prepared, user_id=user_id, reason=payload.reason, action="structure_merge", status="merged",
        details={**details, "confirm": payload.confirm, "exclude": payload.exclude},
    )


@router.post("/{rubric_id}/suggestions/undo")
def undo_structure_suggestion(
    rubric_id: str,
    payload: StructureUndoRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """撤销最近一次结构合入（发布前）：按合入前的结构重新解析；合入新增过评分项时无法撤销。"""

    ensure_dev_user(db)
    _visible_rubric(db, rubric_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        prepared, stored = structure_state.prepare_undo(db, rubric_id)
    except parse_state.ParseStateError as exc:
        db.rollback()
        raise _parse_state_problem(exc) from exc
    return _persist_structure(
        db, rubric_id, prepared, user_id=user_id, reason=payload.reason, action="structure_undo", status="undone",
        details={"fingerprint": stored.get("fingerprint")},
    )


@router.post("/{rubric_id}/units/resolve-batch")
def resolve_source_units_batch(
    rubric_id: str,
    payload: SourceUnitBatchResolveRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """批量处理未认领的原文单元（批量忽略 / 标为上下文 / 指派到同一评分项）。"""

    ensure_dev_user(db)
    _visible_rubric(db, rubric_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        result = parse_state.resolve_units(
            db,
            rubric_id,
            payload.unit_ids,
            action=payload.action,
            reason=payload.reason,
            actor_id=user_id,
            criterion_code=payload.criterion_code,
        )
        db.commit()
    except parse_state.ParseStateError as exc:
        db.rollback()
        raise HTTPException(
            status_code=exc.status,
            detail=_rubric_problem(code=exc.code, message=exc.message, user_action="请刷新后重试。"),
        ) from exc
    return jsonable_encoder(result)


@router.post("/{rubric_id}/units/{unit_id:path}/resolve")
def resolve_source_unit(
    rubric_id: str,
    unit_id: str,
    payload: SourceUnitResolveRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user_id),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """人工处理未认领的原文单元：指派到已有评分项，或确认不是规则。"""

    ensure_dev_user(db)
    _visible_rubric(db, rubric_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    try:
        result = parse_state.resolve_unit(
            db,
            rubric_id,
            unit_id,
            action=payload.action,
            reason=payload.reason,
            actor_id=user_id,
            criterion_code=payload.criterion_code,
        )
        db.commit()
    except parse_state.ParseStateError as exc:
        db.rollback()
        raise HTTPException(
            status_code=exc.status,
            detail=_rubric_problem(code=exc.code, message=exc.message, user_action="请刷新后重试。"),
        ) from exc
    return jsonable_encoder(result)


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
        if payload.atomic_rules is not None:
            from backend.app.services.rubrics.atomic_recompile import prepare_atomic_recompile
            prepared = prepare_atomic_recompile(db, predecessor_version, command, payload.atomic_rules)
        else:
            if db.scalar(select(AtomicRule.id).where(
                AtomicRule.rubric_version_id == predecessor_version.id,
                AtomicRule.creation_method != "manual",
            )):
                from backend.app.services.rubrics.atomic_recompile import prepare_atomic_ai_append
                prepared = prepare_atomic_ai_append(db, predecessor_version, command)
            else:
                prepared = rubric_pipeline.prepare_manual_json_recompile(command=command)
        db.commit()
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
        # 规则审查可跳过但须留痕：发布后编译记录冻结，只能在发布前同一事务内写入。
        review_state.record_review_at_publish(db, rubric_id, compilation_id, actor_id=user_id)
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
    policy = deepcopy(predecessor_version.global_policy or {})
    next_total = payload.total_score or float(rubric.total_score)
    if policy and next_total != float(rubric.total_score):
        try:
            policy["aggregation"]["total_score"] = str(next_total)
            policy.pop("policy_hash", None)
            policy = compile_scoring_policy(policy, total_score=next_total).to_mapping()
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="总分修改与现有全局评分政策不兼容，请核对政策配置。") from exc
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
            "global_policy": policy,
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
    rules_file_name: str | None,
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
