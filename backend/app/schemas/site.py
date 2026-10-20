"""官网申请的请求与响应（docs/官网申请落库改造方案.md）。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


SiteLeadKind = Literal["trial", "deploy_assessment"]
SiteLeadStatus = Literal["new", "contacted", "closed", "spam"]


class SiteLeadSubmit(BaseModel):
    """公开提交。上限比落库长度宽松：清洗与逐字段中文提示在路由里做，这里只防超大请求体。"""

    model_config = ConfigDict(extra="ignore")

    kind: SiteLeadKind
    email: str = Field(max_length=400)
    organization_name: str | None = Field(default=None, max_length=400)
    note: str | None = Field(default=None, max_length=2000)
    #: 蜜罐：页面上对人隐藏，填了就静默丢弃。
    website: str | None = Field(default=None, max_length=400)


class SiteLeadReceived(BaseModel):
    status: Literal["received"] = "received"


class SiteLeadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: SiteLeadKind
    email: str
    organization_name: str | None
    note: str | None
    source_path: str
    status: SiteLeadStatus
    submit_count: int
    created_at: datetime
    last_submitted_at: datetime
    handled_at: datetime | None


class SiteLeadPage(BaseModel):
    items: list[SiteLeadRead]
    total: int
    new_count: int


class SiteLeadStatusUpdate(BaseModel):
    status: SiteLeadStatus
