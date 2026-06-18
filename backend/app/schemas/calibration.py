from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class CalibrationAnchorCreate(BaseModel):
    rubric_id: str
    criterion_code: str
    score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    label: Optional[str] = None
    excerpt: str = Field(min_length=1)
    rationale: Optional[str] = None
    source: str = "范文"
    # score<=max_score 由路由 create_calibration_anchor 校验并返回 400（保持 API 契约）


class CalibrationAnchorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    rubric_id: str
    criterion_code: str
    score: float
    max_score: float
    label: Optional[str] = None
    excerpt: str
    rationale: Optional[str] = None
    source: str
    created_at: datetime
