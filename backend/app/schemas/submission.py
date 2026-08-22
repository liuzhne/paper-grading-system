from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from pydantic import Field


class EvaluationBatchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    rubric_version_id: str = Field(min_length=1)
    business_profile_key: str = Field(min_length=1, max_length=100)
    business_profile_version: str = Field(min_length=1, max_length=100)
    ai_connection_id: str | None = None


class EvaluationBatchRead(BaseModel):
    id: str
    name: str
    rubric_id: str
    rubric_version_id: str
    business_profile_key: str
    business_profile_version: str
    ai_connection_id: str | None = None
    ai_connection_key_version: int | None = None
    ai_connection_snapshot: dict | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class SubmissionRead(BaseModel):
    id: str
    evaluation_batch_id: str
    business_profile_key: str
    business_profile_version: str
    source_artifact_hash: str
    source_artifact_ref: str
    file_name: str
    media_type: str
    byte_length: int
    metadata: dict
    status: str
    error_message: str | None = None
    document_snapshot_id: str | None = None
    created_at: datetime
    updated_at: datetime


class DocumentSnapshotSummary(BaseModel):
    id: str
    submission_id: str
    schema_version: str
    business_profile_key: str
    business_profile_version: str
    parser_version: str
    normalizer_version: str
    content_hash: str
    snapshot_hash: str
    snapshot_ref: str
    section_count: int
    evidence_unit_count: int
    parse_quality: str | float | int | None = None
    created_at: datetime


class ScoreSubmissionRequest(BaseModel):
    rescore_generation: int = Field(default=0, ge=0)
    document_snapshot_id: str | None = None


class V2ScoreItemRead(BaseModel):
    id: str
    criterion_id: str
    criterion_code: str
    criterion_name: str
    max_score: float
    ai_score: float | None = None
    final_score: float | None = None
    evidence_sufficient: bool
    evidence: list
    reason: str
    need_manual_review: bool
    auto_score_status: str | None = None
    aggregation: dict | None = None
    rule_results: list = Field(default_factory=list)
    rule_results_schema_version: str | None = None


class V2ScoringRunRead(BaseModel):
    schema_version: Literal["run-read@2"] = "run-read@2"
    id: str
    paper_id: str | None = None
    submission_id: str
    document_snapshot_id: str
    rubric_id: str
    rubric_version_id: str
    rubric_version_hash: str
    rubric_hash_scheme: str
    rubric_snapshot_hash: str
    business_profile_key: str
    business_profile_version: str
    workflow_profile: str
    policy_hash: str
    policy_schema_version: str
    execution_plan_hash: str
    plan_schema_version: str
    source_artifact_hash: str
    normalized_content_hash: str
    document_snapshot_hash: str
    prompt_version: str
    runtime_identity: dict
    engine_version: str
    model_provider: str
    model_name: str
    model_version: str | None = None
    ai_connection_id: str | None = None
    ai_connection_key_version: int | None = None
    ai_connection_snapshot: dict | None = None
    rescore_generation: int
    idempotency_key: str
    status: str
    ai_total_score: float | None = None
    final_total_score: float | None = None
    grade: str | None = None
    need_manual_review: bool
    items: list[V2ScoreItemRead]
    created_at: datetime


class GenericScoreItemReview(BaseModel):
    final_score: float = Field(ge=0)
    reason: str = Field(min_length=1)
    resolution_type: Literal[
        "ordinary_override",
        "resolve_validation",
        "resolve_block",
    ]


class GenericReviewSubmit(BaseModel):
    reason: str = Field(min_length=1)


class V2ReviewLogRead(BaseModel):
    id: str
    scoring_run_id: str
    score_item_id: str | None = None
    reviewer_id: str
    before_score: float | None = None
    after_score: float | None = None
    reason: str
    policy_hash: str
    resolution_type: str
    created_at: datetime


__all__ = [
    "DocumentSnapshotSummary",
    "EvaluationBatchCreate",
    "EvaluationBatchRead",
    "GenericReviewSubmit",
    "GenericScoreItemReview",
    "ScoreSubmissionRequest",
    "SubmissionRead",
    "V2ReviewLogRead",
    "V2ScoreItemRead",
    "V2ScoringRunRead",
]
