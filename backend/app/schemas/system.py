"""前端 v2 能力投影的响应契约（计划 §2.1、§6）。

本模块只描述**可以下发给已登录用户**的信息。任何 Key、Secret、
webapp URL 或平台敏感配置都不得进入这些模型。
"""

from typing import Optional

from pydantic import BaseModel


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


class CapabilitiesRead(BaseModel):
    user_id: str
    organization_id: Optional[str] = None
    organization_role: Optional[str] = None
    platform_role: str
    auth_enforced: bool
    abilities: CapabilityAbilities
    upload: CapabilityUpload
    export: CapabilityExport
