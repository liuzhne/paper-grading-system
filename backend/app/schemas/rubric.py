from datetime import datetime
from typing import Any
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from backend.app.services.scoring.core.policy import validate_weight_configuration


class RubricCriterionCreate(BaseModel):
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    max_score: float = Field(gt=0)
    weight: Optional[float] = None
    description: Optional[str] = None
    evidence_hints: list[str] = Field(default_factory=list)
    deduction_rules: list[str] = Field(default_factory=list)
    display_order: int = 0
    criterion_type: str = "llm_judgment"
    scoring_mode: str = "llm_direct"
    applies_to: str = "global"
    rubric_levels: list[dict] = Field(default_factory=list)
    sub_checks: list[dict] = Field(default_factory=list)
    dimension: Optional[str] = None
    deduction_rules_structured: list[dict] = Field(default_factory=list)


class RubricCreate(BaseModel):
    name: str = Field(min_length=1)
    version: str = Field(default="v1.0", min_length=1)
    total_score: float = Field(default=100, gt=0)
    description: Optional[str] = None
    criteria: list[RubricCriterionCreate]

    @model_validator(mode="after")
    def validate_score_sum(self):
        validate_weight_configuration(self.criteria, total_score=self.total_score)
        return self


class RubricCloneRequest(BaseModel):
    new_version: str = Field(min_length=1)
    name: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = None


class RubricLifecycleReason(BaseModel):
    reason: str = Field(default="人工生命周期操作", min_length=1)


class RubricPublishRequest(RubricLifecycleReason):
    compilation_id: Optional[str] = None


class AtomicRuleEditRequest(RubricLifecycleReason):
    changes: dict


class RubricDraftRecompileRequest(RubricLifecycleReason):
    supersedes_compilation_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    criteria: list[RubricCriterionCreate] = Field(min_length=1)
    global_policy: dict = Field(default_factory=dict)
    workflow_profile: str = Field(default="manual_json", min_length=1)
    business_profile_key: str = Field(default="thesis", min_length=1)


class ExecutionDraftVersionRead(BaseModel):
    id: str
    version: str
    workflow_profile: str
    business_profile_key: str


class ExecutionDraftRuleRead(BaseModel):
    id: str
    rule_code: str
    name: str
    direction: str
    effect_type: str
    judge_type: str
    status: str
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None


class ExecutionDraftTemplateLinkRead(BaseModel):
    id: str
    rule_code: str
    review_status: str
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None


class ExecutionDraftCompilationRead(BaseModel):
    id: str
    status: str
    blockers: list[Any] = Field(default_factory=list)
    warnings: list[Any] = Field(default_factory=list)
    version: Optional[ExecutionDraftVersionRead] = None
    rules: list[ExecutionDraftRuleRead] = Field(default_factory=list)
    template_links: list[ExecutionDraftTemplateLinkRead] = Field(default_factory=list)


class ExecutionDraftCompilationSummaryRead(BaseModel):
    id: str
    status: str
    is_active: bool
    blocker_count: int
    created_at: datetime
    published_at: Optional[datetime] = None


class RubricExecutionDraftRead(BaseModel):
    rubric_id: str
    rubric_status: str
    active_compilation: Optional[ExecutionDraftCompilationRead] = None
    compilations: list[ExecutionDraftCompilationSummaryRead] = Field(
        default_factory=list
    )
    ambiguity: Optional[str] = None


class TemplateLinkReviewRequest(RubricLifecycleReason):
    decision: str


class RubricUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1)
    version: Optional[str] = Field(default=None, min_length=1)
    total_score: Optional[float] = Field(default=None, gt=0)
    description: Optional[str] = None
    criteria: Optional[list[RubricCriterionCreate]] = None

    @model_validator(mode="after")
    def validate_criteria_when_present(self):
        if self.criteria is not None and not self.criteria:
            raise ValueError("rubric must contain at least one criterion")
        if self.criteria is not None and self.total_score is not None:
            validate_weight_configuration(self.criteria, total_score=self.total_score)
        return self


class RubricCriterionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    rubric_id: str
    code: str
    name: str
    max_score: float
    weight: Optional[float] = None
    description: Optional[str] = None
    evidence_hints: list[str]
    deduction_rules: list[str]
    display_order: int
    criterion_type: str = "llm_judgment"
    scoring_mode: str = "llm_direct"
    applies_to: str = "global"
    rubric_levels: list = Field(default_factory=list)
    sub_checks: list = Field(default_factory=list)
    dimension: Optional[str] = None
    deduction_rules_structured: list = Field(default_factory=list)


class RubricRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    version: str
    total_score: float
    status: str
    description: Optional[str] = None
    format_spec: dict = Field(default_factory=dict)
    created_at: datetime
    published_at: Optional[datetime] = None
    criteria: list[RubricCriterionRead] = Field(default_factory=list)


class RubricImportResult(BaseModel):
    rubric: RubricRead
    warnings: list[str] = Field(default_factory=list)
    template_summary: dict = Field(default_factory=dict)
