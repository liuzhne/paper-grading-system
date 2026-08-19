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
    ai_score: Optional[float] = None
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
    aggregation: Optional[dict] = Field(default=None, exclude_if=lambda value: value is None)
    aggregation_schema_version: Optional[str] = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    auto_score_status: Optional[str] = Field(default=None, exclude_if=lambda value: value is None)
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
        items = value if isinstance(value, list) else ([value] if value else [])
        normalized = []
        for item in items:
            # M1 Core evidence has a discriminated, auditable shape.  Do not
            # project it back to the old quote/location/chunk-only DTO.
            if isinstance(item, dict) and item.get("type"):
                normalized.append(item)
            else:
                normalized.extend(coerce_evidence_list([item]))
        return normalized


class ScoringRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    paper_id: Optional[str] = None
    submission_id: Optional[str] = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    rubric_id: str
    owner_id: Optional[str] = None
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
    policy_snapshot: Optional[dict] = Field(default=None, exclude_if=lambda value: value is None)
    policy_hash: Optional[str] = Field(default=None, exclude_if=lambda value: value is None)
    policy_schema_version: Optional[str] = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    business_profile_key: Optional[str] = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    business_profile_version: Optional[str] = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    prompt_version: Optional[str] = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    runtime_identity: Optional[dict] = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
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
    policy_hash: Optional[str] = Field(default=None, exclude_if=lambda value: value is None)
    resolution_type: Optional[str] = Field(default=None, exclude_if=lambda value: value is None)
    created_at: datetime
