from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.deps import CurrentPrincipal
from backend.app.api.deps import current_principal
from backend.app.api.deps import require_organization_role
from backend.app.db.models import GradingBatch
from backend.app.db.models import Paper
from backend.app.db.models import PaperChunk
from backend.app.db.session import get_db
from backend.app.schemas.paper import PaperChunkRead
from backend.app.schemas.paper import PaperRead
from backend.app.schemas.paper import PaperUpdate
from backend.app.schemas.paper import ParsedPaperResponse
from backend.app.services.papers.ingestion import parse_and_store
from backend.app.services.storage.local import ensure_storage_dirs
from backend.app.services.storage.local import read_json
from backend.app.services.storage.local import safe_filename
from backend.app.services.storage.local import store_binary

router = APIRouter(prefix="/papers", tags=["papers"])


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

    parse_and_store(db, paper)
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
