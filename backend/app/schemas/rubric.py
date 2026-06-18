from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator


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
        if not self.criteria:
            raise ValueError("rubric must contain at least one criterion")
        max_sum = round(sum(item.max_score for item in self.criteria), 2)
        if max_sum != round(self.total_score, 2):
            weighted_sum = round(sum(item.weight or 0 for item in self.criteria), 2)
            if weighted_sum != round(self.total_score, 2):
                raise ValueError("criterion max_score sum or weight sum must equal total_score")
        return self


class RubricCloneRequest(BaseModel):
    new_version: str = Field(min_length=1)
    name: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = None


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
