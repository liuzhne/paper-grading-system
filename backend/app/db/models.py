import uuid
from datetime import datetime
from datetime import timezone

from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import JSON
from sqlalchemy import Numeric
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship


def new_id():
    return str(uuid.uuid4())


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="developer")
    department: Mapped[str] = mapped_column(String(100), nullable=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class Rubric(Base):
    __tablename__ = "rubrics"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_rubrics_name_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    total_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False, default=100)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="draft")
    description: Mapped[str] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    criteria: Mapped[list["RubricCriterion"]] = relationship(
        back_populates="rubric",
        cascade="all, delete-orphan",
        order_by="RubricCriterion.display_order",
    )


class RubricCriterion(Base):
    __tablename__ = "rubric_criteria"
    __table_args__ = (UniqueConstraint("rubric_id", "code", name="uq_rubric_criteria_rubric_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rubric_id: Mapped[str] = mapped_column(String(36), ForeignKey("rubrics.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    max_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    weight: Mapped[float] = mapped_column(Numeric(6, 2), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    evidence_hints: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    deduction_rules: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 设计§2 原子项语义。criterion_type: deterministic|llm_judgment|hybrid。
    # scoring_mode: deductive|banded|llm_direct（llm_direct 为过渡态=模型直接给分，目标是迁移到 deductive/banded）。
    criterion_type: Mapped[str] = mapped_column(String(20), nullable=False, default="llm_judgment")
    scoring_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="llm_direct")
    applies_to: Mapped[str] = mapped_column(String(100), nullable=False, default="global")
    rubric_levels: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sub_checks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    rubric: Mapped["Rubric"] = relationship(back_populates="criteria")
    score_items: Mapped[list["ScoreItem"]] = relationship(back_populates="criterion")


class GradingBatch(Base):
    __tablename__ = "grading_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    department: Mapped[str] = mapped_column(String(100), nullable=True)
    major: Mapped[str] = mapped_column(String(100), nullable=True)
    academic_year: Mapped[str] = mapped_column(String(20), nullable=True)
    paper_type: Mapped[str] = mapped_column(String(50), nullable=True)
    rubric_id: Mapped[str] = mapped_column(String(36), ForeignKey("rubrics.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="draft")
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    rubric: Mapped["Rubric"] = relationship()
    papers: Mapped[list["Paper"]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(String(36), ForeignKey("grading_batches.id"), nullable=False)
    student_id: Mapped[str] = mapped_column(String(100), nullable=True)
    student_name: Mapped[str] = mapped_column(String(100), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=True)
    department: Mapped[str] = mapped_column(String(100), nullable=True)
    major: Mapped[str] = mapped_column(String(100), nullable=True)
    advisor: Mapped[str] = mapped_column(String(100), nullable=True)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_text_path: Mapped[str] = mapped_column(Text, nullable=True)
    parse_quality: Mapped[float] = mapped_column(Numeric(5, 3), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="uploaded")
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    batch: Mapped["GradingBatch"] = relationship(back_populates="papers")
    chunks: Mapped[list["PaperChunk"]] = relationship(back_populates="paper", cascade="all, delete-orphan")
    scoring_runs: Mapped[list["ScoringRun"]] = relationship(back_populates="paper", cascade="all, delete-orphan")


class PaperChunk(Base):
    __tablename__ = "paper_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paper_id: Mapped[str] = mapped_column(String(36), ForeignKey("papers.id"), nullable=False)
    section_title: Mapped[str] = mapped_column(Text, nullable=True)
    page_start: Mapped[int] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int] = mapped_column(Integer, nullable=True)
    paragraph_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    paper: Mapped["Paper"] = relationship(back_populates="chunks")


class ScoringRun(Base):
    __tablename__ = "scoring_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paper_id: Mapped[str] = mapped_column(String(36), ForeignKey("papers.id"), nullable=False)
    rubric_id: Mapped[str] = mapped_column(String(36), ForeignKey("rubrics.id"), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(100), nullable=False, default="mock")
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, default="mock-criterion-scorer")
    model_version: Mapped[str] = mapped_column(String(100), nullable=True, default="v1")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="created")
    ai_total_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=True)
    final_total_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=True)
    grade: Mapped[str] = mapped_column(String(50), nullable=True)
    need_manual_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    paper: Mapped["Paper"] = relationship(back_populates="scoring_runs")
    rubric: Mapped["Rubric"] = relationship()
    items: Mapped[list["ScoreItem"]] = relationship(
        back_populates="scoring_run",
        cascade="all, delete-orphan",
        order_by="ScoreItem.created_at",
    )


class ScoreItem(Base):
    __tablename__ = "score_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scoring_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("scoring_runs.id"), nullable=False)
    criterion_id: Mapped[str] = mapped_column(String(36), ForeignKey("rubric_criteria.id"), nullable=False)
    max_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    ai_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    final_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=True)
    evidence_sufficient: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    deductions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 结构化扣分（设计哲学第3条/N6）：每项带 points/reason/rule_ref/evidence_*；deductions 为其展示投影。
    deduction_items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 分档制选档（带证据）与 hybrid 子检查结果（设计§2/§6.3），llm_direct/deductive 时为空。
    band_selection: Mapped[dict] = mapped_column(JSON, nullable=True)
    sub_results: Mapped[list] = mapped_column(JSON, nullable=True)
    suggestion: Mapped[str] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Numeric(5, 3), nullable=True)
    need_manual_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw_model_output: Mapped[dict] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    scoring_run: Mapped["ScoringRun"] = relationship(back_populates="items")
    criterion: Mapped["RubricCriterion"] = relationship(back_populates="score_items")

    @property
    def criterion_name(self):
        return self.criterion.name if self.criterion else None

    @property
    def criterion_code(self):
        return self.criterion.code if self.criterion else None


class ReviewLog(Base):
    __tablename__ = "review_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scoring_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("scoring_runs.id"), nullable=False)
    score_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("score_items.id"), nullable=True)
    reviewer_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    before_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=True)
    after_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class SpreadsheetWriteLog(Base):
    __tablename__ = "spreadsheet_write_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scoring_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("scoring_runs.id"), nullable=False)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    response: Mapped[dict] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
