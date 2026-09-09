"""前端 v2 能力投影的响应契约（计划 §2.1、§6）。

本模块只描述**可以下发给已登录用户**的信息。任何 Key、Secret、
webapp URL 或平台敏感配置都不得进入这些模型。
"""

from typing import Optional

from pydantic import BaseModel
from pydantic import Field


class CapabilityAbilities(BaseModel):
    """导航与控件可见性开关；服务端仍对每个端点单独鉴权。"""

    view_organization_ops: bool
    view_platform_ops: bool
    manage_members: bool
    manage_own_ai_connections: bool


class CapabilityUpload(BaseModel):
    """上传能力投影：UI 据此提示限制，服务端最终校验。"""

    provider: str
    max_size_mb: int
    tus_threshold_mb: int
    accepted_extensions: list[str]


class CapabilityExport(BaseModel):
    """导出通道可用性；离线部署下 Sheets 必须为不可用。"""

    sheets_available: bool
    offline_mode: bool


class CapabilityLLM(BaseModel):
    """这个用户现在能不能调模型（D-027、D-028）。

    前端据此决定是否把用户引导去配置 BYOK。两条来源任一可用即可，**停用的都不算**。
    """

    platform_model_available: bool
    has_own_connection: bool
    can_use_llm: bool


class CapabilitiesRead(BaseModel):
    user_id: str
    organization_id: Optional[str] = None
    organization_role: Optional[str] = None
    platform_role: str
    auth_enforced: bool
    llm: CapabilityLLM
    abilities: CapabilityAbilities
    upload: CapabilityUpload
    export: CapabilityExport


class PlatformLLMConfigWrite(BaseModel):
    """平台默认模型的写入请求（D-028）。

    `api_key` 只进不出：读接口返回脱敏视图，永远不回显它。
    """

    provider_type: str
    base_url: str
    model_name: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    provider_options: dict | None = None
