from datetime import datetime
from typing import Any
from typing import Literal
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
    visibility: Literal["system", "organization", "private"] = "private"
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


class AtomicRuleConfirmRequest(RubricLifecycleReason):
    compilation_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    content_token: str = Field(min_length=64, max_length=64)


class RubricPublishRequest(RubricLifecycleReason):
    compilation_id: Optional[str] = None
    #: 分享范围（D-029）。不传就沿用当前范围——扩大范围必须是显式动作。
    #: 与编译产物在同一次发布里原子生效，之后与版本一起冻结。
    visibility: Optional[Literal["private", "organization", "system"]] = None


class AtomicRuleEditRequest(RubricLifecycleReason):
    changes: dict


class RubricDraftRecompileRequest(RubricLifecycleReason):
    supersedes_compilation_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    name: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = None
    total_score: Optional[float] = Field(default=None, gt=0)
    criteria: list[RubricCriterionCreate] = Field(min_length=1)
    atomic_rules: Optional[list[dict]] = None
    global_policy: dict = Field(default_factory=dict)
    workflow_profile: str = Field(default="manual_json", min_length=1)
    business_profile_key: str = Field(default="thesis", min_length=1)


class RubricAIRuleDraftRequest(BaseModel):
    criteria: list[RubricCriterionCreate] = Field(min_length=1)
    ai_connection_id: Optional[str] = None


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
    visibility: str = "private"
    description: Optional[str] = None
    format_spec: dict = Field(default_factory=dict)
    created_at: datetime
    published_at: Optional[datetime] = None
    criteria: list[RubricCriterionRead] = Field(default_factory=list)


class RubricImportResult(BaseModel):
    rubric: RubricRead
    warnings: list[str] = Field(default_factory=list)
    template_summary: dict = Field(default_factory=dict)
    coverage: dict = Field(default_factory=dict)
    triggers: list[dict] = Field(default_factory=list)
    unclaimed_summary: dict = Field(default_factory=dict)
    conflicts: list[dict] = Field(default_factory=list)
    artifacts: list[dict] = Field(default_factory=list)


class RubricImportCriterionDraft(BaseModel):
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    max_score: int = Field(gt=0)
    description: Optional[str] = None
    display_order: int = Field(default=0, ge=0)
    source_refs: list[dict] = Field(default_factory=list)
    parse_status: str = "parsed"
    deleted: bool = False


class RubricScoreAdjustment(BaseModel):
    code: str
    original: str
    rounded: int
    message: str


class RubricImportSessionRead(BaseModel):
    id: str
    status: Literal["draft", "confirmed", "expired", "cancelled"]
    state_version: int
    rubric_id: Optional[str] = None
    name: str
    version: str
    description: Optional[str] = None
    visibility: Literal["system", "organization", "private"]
    total_score: int = Field(ge=0)
    criteria: list[RubricImportCriterionDraft] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    score_adjustments: list[RubricScoreAdjustment] = Field(default_factory=list)
    files: dict = Field(default_factory=dict)
    template_summary: dict = Field(default_factory=dict)
    coverage: dict = Field(default_factory=dict)
    conflicts: list[dict] = Field(default_factory=list)
    expires_at: datetime


class RubricImportSessionUpdate(BaseModel):
    expected_state_version: int = Field(ge=1)
    name: Optional[str] = Field(default=None, min_length=1)
    version: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = None
    total_score: Optional[int] = Field(default=None, gt=0)
    criteria: Optional[list[RubricImportCriterionDraft]] = None


class RubricImportSessionConfirm(BaseModel):
    expected_state_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)


class RubricImportConflictResolve(BaseModel):
    expected_state_version: int = Field(ge=1)
    decision: Literal["use_excel", "use_word"]
    reason: str = Field(min_length=1, max_length=500)


class RubricImportSessionStateRequest(BaseModel):
    expected_state_version: int = Field(ge=1)


class RubricImportSessionConfirmResult(BaseModel):
    status: Literal["confirmed"]
    import_session_id: str
    state_version: int
    rubric: RubricRead


class RubricReuploadCriterionDiff(BaseModel):
    code: str
    name: str
    change_type: Literal["added", "removed", "modified", "unchanged"]
    old_max_score: Optional[float] = None
    new_max_score: Optional[float] = None
    old_name: Optional[str] = None
    new_name: Optional[str] = None


