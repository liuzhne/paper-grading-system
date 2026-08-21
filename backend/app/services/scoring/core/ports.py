"""Infrastructure-neutral runtime ports for the scoring Core.

The interfaces in this module deliberately describe behavior rather than
adapter implementations.  Core callers can therefore be exercised with
in-memory substitutes without importing a database, web framework, filesystem
or provider SDK.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol, runtime_checkable

from backend.app.services.scoring.core.contracts import PromptEnvelopeV1


@runtime_checkable
class LLMRuntime(Protocol):
    """Execute one immutable, provider-ready scoring request."""

    def score(self, *, envelope: PromptEnvelopeV1) -> Mapping[str, object]: ...


@runtime_checkable
class CacheLedger(Protocol):
    """Scope-aware cache and audit-ledger boundary."""

    def get(
        self,
        *,
        cache_key: str,
        requester_scope: str,
        now: datetime,
    ) -> object | None: ...

    def put(
        self,
        *,
        cache_key: str,
        response: object,
        access_scope: str,
        expires_at: datetime,
    ) -> None: ...


@runtime_checkable
class EvidenceRetriever(Protocol):
    """Select detached evidence from an immutable document projection."""

    def retrieve(
        self,
        *,
        document: Mapping[str, object],
        query: Mapping[str, object],
        limit: int,
    ) -> tuple[Mapping[str, object], ...]: ...


@runtime_checkable
class CheckerRegistry(Protocol):
    """Resolve versioned deterministic checkers and expose their manifest."""

    def resolve(self, *, checker_key: str, checker_version: str) -> object: ...

    def manifest(
        self,
        *,
        profile_key: str,
        document_schema_version: str,
    ) -> Mapping[str, Mapping[str, object]]: ...


@runtime_checkable
class Clock(Protocol):
    """Inject time into Core operations that need expiry or audit timestamps."""

    def now(self) -> datetime: ...


__all__ = [
    "CacheLedger",
    "CheckerRegistry",
    "Clock",
    "EvidenceRetriever",
    "LLMRuntime",
]
