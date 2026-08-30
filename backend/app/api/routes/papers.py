from datetime import timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import Session

from backend.app.api.deps import CurrentPrincipal
from backend.app.api.deps import current_principal
from backend.app.api.deps import require_organization_role
from backend.app.core.config import settings
from backend.app.db.models import GradingBatch
from backend.app.db.models import Paper
from backend.app.db.models import PaperChunk
from backend.app.db.models import utcnow
from backend.app.db.session import get_db
from backend.app.schemas.paper import CompleteDirectUpload
from backend.app.schemas.paper import DirectUploadIntentCreate
from backend.app.schemas.paper import DirectUploadIntentRead
from backend.app.schemas.paper import PaperChunkRead
from backend.app.schemas.paper import PaperRead
from backend.app.schemas.paper import PaperUpdate
from backend.app.schemas.paper import ParsedPaperResponse
from backend.app.services.papers.ingestion import parse_and_store
from backend.app.services.storage.local import artifact_not_found
from backend.app.services.storage.local import create_signed_upload
from backend.app.services.storage.local import ensure_storage_dirs
from backend.app.services.storage.local import private_object_path
from backend.app.services.storage.local import private_object_ref
from backend.app.services.storage.local import private_object_size
from backend.app.services.storage.local import read_json
from backend.app.services.storage.local import safe_filename
from backend.app.services.storage.local import store_binary
from backend.app.services.storage.local import supabase_tus_endpoint

router = APIRouter(prefix="/papers", tags=["papers"])

_MIB = 1024 * 1024
_PAPER_MEDIA_TYPES = {
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/octet-stream",
        "application/zip",
    },
    ".pdf": {"application/pdf", "application/octet-stream"},
}


def _paper_problem(
    status_code: int,
    code: str,
    message: str,
    user_action: str,
    *,
    retryable: bool,
):
    raise HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "user_action": user_action,
            "retryable": retryable,
        },
    )


def _visible_paper(db: Session, paper_id: str, principal: CurrentPrincipal) -> Paper:
    paper = db.get(Paper, paper_id)
    if paper is None or (
        principal.organization_id is not None and paper.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="paper not found")
    return paper


def _visible_batch(db: Session, batch_id: str, principal: CurrentPrincipal) -> GradingBatch:
    batch = db.get(GradingBatch, batch_id)
    if batch is None or (
        principal.organization_id is not None and batch.organization_id != principal.organization_id
    ):
        raise HTTPException(status_code=404, detail="batch not found")
    return batch


@router.get("", response_model=list[PaperRead])
def list_papers(batch_id: Optional[str] = None, db: Session = Depends(get_db), principal: CurrentPrincipal = Depends(current_principal)):
    query = select(Paper).order_by(Paper.created_at.desc())
    if principal.organization_id is not None:
        query = query.where(Paper.organization_id == principal.organization_id)
    if batch_id:
        query = query.where(Paper.batch_id == batch_id)
    return db.scalars(query).all()


