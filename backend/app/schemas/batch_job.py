from datetime import datetime
from typing import Any
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class BatchScoringJobCreate(BaseModel):
    rescore: bool = False
    max_workers: int = Field(default=4, ge=1, le=16)
    observation_policy: dict[str, Any]


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
    runner_token: Optional[str] = None
    heartbeat_at: Optional[datetime] = None
    cancel_requested_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    items: list[BatchScoringItemRead]
