from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class ReleaseGateProfileCreate(BaseModel):
    gate_key: Literal["GATE-01", "GATE-02", "GATE-03"]
    name: str = Field(min_length=1, max_length=200)
    rubric_id: str = Field(min_length=1)
    rubric_version_id: str = Field(min_length=1)
    dataset_identity: dict
    model_identity: dict
    anchors_identity: dict
    acceptance_thresholds: dict
    regression_tolerances: dict


class ReleaseGateProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    gate_key: str
    name: str
    rubric_id: str
    rubric_version_id: str
    dataset_identity: dict
    model_identity: dict
    anchors_identity: dict
    acceptance_thresholds: dict
    regression_tolerances: dict
    profile_hash: str
    status: str
    created_by: str
    created_at: datetime


class ReleaseGateRunCreate(BaseModel):
    candidate_record: dict


class ReleaseGateRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    profile_id: str
    evaluation_id: str
    candidate_sha256: str
    candidate_record: dict
    status: str
    final_record: dict | None = None
    final_record_sha256: str | None = None
    created_at: datetime
    finalized_at: datetime | None = None


class ReleaseGateApprovalCreate(BaseModel):
    privacy_review: dict
