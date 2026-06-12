"""论文摄取（解析+落库）共享服务。

从 api/routes/papers.py 抽出，供上传路由与离线脚本（如 QWK 评估批量导入）复用同一套解析逻辑，
避免行为漂移。`parse_and_store` 对已落库 paper（含 file_path）解析+分块+持久化；
`ingest_file` 按**文件系统路径**新建 paper 并解析。
"""

from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.db.models import Paper
from backend.app.db.models import PaperChunk
from backend.app.services.document_parser.chunking import build_chunks
from backend.app.services.document_parser.parser import parse_document
from backend.app.services.storage.local import ensure_storage_dirs
from backend.app.services.storage.local import save_binary
from backend.app.services.storage.local import safe_filename
from backend.app.services.storage.local import write_json


def parse_and_store(db: Session, paper: Paper):
    """对已有 file_path 的 paper 解析→写 parsed JSON→建 chunks→更新元数据；失败置 status=failed。"""
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
    # chunk 必须对同会话的后续 SELECT 立即可见：调用方会话可能 autoflush=False（如 CLI），
    # 不 flush 则"导入后立即评分"的检索读不到 chunk，证据为空导致全 0 分。
    db.flush()
    return paper


def ingest_file(db: Session, batch_id: str, file_path, file_name: str = None):
    """按文件系统路径新建 paper：拷入 uploads 目录并解析（供离线批量导入脚本用）。"""
    source = Path(file_path)
    name = safe_filename(file_name or source.name)
    ensure_storage_dirs()
    paper = Paper(batch_id=batch_id, file_name=name, file_path="", status="uploaded")
    db.add(paper)
    db.flush()

    destination = settings.uploads_dir / ("%s_%s" % (paper.id, name))
    with open(source, "rb") as handle:
        save_binary(handle, destination)
    paper.file_path = str(destination)
    parse_and_store(db, paper)
    return paper
