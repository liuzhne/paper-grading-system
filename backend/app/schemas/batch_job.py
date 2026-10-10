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
    ordinal: int = 0
    stall_count: int = 0
    heartbeat_at: Optional[datetime] = None
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
        """healthy / stale / waiting / inactive。

        统一执行模型后心跳记在条目上：有在跑的条目就看它们最新的心跳；没有在跑、
        但还有待处理条目时是 waiting（排队等模型名额或叫醒），不是“执行中断”。
        """

        if self.status not in ("running", "cancel_requested"):
            return "inactive"
        items = getattr(self, "items", None)
        heartbeats = [self.heartbeat_at] if self.heartbeat_at is not None else []
        if items is not None:
            running = [item for item in items if item.status == "running"]
            if running:
                heartbeats = [item.heartbeat_at for item in running if item.heartbeat_at is not None]
            elif any(item.status == "pending" for item in items):
                return "waiting"
        if not heartbeats:
            return "stale"
        # PostgreSQL stores these timestamps as naive UTC.  ``datetime.now(None)``
        # means local wall-clock time, so hosts outside UTC would otherwise mark
        # a fresh heartbeat stale by their timezone offset (for example +08:00).
        latest = max(_as_utc(value) for value in heartbeats)
        age = (datetime.now(timezone.utc) - latest).total_seconds()
        return "stale" if age > RUNNER_LEASE_SECONDS else "healthy"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
