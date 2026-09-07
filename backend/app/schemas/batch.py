from datetime import datetime
from typing import Literal
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class BatchCreate(BaseModel):
    name: str = Field(min_length=1)
    rubric_id: str
    rubric_version_id: Optional[str] = None
    ai_connection_id: Optional[str] = None
    department: Optional[str] = None
    major: Optional[str] = None
    academic_year: Optional[str] = None
    paper_type: Optional[str] = None
    # 创建只允许 draft（前端 v2 计划 §5-C）。旧客户端显式传 "draft" 仍可用；
    # 任何其它阶段必须走动作服务，不能由客户端在创建时直接指定。
    status: Literal["draft"] = "draft"


class BatchUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1)
    rubric_id: Optional[str] = None
    rubric_version_id: Optional[str] = None
    ai_connection_id: Optional[str] = None
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
    rubric_version_id: Optional[str] = None
    ai_connection_id: Optional[str] = None
    ai_connection_key_version: Optional[int] = None
    ai_connection_snapshot: Optional[dict] = None
    status: str
    # 乐观并发游标：客户端带回它，过期的写请求会冲突失败而非静默覆盖。
    state_version: int
    owner_id: Optional[str] = None
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
