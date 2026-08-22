from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ProviderType = Literal["openai_responses", "openai_compatible"]


class AIConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    provider_type: ProviderType
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
