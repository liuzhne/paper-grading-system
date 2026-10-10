from datetime import datetime
from typing import Any
from typing import Literal
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


AITaskKind = Literal["rule_draft", "unit_classification", "rule_review", "structure_suggestion"]


class AITaskCreate(BaseModel):
    kind: AITaskKind
    # 每种操作自己的参数，例如起草的 {"criterion": {...}}、归类的 {"unit_ids": [...]}。
    params: dict[str, Any] = Field(default_factory=dict)
    ai_connection_id: Optional[str] = None
    # “重新生成”：作废同内容的旧任务（进行中的先取消）后新建。
    regenerate: bool = False


class AITaskItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    ordinal: int
    status: str
    attempt_count: int
    deferral_count: int
    # 归类条目处理的单元数（每批最多 3 个）；起草条目为 0。
    unit_count: int = 0
    not_before: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class AITaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    rubric_id: str
    scope: dict[str, Any]
    status: str
    model_name: Optional[str] = None
    ai_connection_id: Optional[str] = None
    total_items: int
    pending_count: int
    running_count: int
    succeeded_count: int
    failed_count: int
    canceled_count: int
    # 只在 succeeded 时有值：例如完整的起草草稿。
    result: Optional[dict[str, Any]] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    state_version: int
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: datetime
    items: list[AITaskItemRead]
