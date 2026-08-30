from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class PaperUpdate(BaseModel):
    student_id: Optional[str] = None
    student_name: Optional[str] = None
    title: Optional[str] = None
    department: Optional[str] = None
    major: Optional[str] = None
    advisor: Optional[str] = None


class DirectUploadIntentCreate(BaseModel):
    batch_id: str
    file_name: str = Field(min_length=1, max_length=500)
    content_type: str = Field(default="application/octet-stream", max_length=200)
    byte_size: int = Field(gt=0)


class CompleteDirectUpload(BaseModel):
    byte_size: int = Field(gt=0)


class PaperRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    batch_id: str
    student_id: Optional[str] = None
    student_name: Optional[str] = None
    title: Optional[str] = None
    department: Optional[str] = None
    major: Optional[str] = None
    advisor: Optional[str] = None
    file_name: str
    parsed_text_path: Optional[str] = None
    parse_quality: Optional[float] = None
    status: str
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class DirectUploadIntentRead(BaseModel):
    paper: PaperRead
    mode: str
    signed_url: str
    token: str
    tus_endpoint: str
    bucket_name: str
    object_path: str
    threshold_bytes: int


class PaperChunkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    paper_id: str
    section_title: Optional[str] = None
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    paragraph_ids: list
    text: str
    created_at: datetime


class ParsedPaperResponse(BaseModel):
    paper_id: str
    parsed: dict