@router.post("/upload", response_model=PaperRead)
def upload_paper(
    batch_id: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    batch = _visible_batch(db, batch_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    paper = _save_and_parse_upload(db, batch_id, file, strict_type=True)
    paper.organization_id = batch.organization_id
    db.commit()
    db.refresh(paper)
    return paper


@router.post("/bulk-upload", response_model=list[PaperRead])
def bulk_upload_papers(
    batch_id: str = Form(...),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    batch = _visible_batch(db, batch_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    if not files:
        raise HTTPException(status_code=400, detail="at least one file is required")

    papers = []
    for file in files:
        paper = _save_and_parse_upload(db, batch_id, file, strict_type=False)
        paper.organization_id = batch.organization_id
        papers.append(paper)
    db.commit()
    for paper in papers:
        db.refresh(paper)
    return papers


@router.post(
    "/direct-upload-intents",
    response_model=DirectUploadIntentRead,
    status_code=201,
)
def create_direct_upload_intent(
    payload: DirectUploadIntentCreate,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """Reserve one private object path and return a short-lived write token."""

    batch = _visible_batch(db, payload.batch_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    if (
        settings.STORAGE_PROVIDER != "supabase"
        or not settings.SUPABASE_URL
        or not settings.SUPABASE_SECRET_KEY
        or not settings.SUPABASE_STORAGE_BUCKET
    ):
        _paper_problem(
            503,
            "DIRECT_STORAGE_NOT_CONFIGURED",
            "私有文件存储尚未配置，当前无法安全直传材料。",
            "请联系管理员配置 Supabase 私有桶后重试。",
            retryable=False,
        )

    suffix = Path(payload.file_name).suffix.lower()
    content_type = (payload.content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    if suffix not in _PAPER_MEDIA_TYPES or content_type not in _PAPER_MEDIA_TYPES[suffix]:
        _paper_problem(
            400,
            "UNSUPPORTED_PAPER_TYPE",
            "该文件不是受支持的 Word 或 PDF 材料。",
            "请选择 .docx 或文本型 .pdf 文件。",
            retryable=False,
        )
    max_bytes = settings.DIRECT_UPLOAD_MAX_SIZE_MB * _MIB
    if payload.byte_size > max_bytes:
        _paper_problem(
            413,
            "PAPER_FILE_TOO_LARGE",
            f"文件超过 {settings.DIRECT_UPLOAD_MAX_SIZE_MB} MiB 的上传上限。",
            "请压缩文件或拆分材料后重新选择。",
            retryable=False,
        )

    filename = safe_filename(payload.file_name)
    paper = Paper(
        batch_id=batch.id,
        owner_id=batch.owner_id,
        organization_id=batch.organization_id,
        file_name=filename,
        file_path="",
        status="uploading",
    )
    db.add(paper)
    db.flush()
    organization_segment = batch.organization_id or "legacy"
    # Supabase Storage rejects non-ASCII object keys with ``InvalidKey``.
    # Keep the original, user-facing filename on Paper.file_name, while the
    # private object path uses only server-generated ASCII identity.
    object_name = f"{payload.byte_size}-source{suffix}"
    object_path = (
        f"uploads/{organization_segment}/{batch.id}/{paper.id}/{object_name}"
    )
    paper.file_path = private_object_ref(object_path)
    try:
        signed = create_signed_upload(object_path)
        tus_endpoint = supabase_tus_endpoint()
    except Exception:
        db.rollback()
        _paper_problem(
            502,
            "DIRECT_UPLOAD_SIGNING_FAILED",
            "暂时无法创建安全上传凭证。",
            "请稍后重试；已选择的本地文件不会丢失。",
            retryable=True,
        )

    db.commit()
    db.refresh(paper)
    threshold_bytes = settings.DIRECT_UPLOAD_TUS_THRESHOLD_MB * _MIB
    return {
        "paper": paper,
        "mode": "standard" if payload.byte_size <= threshold_bytes else "tus",
        "signed_url": signed["signed_url"],
        "token": signed["token"],
        "tus_endpoint": tus_endpoint,
        "bucket_name": settings.SUPABASE_STORAGE_BUCKET,
        "object_path": object_path,
        "threshold_bytes": threshold_bytes,
    }


@router.post("/{paper_id}/complete-upload", response_model=PaperRead)
def complete_direct_upload(
    paper_id: str,
    payload: CompleteDirectUpload,
    db: Session = Depends(get_db),
    principal: CurrentPrincipal = Depends(current_principal),
):
    """Verify the archived object before making it eligible for parsing."""

    paper = _visible_paper(db, paper_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    if paper.status in {"uploaded", "parsing", "parsed", "failed"}:
        return paper
    if paper.status != "uploading" or not paper.file_path.startswith("supabase://"):
        _paper_problem(
            409,
            "PAPER_UPLOAD_STATE_INVALID",
            "该材料当前不在等待归档确认的状态。",
            "请刷新材料列表后按当前状态继续。",
            retryable=False,
        )

    object_path = private_object_path(paper.file_path)
    encoded_size, separator, _ = Path(object_path).name.partition("-")
    try:
        intended_size = int(encoded_size) if separator else -1
    except ValueError:
        intended_size = -1
    if payload.byte_size != intended_size:
        _paper_problem(
            409,
            "UPLOAD_DECLARATION_MISMATCH",
            "本次确认的文件大小与上传凭证不一致。",
            "请不要更换文件；重新选择材料并发起上传。",
            retryable=False,
        )
    try:
        archived_size = private_object_size(paper.file_path)
    except Exception as exc:
        if artifact_not_found(exc):
            _paper_problem(
                409,
                "ARCHIVED_OBJECT_NOT_FOUND",
                "私有存储中尚未找到完整文件。",
                "请等待上传完成，或重新上传当前文件。",
                retryable=True,
            )
        _paper_problem(
            502,
            "ARCHIVED_OBJECT_CHECK_FAILED",
            "暂时无法核对私有存储中的文件。",
            "请稍后重试归档确认。",
            retryable=True,
        )
    if archived_size != intended_size:
        _paper_problem(
            409,
            "ARCHIVED_OBJECT_SIZE_MISMATCH",
            "私有存储中的文件不完整。",
            "请重新上传当前文件，系统不会解析不完整材料。",
            retryable=True,
        )

    paper.status = "uploaded"
    paper.error_message = None
    db.commit()
    db.refresh(paper)
    return paper


@router.get("/{paper_id}", response_model=PaperRead)
def get_paper(paper_id: str, db: Session = Depends(get_db), principal: CurrentPrincipal = Depends(current_principal)):
    return _visible_paper(db, paper_id, principal)


@router.patch("/{paper_id}", response_model=PaperRead)
def update_paper(paper_id: str, payload: PaperUpdate, db: Session = Depends(get_db), principal: CurrentPrincipal = Depends(current_principal)):
    paper = _visible_paper(db, paper_id, principal)
    require_organization_role(principal, "org_admin", "teacher")

    for field, value in payload.model_dump(exclude_unset=True).items():
        if isinstance(value, str):
            value = value.strip() or None
        setattr(paper, field, value)

    db.commit()
    db.refresh(paper)
    return paper


@router.post("/{paper_id}/parse", response_model=PaperRead)
def reparse_paper(paper_id: str, db: Session = Depends(get_db), principal: CurrentPrincipal = Depends(current_principal)):
    paper = _visible_paper(db, paper_id, principal)
    require_organization_role(principal, "org_admin", "teacher")
    if not paper.file_path:
        raise HTTPException(status_code=400, detail="paper has no source file path")
    if paper.status == "parsed":
        return paper

    stale_before = utcnow() - timedelta(seconds=settings.PAPER_PARSE_LEASE_SECONDS)
    claim = db.execute(
        update(Paper)
        .where(
            Paper.id == paper.id,
            (
                Paper.status.in_(("uploaded", "failed"))
                | ((Paper.status == "parsing") & (Paper.updated_at < stale_before))
            ),
        )
        .values(status="parsing", error_message=None, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    if claim.rowcount != 1:
        db.rollback()
        paper = _visible_paper(db, paper_id, principal)
        if paper.status == "parsing":
            _paper_problem(
                409,
                "PAPER_PARSE_IN_PROGRESS",
                "该材料正在解析中。",
                "请稍候刷新；超过恢复时限后可重新解析。",
                retryable=True,
            )
        if paper.status == "uploading":
            _paper_problem(
                409,
                "PAPER_UPLOAD_NOT_CONFIRMED",
                "文件尚未完成私有存储归档确认。",
                "请先完成上传和归档确认，再开始解析。",
                retryable=True,
            )
        _paper_problem(
            409,
            "PAPER_PARSE_STATE_INVALID",
            "该材料当前状态不能开始解析。",
            "请刷新材料列表并按当前状态处理。",
            retryable=False,
        )

    # 先提交领取状态：即使后续函数超时/进程中断，另一请求也能在租约过期后恢复。
    db.commit()
    paper = _visible_paper(db, paper_id, principal)
    try:
        parse_and_store(db, paper)
        db.commit()
    except Exception:
        db.rollback()
        paper = _visible_paper(db, paper_id, principal)
        paper.status = "failed"
        paper.error_message = "解析服务意外中断，请重新解析。"
        db.commit()
    db.refresh(paper)
    return paper


@router.get("/{paper_id}/parsed", response_model=ParsedPaperResponse)
def get_parsed_paper(paper_id: str, db: Session = Depends(get_db), principal: CurrentPrincipal = Depends(current_principal)):
    paper = _visible_paper(db, paper_id, principal)
    if not paper.parsed_text_path:
        raise HTTPException(status_code=404, detail="paper has no parsed text")
    try:
        parsed = read_json(paper.parsed_text_path)
    except (OSError, ValueError):  # 文件缺失/损坏(JSONDecodeError 属 ValueError)→ 明确 404 而非 500
        raise HTTPException(status_code=404, detail="parsed text file not found or unreadable")
    return {"paper_id": paper.id, "parsed": parsed}


@router.get("/{paper_id}/chunks", response_model=list[PaperChunkRead])
def list_paper_chunks(paper_id: str, db: Session = Depends(get_db), principal: CurrentPrincipal = Depends(current_principal)):
    paper = _visible_paper(db, paper_id, principal)
    return db.scalars(
        select(PaperChunk).where(PaperChunk.paper_id == paper_id).order_by(PaperChunk.created_at, PaperChunk.id)
    ).all()


def _save_and_parse_upload(db: Session, batch_id: str, file: UploadFile, strict_type: bool):
    suffix = Path(file.filename or "").suffix.lower()
    filename = safe_filename(file.filename or "paper%s" % suffix)
    owner_id = db.scalar(select(GradingBatch.owner_id).where(GradingBatch.id == batch_id))  # 继承批次归属
    if suffix not in [".docx", ".pdf"]:
        if strict_type:
            raise HTTPException(status_code=400, detail="only .docx and text PDF files are supported")
        paper = Paper(
            batch_id=batch_id,
            owner_id=owner_id,
            file_name=filename,
            file_path="",
            status="failed",
            error_message="unsupported file type; only .docx and text PDF files are supported",
        )
        db.add(paper)
        db.flush()
        return paper

    ensure_storage_dirs()
    paper = Paper(
        batch_id=batch_id,
        owner_id=owner_id,
        file_name=filename,
        file_path="",
        status="uploaded",
    )
    db.add(paper)
    db.flush()

    paper.file_path = store_binary(
        file.file,
        "uploads",
        "%s_%s" % (paper.id, filename),
        content_type=file.content_type,
    )
    parse_and_store(db, paper)
    return paper
