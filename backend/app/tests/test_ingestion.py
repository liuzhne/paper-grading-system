"""摄取事务健壮性（A4）：重解析失败保留旧 chunks；拷贝失败清理孤儿 Paper。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.config import settings
from backend.app.db.models import Base
from backend.app.db.models import GradingBatch
from backend.app.db.models import Paper
from backend.app.db.models import PaperChunk
from backend.app.db.models import Rubric
from backend.app.services.papers.ingestion import ingest_file
from backend.app.services.papers.ingestion import parse_and_store


@pytest.fixture
def session(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "STORAGE_ROOT", tmp_path / "storage")
    db = sessionmaker(bind=engine, autoflush=False)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _batch(db):
    rubric = Rubric(name="r", version="v1", total_score=10)
    db.add(rubric)
    db.flush()
    batch = GradingBatch(name="b", rubric_id=rubric.id)
    db.add(batch)
    db.flush()
    return batch


def test_ingest_file_cleans_orphan_on_copy_failure(session):
    batch = _batch(session)
    with pytest.raises(Exception):
        ingest_file(session, batch.id, "/no/such/file.docx", "x.docx")
    assert session.scalars(select(Paper)).all() == []  # 无孤儿 Paper 残留


def test_reparse_failure_preserves_old_chunks(session):
    batch = _batch(session)
    paper = Paper(batch_id=batch.id, file_name="x.docx", file_path="/no/such/file.docx", status="parsed")
    session.add(paper)
    session.flush()
    session.add(PaperChunk(paper_id=paper.id, section_title="旧章", text="旧内容"))
    session.flush()

    parse_and_store(session, paper)  # parse_document 读不到文件 → 失败

    assert paper.status == "failed"
    chunks = session.scalars(select(PaperChunk).where(PaperChunk.paper_id == paper.id)).all()
    assert len(chunks) == 1  # 旧 chunk 未被删（解析成功才删）
