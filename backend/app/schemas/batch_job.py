from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import computed_field

from backend.app.services.batch_scoring.jobs import RUNNER_LEASE_SECONDS


class BatchScoringJobCreate(BaseModel):
    rescore: bool = False
    max_workers: int = Field(default=2, ge=1, le=16)
    observation_policy: Optional[dict[str, Any]] = None


class BatchScoringItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    paper_id: str
    status: str
    attempt_count: int
    scoring_run_id: Optional[str] = None
    baseline_scoring_run_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    telemetry: Optional[dict[str, Any]] = None
    attempt_history: list[dict[str, Any]]
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class BatchScoringJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    grading_batch_id: str
    generation: int
    rescore: bool
    max_workers: int
    status: str
    total_items: int
    pending_count: int
    running_count: int
    succeeded_count: int
    skipped_count: int
    failed_count: int
    canceled_count: int
    observation_policy: dict[str, Any]
    observation_policy_hash: str
    metrics_snapshot: Optional[dict[str, Any]] = None
    heartbeat_at: Optional[datetime] = None
    cancel_requested_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    items: list[BatchScoringItemRead]

    @computed_field
    @property
    def runner_lease_seconds(self) -> int:
        return RUNNER_LEASE_SECONDS

    @computed_field
    @property
    def heartbeat_state(self) -> str:
        if self.status not in ("running", "cancel_requested"):
            return "inactive"
        if self.heartbeat_at is None:
            return "stale"
        # PostgreSQL stores these timestamps as naive UTC.  ``datetime.now(None)``
        # means local wall-clock time, so hosts outside UTC would otherwise mark
        # a fresh heartbeat stale by their timezone offset (for example +08:00).
        heartbeat = self.heartbeat_at
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age = (now - heartbeat.astimezone(timezone.utc)).total_seconds()
        return "stale" if age > RUNNER_LEASE_SECONDS else "healthy"


class ScoreEstimatePaper(BaseModel):
    paper_id: str
    title: Optional[str] = None
    supported: bool
    reason: Optional[str] = None
    semantic_rules: int
    reused_rules: int
    calls: int
    estimated_input_tokens: int


class ScoreEstimateViolation(BaseModel):
    kind: str
    estimated: int
    cap: int
    paper_id: Optional[str] = None
    title: Optional[str] = None


class ScoreEstimateCaps(BaseModel):
    per_paper: int
    per_batch: int


class BatchScoreEstimateRead(BaseModel):
    """Local, conservative input-token estimate; no provider is called."""

    batch_id: str
    rescore: bool
    paper_count: int
    skipped_complete_papers: int
    unsupported_papers: int
    calls: int
    reused_rules: int
    estimated_input_tokens: int
    caps: ScoreEstimateCaps
    violations: list[ScoreEstimateViolation]
    papers: list[ScoreEstimatePaper]
