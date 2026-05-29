from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import UploadFile
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.db.models import GradingBatch
from backend.app.db.models import Paper
from backend.app.db.models import PaperChunk
from backend.app.db.session import get_db
from backend.app.schemas.paper import PaperChunkRead
from backend.app.schemas.paper import PaperRead
from backend.app.schemas.paper import PaperUpdate
from backend.app.schemas.paper import ParsedPaperResponse
from backend.app.services.document_parser.chunking import build_chunks
from backend.app.services.document_parser.parser import parse_document
from backend.app.services.storage.local import ensure_storage_dirs
from backend.app.services.storage.local import read_json
from backend.app.services.storage.local import safe_filename
from backend.app.services.storage.local import save_binary
from backend.app.services.storage.local import write_json

router = APIRouter(prefix="/papers", tags=["papers"])


@router.get("", response_model=list[PaperRead])
def list_papers(batch_id: Optional[str] = None, db: Session = Depends(get_db)):
    query = select(Paper).order_by(Paper.created_at.desc())
    if batch_id:
        query = query.where(Paper.batch_id == batch_id)
    return db.scalars(query).all()


@router.post("/upload", response_model=PaperRead)
def upload_paper(
    batch_id: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    batch = db.get(GradingBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    paper = _save_and_parse_upload(db, batch_id, file, strict_type=True)
    db.commit()
    db.refresh(paper)
    return paper


@router.post("/bulk-upload", response_model=list[PaperRead])
def bulk_upload_papers(
    batch_id: str = Form(...),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    batch = db.get(GradingBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    if not files:
        raise HTTPException(status_code=400, detail="at least one file is required")

    papers = []
    for file in files:
        papers.append(_save_and_parse_upload(db, batch_id, file, strict_type=False))
    db.commit()
    for paper in papers:
        db.refresh(paper)
    return papers


@router.get("/{paper_id}", response_model=PaperRead)
def get_paper(paper_id: str, db: Session = Depends(get_db)):
    paper = db.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")
    return paper


@router.patch("/{paper_id}", response_model=PaperRead)
def update_paper(paper_id: str, payload: PaperUpdate, db: Session = Depends(get_db)):
    paper = db.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        if isinstance(value, str):
            value = value.strip() or None
        setattr(paper, field, value)

    db.commit()
    db.refresh(paper)
    return paper


@router.post("/{paper_id}/parse", response_model=PaperRead)
def reparse_paper(paper_id: str, db: Session = Depends(get_db)):
    paper = db.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")
    if not paper.file_path:
        raise HTTPException(status_code=400, detail="paper has no source file path")

    _parse_existing_paper(db, paper)
    db.commit()
    db.refresh(paper)
    return paper


@router.get("/{paper_id}/parsed", response_model=ParsedPaperResponse)
def get_parsed_paper(paper_id: str, db: Session = Depends(get_db)):
    paper = db.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")
    if not paper.parsed_text_path:
        raise HTTPException(status_code=404, detail="paper has no parsed text")
    return {"paper_id": paper.id, "parsed": read_json(Path(paper.parsed_text_path))}


@router.get("/{paper_id}/chunks", response_model=list[PaperChunkRead])
def list_paper_chunks(paper_id: str, db: Session = Depends(get_db)):
    paper = db.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")
    return db.scalars(
        select(PaperChunk).where(PaperChunk.paper_id == paper_id).order_by(PaperChunk.created_at, PaperChunk.id)
    ).all()


def _save_and_parse_upload(db: Session, batch_id: str, file: UploadFile, strict_type: bool):
    suffix = Path(file.filename or "").suffix.lower()
    filename = safe_filename(file.filename or "paper%s" % suffix)
    if suffix not in [".docx", ".pdf"]:
        if strict_type:
            raise HTTPException(status_code=400, detail="only .docx and text PDF files are supported")
        paper = Paper(
            batch_id=batch_id,
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
        file_name=filename,
        file_path="",
        status="uploaded",
    )
    db.add(paper)
    db.flush()

    destination = settings.uploads_dir / ("%s_%s" % (paper.id, filename))
    save_binary(file.file, destination)
    paper.file_path = str(destination)
    _parse_existing_paper(db, paper)
    return paper


def _parse_existing_paper(db: Session, paper: Paper):
    existing_title = paper.title
    existing_student_id = paper.student_id
    existing_student_name = paper.student_name
    existing_department = paper.department
    existing_major = paper.major
    existing_advisor = paper.advisor
    db.execute(delete(PaperChunk).where(PaperChunk.paper_id == paper.id))
    paper.parsed_text_path = None
    paper.parse_quality = None
    paper.error_message = None
    paper.status = "parsing"
    try:
        parsed = parse_document(paper.file_path)
        parsed_path = settings.parsed_dir / ("%s.json" % paper.id)
        write_json(parsed_path, parsed.to_dict())
        paper.title = existing_title or parsed.title
        paper.student_id = existing_student_id or parsed.student_id
        paper.student_name = existing_student_name or parsed.student_name
        paper.department = existing_department or parsed.department
        paper.major = existing_major or parsed.major
        paper.advisor = existing_advisor or parsed.advisor
        paper.parsed_text_path = str(parsed_path)
        paper.parse_quality = parsed.parse_quality
        paper.status = "parsed"
        for chunk in build_chunks(parsed, paper.id):
            db.add(chunk)
    except Exception as exc:
        paper.status = "failed"
        paper.error_message = str(exc)
    return paper
