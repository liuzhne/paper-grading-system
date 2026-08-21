"""Minimal business-profile boundary.

A profile may interpret business metadata and enrich a prompt, but it does not
own Core identities, persistence, provider calls, or score aggregation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from backend.app.services.scoring.core.contracts import (
    DocumentSnapshot,
    SubmissionSnapshot,
)


@runtime_checkable
class BusinessProfile(Protocol):
    """Business interpretation used while assembling a Core prompt."""

    profile_key: str
    profile_version: str
    prompt_version: str

    def select_prompt_metadata(
        self,
        *,
        metadata: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    def build_prompt_extensions(
        self,
        *,
        submission_snapshot: SubmissionSnapshot,
        document_snapshot: DocumentSnapshot,
    ) -> Mapping[str, object]: ...


__all__ = ["BusinessProfile"]
