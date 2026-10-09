from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ProviderType = Literal["openai_responses", "openai_compatible", "anthropic_messages"]
# 创建与测试时可以不选协议：auto 由服务端按地址后缀、已知平台和探测请求识别，
# 识别结果写回 provider_type；手动选择放在前端「高级设置」里，始终优先。
ProviderTypeChoice = Literal["auto", "openai_responses", "openai_compatible", "anthropic_messages"]
ProtocolDetection = Literal["manual", "url_suffix", "url_path", "known_host", "probe", "stored"]


class AIConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    provider_type: ProviderTypeChoice = "auto"
    base_url: str = Field(min_length=1, max_length=2000)
    model_name: str = Field(min_length=1, max_length=200)
    provider_options: dict = Field(default_factory=dict)
    api_key: str = Field(min_length=1, max_length=2000)


class AIConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, min_length=1, max_length=2000)
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
    provider_options: dict | None = None


class AIConnectionRotateKey(BaseModel):
    api_key: str = Field(min_length=1, max_length=2000)


class AIConnectionTestDraft(AIConnectionCreate):
    pass


class AIConnectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    owner_id: str
    name: str
    scope: str
    provider_type: ProviderType
    base_url: str
    model_name: str
    provider_options: dict
    key_version: int
    key_last4: str
    key_masked: str
    status: str
    last_verified_at: datetime | None = None
    last_error_code: str | None = None
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None = None


class AIConnectionDraftTestResult(BaseModel):
    provider_type: ProviderType
    model_name: str
    status: Literal["configuration_valid"]


class AIConnectionProbeResult(BaseModel):
    provider_type: ProviderType
    model_name: str
    status: Literal["verified"]
    # 测试草稿时返回识别依据与规范化后的地址（去掉粘贴进来的接口路径）；
    # 测试已保存连接不重新识别，依据为 stored。
    detection: ProtocolDetection = "stored"
    base_url: str | None = None