class RubricReuploadPreviewResponse(BaseModel):
    fingerprint: str
    file_type: Literal["rules", "template"]
    filename: str
    criteria_diff: list[RubricReuploadCriterionDiff] = Field(default_factory=list)
    added_count: int = 0
    removed_count: int = 0
    modified_count: int = 0
    unchanged_count: int = 0
    invalidated_rules_count: int = 0
    retained_rules_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class RubricImportReuploadPreviewResponse(BaseModel):
    fingerprint: str
    criteria_diff: list[RubricReuploadCriterionDiff] = Field(default_factory=list)
    added_count: int = 0
    removed_count: int = 0
    modified_count: int = 0
    unchanged_count: int = 0
    score_adjustments: list[RubricScoreAdjustment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RubricImportSourcePreviewItem(BaseModel):
    unit_id: Optional[str] = None
    kind: Optional[str] = None
    locator: dict = Field(default_factory=dict)
    text: str = ""


class RubricImportSourcePreview(BaseModel):
    document: Literal["word", "excel"]
    items: list[RubricImportSourcePreviewItem] = Field(default_factory=list)


class RubricSourceFiles(BaseModel):
    rules: Optional[str] = None
    template: Optional[str] = None


class RubricSourceFileMetadata(BaseModel):
    size_bytes: int = Field(ge=0)
    uploaded_at: datetime


class RubricSourceFilesMetadata(BaseModel):
    rules: Optional[RubricSourceFileMetadata] = None
    template: Optional[RubricSourceFileMetadata] = None


class RubricSourceReference(BaseModel):
    kind: Optional[Literal["word", "excel"]] = None
    sheet_name: Optional[str] = None
    row_number: Optional[int] = None
    locator: Optional[str] = None
    text: Optional[str] = None


class RubricCriterionSourceRead(BaseModel):
    code: str
    source_refs: list[RubricSourceReference] = Field(default_factory=list)
    parse_status: str


class RubricSourcePreviews(BaseModel):
    word: list[RubricImportSourcePreviewItem] = Field(default_factory=list)
    excel: list[RubricImportSourcePreviewItem] = Field(default_factory=list)


class RubricSourceWorkspaceRead(BaseModel):
    rubric_id: str
    compilation_id: Optional[str] = None
    files: RubricSourceFiles
    file_metadata: RubricSourceFilesMetadata
    criteria: list[RubricCriterionSourceRead] = Field(default_factory=list)
    score_adjustments: list[RubricScoreAdjustment] = Field(default_factory=list)
    previews: RubricSourcePreviews


class RubricStepOneConfirmRequest(RubricLifecycleReason):
    name: Optional[str] = Field(default=None, min_length=1)
    version: str = Field(min_length=1)
    description: Optional[str] = None
    total_score: float = Field(gt=0)
    business_profile_key: str = Field(default="thesis", min_length=1)
    criteria: list[RubricCriterionCreate] = Field(min_length=1)



class RuleReviewRequest(BaseModel):
    ai_connection_id: Optional[str] = None
    scope: Literal["priority", "all"] = "priority"
    dry_run: bool = False


class FindingDismissRequest(BaseModel):
    reason: str = Field(min_length=1)


class StructureSuggestionRequest(BaseModel):
    ai_connection_id: Optional[str] = None
    dry_run: bool = False


class StructureMergeRequest(BaseModel):
    fingerprint: str = Field(min_length=1)
    confirm: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class StructureUndoRequest(BaseModel):
    reason: str = Field(min_length=1)


class UnitClassificationRequest(BaseModel):
    unit_ids: Optional[list[str]] = Field(default=None, max_length=500)
    ai_connection_id: Optional[str] = None


class SourceUnitBatchResolveRequest(BaseModel):
    unit_ids: list[str] = Field(min_length=1, max_length=500)
    action: Literal["assign", "not_rule", "restore"]
    reason: str = Field(min_length=1)
    criterion_code: Optional[str] = None


class SourceUnitResolveRequest(BaseModel):
    action: Literal["assign", "not_rule", "restore"]
    reason: str = Field(min_length=1)
    criterion_code: Optional[str] = None
