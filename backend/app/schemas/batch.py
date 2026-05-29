from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class BatchCreate(BaseModel):
    name: str = Field(min_length=1)
    rubric_id: str
    department: Optional[str] = None
    major: Optional[str] = None
    academic_year: Optional[str] = None
    paper_type: Optional[str] = None
    status: str = "draft"


class BatchUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1)
    rubric_id: Optional[str] = None
    department: Optional[str] = None
    major: Optional[str] = None
    academic_year: Optional[str] = None
    paper_type: Optional[str] = None
    status: Optional[str] = None


class BatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    department: Optional[str] = None
    major: Optional[str] = None
    academic_year: Optional[str] = None
    paper_type: Optional[str] = None
    rubric_id: str
    status: str
    created_at: datetime
    updated_at: datetime


class BatchScoreResult(BaseModel):
    batch_id: str
    total_papers: int
    scored_count: int
    skipped_count: int
    failed_count: int
    run_ids: list[str]
    errors: list[dict]


class BatchPaperSummary(BaseModel):
    paper_id: str
    title: Optional[str] = None
    student_id: Optional[str] = None
    student_name: Optional[str] = None
    file_name: str
    paper_status: str
    parse_quality: Optional[float] = None
    latest_run_id: Optional[str] = None
    latest_run_status: Optional[str] = None
    latest_final_score: Optional[float] = None
    latest_grade: Optional[str] = None
    latest_need_manual_review: Optional[bool] = None


class BatchSummary(BaseModel):
    batch: BatchRead
    rubric_name: str
    rubric_version: str
    paper_stats: dict[str, int]
    run_stats: dict[str, int]
    papers: list[BatchPaperSummary]
