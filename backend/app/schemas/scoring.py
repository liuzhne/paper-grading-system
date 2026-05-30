from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

from backend.app.services.scoring.validator import coerce_evidence_list
from backend.app.services.scoring.validator import coerce_string_list


class ScoreItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    scoring_run_id: str
    criterion_id: str
    criterion_name: Optional[str] = None
    criterion_code: Optional[str] = None
    max_score: float
    ai_score: float
    final_score: Optional[float] = None
    evidence_sufficient: bool
    reason: str
    deductions: list[str]
    deduction_items: list[dict] = Field(default_factory=list)
    evidence: list[dict]
    band_selection: Optional[dict] = None
    sub_results: Optional[list] = None
    suggestion: Optional[str] = None
    confidence: Optional[float] = None
    need_manual_review: bool
    created_at: datetime

    @field_validator("deductions", mode="before")
    @classmethod
    def normalize_deductions(cls, value):
        return coerce_string_list(value)

    @field_validator("deduction_items", mode="before")
    @classmethod
    def normalize_deduction_items(cls, value):
        return value or []

    @field_validator("evidence", mode="before")
    @classmethod
    def normalize_evidence(cls, value):
        return coerce_evidence_list(value)


class ScoringRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    paper_id: str
    rubric_id: str
    model_provider: str
    model_name: str
    model_version: Optional[str] = None
    status: str
    ai_total_score: Optional[float] = None
    final_total_score: Optional[float] = None
    grade: Optional[str] = None
    need_manual_review: bool
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    coherence_findings: list = Field(default_factory=list)
    format_findings: list = Field(default_factory=list)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime

    @field_validator("coherence_findings", mode="before")
    @classmethod
    def normalize_coherence(cls, value):
        return value or []

    @field_validator("format_findings", mode="before")
    @classmethod
    def normalize_format_findings(cls, value):
        return value or []


class ScoreItemUpdate(BaseModel):
    final_score: float = Field(ge=0)
    reason: str = Field(min_length=1)


class ReviewSubmit(BaseModel):
    reason: str = Field(min_length=1)


class ReviewLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    scoring_run_id: str
    score_item_id: Optional[str] = None
    reviewer_id: str
    before_score: Optional[float] = None
    after_score: Optional[float] = None
    reason: str
    created_at: datetime
