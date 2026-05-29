from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict


class ExportLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    scoring_run_id: str
    target_type: str
    target_id: Optional[str] = None
    status: str
    response: Optional[dict] = None
    error_message: Optional[str] = None
    created_at: datetime


class WriteSheetRequest(BaseModel):
    target_id: Optional[str] = None
