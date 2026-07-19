"""Persistence adapter for authoritative Core scoring runs.

The Core returns immutable transport-neutral DTOs.  This module is the single
place that translates them into the legacy ``ScoringRun``/``ScoreItem``
schema, while preserving enough frozen identity for replay and audit.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.db.models import Paper, ScoreItem, ScoringRun
from backend.app.core.config import settings
from backend.app.services.storage.local import read_json, write_json
from backend.app.services.scoring.core.contracts import (
    DocumentSnapshot,
    ScoringRequest,
)
from backend.app.services.scoring.core.results import ScoringOutcome


def _mapping(value, *, label: str) -> dict:
    def thaw(item):
        if isinstance(item, Mapping):
            return {str(key): thaw(nested) for key, nested in item.items()}
        if isinstance(item, (list, tuple)):
            return [thaw(nested) for nested in item]
        return deepcopy(item)

    if isinstance(value, Mapping):
        return thaw(value)
    method = getattr(value, "to_mapping", None)
    if not callable(method):
        raise TypeError(f"{label} must be a mapping or immutable Core DTO")
    mapped = method()
    if not isinstance(mapped, Mapping):
        raise TypeError(f"{label}.to_mapping() must return a mapping")
    return thaw(mapped)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _request_mapping(value) -> dict:
    raw = _mapping(value, label="request")
    try:
        return ScoringRequest.from_mapping(raw).to_mapping()
    except (TypeError, ValueError) as exc:
        # Public persistence errors name the security boundary.  In
        # particular, callers retaining an occupied key while changing any
        # replay identity receive an explicit idempotency/collision failure.
        raise ValueError(f"idempotency identity collision or invalid request: {exc}") from exc


def _outcome_mapping(value) -> dict:
    raw = _mapping(value, label="outcome")
    return ScoringOutcome.from_mapping(raw).to_mapping()


def _validate_document_snapshot(*, store, ref, request_document: Mapping) -> None:
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("document snapshot ref is required for replay")
    if store is None or not callable(getattr(store, "resolve", None)):
        raise TypeError("document snapshot store must expose resolve(ref=...)")
    try:
        resolved = store.resolve(ref=ref)
    except (KeyError, TypeError, ValueError):
        raise
    except Exception as exc:  # adapter errors retain a stable domain boundary
        raise ValueError("document snapshot ref could not be resolved") from exc
    try:
        normalized = DocumentSnapshot.from_mapping(
            _mapping(resolved, label="resolved document snapshot")
        ).to_mapping()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"document snapshot content/hash verification failed: {exc}") from exc
    supplied = DocumentSnapshot.from_mapping(dict(request_document)).to_mapping()
    if normalized != supplied:
        raise ValueError("document snapshot ref resolves to a different snapshot identity")


def _request_identity(request: Mapping) -> dict:
    submission = request["submission"]
    document = request["document"]
    plan = request["plan"]
    runtime = request["runtime_identity"]
    return {
        "idempotency_key": request["idempotency_key"],
        "source_artifact_hash": submission["source_artifact_hash"],
        "document_snapshot_hash": document["document_snapshot_hash"],
        "normalized_content_hash": document["content_hash"],
        "rubric_source_kind": plan["rubric_source_kind"],
        "rubric_snapshot_hash": plan["rubric_snapshot_hash"],
        "rubric_version_id": plan.get("rubric_version_id"),
        "rubric_version_hash": plan.get("rubric_version_hash"),
        "rubric_hash_scheme": plan.get("rubric_hash_scheme"),
        "execution_plan_hash": plan["plan_hash"],
        "policy_hash": plan["policy_hash"],
        "business_profile_key": submission["profile_key"],
        "engine_version": runtime["engine_version"],
        "model_provider": runtime["provider"]["name"],
        "model_name": runtime["provider"]["model"],
        "model_version": runtime["provider"]["model_version"],
        "rescore_generation": request["rescore_generation"],
    }


def _stored_identity(run: ScoringRun) -> dict:
    return {
        name: getattr(run, name)
        for name in (
            "idempotency_key",
            "source_artifact_hash",
            "document_snapshot_hash",
            "normalized_content_hash",
            "rubric_source_kind",
            "rubric_snapshot_hash",
            "rubric_version_id",
            "rubric_version_hash",
            "rubric_hash_scheme",
            "execution_plan_hash",
            "policy_hash",
            "business_profile_key",
            "engine_version",
            "model_provider",
            "model_name",
            "model_version",
            "rescore_generation",
        )
    }


def _same_identity(run: ScoringRun, request: Mapping) -> bool:
    return _stored_identity(run) == _request_identity(request)


def _validate_outcome_identity(request: Mapping, outcome: Mapping) -> None:
    expected = {
        "idempotency_key": request["idempotency_key"],
        "document_snapshot_hash": request["document"]["document_snapshot_hash"],
        "rubric_snapshot_hash": request["plan"]["rubric_snapshot_hash"],
        "plan_hash": request["plan"]["plan_hash"],
        "policy_hash": request["plan"]["policy_hash"],
        "profile_key": request["submission"]["profile_key"],
    }
    if outcome["request_identity"] != expected:
        raise ValueError("outcome request identity does not match scoring request")
    if outcome["audit_identity"] != request["runtime_identity"]:
        raise ValueError("outcome runtime identity does not match scoring request")


def _group_rule_results(request: Mapping, outcome: Mapping) -> dict[str, list[dict]]:
    criterion_by_rule = {
        node["rule_code"]: node["criterion_code"]
        for node in request["plan"]["nodes"]
    }
    contributions_by_rule: dict[str | None, list[dict]] = {}
    for contribution in outcome["score_contributions"]:
        contributions_by_rule.setdefault(contribution.get("rule_code"), []).append(
            deepcopy(contribution)
        )
    grouped: dict[str, list[dict]] = {}
    for decision in outcome["rule_decisions"]:
        rule_code = decision["rule_code"]
        criterion_code = criterion_by_rule.get(rule_code)
        if criterion_code is None:
            raise ValueError(f"outcome contains unknown rule identity: {rule_code}")
        grouped.setdefault(criterion_code, []).append(
            {
                "schema_version": "rule-result@1",
                "rule_code": rule_code,
                "status": decision["status"],
                "evidence_refs": deepcopy(decision["evidence_refs"]),
                "score_contributions": contributions_by_rule.get(rule_code, []),
            }
        )
    return grouped


def _score_items(
    *,
    request: Mapping,
    outcome: Mapping,
    criterion_id_by_code: Mapping[str, str],
) -> list[ScoreItem]:
    rule_results = _group_rule_results(request, outcome)
    contributions_by_criterion: dict[str, list[dict]] = {}
    for contribution in outcome["score_contributions"]:
        contributions_by_criterion.setdefault(
            contribution["criterion_code"], []
        ).append(deepcopy(contribution))

    items = []
    for criterion in outcome["criterion_outcomes"]:
        code = criterion["criterion_code"]
        criterion_id = criterion_id_by_code.get(code)
        if criterion_id is None:
            raise ValueError(f"outcome criterion identity is unknown: {code}")
        status = criterion["status"]
        evidence_refs = [
            deepcopy(ref)
            for result in rule_results.get(code, [])
            for ref in result["evidence_refs"]
        ]
        deductions = [
            entry["amount"]
            for entry in contributions_by_criterion.get(code, [])
            if entry["kind"] == "deduction"
        ]
        aggregation = {
            "schema_version": "criterion-aggregation@1",
            "criterion_code": code,
            "status": status,
            "contributions": contributions_by_criterion.get(code, []),
        }
        items.append(
            ScoreItem(
                criterion_id=criterion_id,
                max_score=Decimal(criterion["max_score"]),
                ai_score=(
                    None
                    if criterion["auto_score"] is None
                    else Decimal(criterion["auto_score"])
                ),
                final_score=(
                    None
                    if criterion["final_score"] is None
                    else Decimal(criterion["final_score"])
                ),
                evidence_sufficient=status == "calculated",
                reason=f"Core criterion outcome: {status}",
                deductions=deductions,
                deduction_items=[
                    {
                        "points": str(abs(Decimal(value))),
                        "reason": "Authorized Core rule deduction",
                    }
                    for value in deductions
                ],
                evidence=evidence_refs,
                band_selection=None,
                sub_results=None,
                suggestion=None,
                confidence=None,
                need_manual_review=status != "calculated",
                raw_model_output={
                    "criterion_outcome": deepcopy(criterion),
                    "rule_results": deepcopy(rule_results.get(code, [])),
                },
                aggregation=aggregation,
                aggregation_schema_version="criterion-aggregation@1",
                auto_score_status=status,
                rule_results=rule_results.get(code, []),
                rule_results_schema_version="rule-results@1",
            )
        )
    if set(criterion_id_by_code) != {
        item["criterion_code"] for item in outcome["criterion_outcomes"]
    }:
        raise ValueError("criterion identity map does not match outcome criteria")
    return items


class CoreRunPersistence:
    """Persist one immutable Core request/outcome with DB-backed idempotency."""

    def __init__(self, db, *, document_snapshot_store):
        self.db = db
        self.document_snapshot_store = document_snapshot_store

    def persist(
        self,
        *,
        request,
        outcome,
        paper_id: str,
        rubric_id: str,
        criterion_id_by_code: Mapping[str, str],
        workflow_profile: str,
        document_snapshot_ref: str,
    ) -> ScoringRun:
        request_mapping = _request_mapping(request)
        outcome_mapping = _outcome_mapping(outcome)
        _validate_outcome_identity(request_mapping, outcome_mapping)
        _validate_document_snapshot(
            store=self.document_snapshot_store,
            ref=document_snapshot_ref,
            request_document=request_mapping["document"],
        )
        if not isinstance(workflow_profile, str) or not workflow_profile.strip():
            raise ValueError("workflow profile must be non-empty")
        paper = self.db.get(Paper, paper_id)
        if paper is None:
            raise ValueError("paper identity does not exist")
        if paper.batch.rubric_id != rubric_id:
            raise ValueError("paper/rubric identity mismatch")

        existing = self.db.scalar(
            select(ScoringRun).where(
                ScoringRun.idempotency_key == request_mapping["idempotency_key"]
            )
        )
        if existing is not None:
            if not _same_identity(existing, request_mapping):
                raise ValueError("idempotency key collision with different replay identity")
            return existing

        plan = request_mapping["plan"]
        document = request_mapping["document"]
        submission = request_mapping["submission"]
        runtime = request_mapping["runtime_identity"]
        provider = runtime["provider"]
        now = _utcnow()
        run = ScoringRun(
            paper_id=paper_id,
            owner_id=getattr(paper, "owner_id", None),
            rubric_id=rubric_id,
            model_provider=provider["name"],
            model_name=provider["model"],
            model_version=provider["model_version"],
            status="scored",
            started_at=now,
            finished_at=now,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            coherence_findings=[],
            format_findings=[],
            ai_total_score=(
                None
                if outcome_mapping["unrounded_total"] is None
                else Decimal(outcome_mapping["unrounded_total"])
            ),
            final_total_score=(
                None
                if outcome_mapping["final_total"] is None
                else Decimal(outcome_mapping["final_total"])
            ),
            grade=outcome_mapping["grade"],
            need_manual_review=(
                outcome_mapping["status"] != "completed"
                or bool(outcome_mapping["review_issues"])
            ),
            policy_snapshot=deepcopy(plan["policy_snapshot"]),
            policy_hash=plan["policy_hash"],
            policy_schema_version=plan["policy_snapshot"]["schema_version"],
            rubric_source_kind=plan["rubric_source_kind"],
            rubric_snapshot_hash=plan["rubric_snapshot_hash"],
            rubric_version_id=plan.get("rubric_version_id"),
            rubric_version_hash=plan.get("rubric_version_hash"),
            rubric_hash_scheme=plan.get("rubric_hash_scheme"),
            business_profile_key=submission["profile_key"],
            workflow_profile=workflow_profile,
            execution_plan_snapshot=deepcopy(plan),
            execution_plan_hash=plan["plan_hash"],
            plan_schema_version=plan["schema_version"],
            checker_manifest=deepcopy(plan["checker_manifest"]),
            source_artifact_hash=submission["source_artifact_hash"],
            normalized_content_hash=document["content_hash"],
            document_snapshot_ref=document_snapshot_ref,
            document_snapshot_hash=document["document_snapshot_hash"],
            document_schema_version=document["schema_version"],
            engine_version=runtime["engine_version"],
            rescore_generation=request_mapping["rescore_generation"],
            idempotency_key=request_mapping["idempotency_key"],
        )
        run.items.extend(
            _score_items(
                request=request_mapping,
                outcome=outcome_mapping,
                criterion_id_by_code=criterion_id_by_code,
            )
        )
        self.db.add(run)
        paper.status = "scored"
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            winner = self.db.scalar(
                select(ScoringRun).where(
                    ScoringRun.idempotency_key
                    == request_mapping["idempotency_key"]
                )
            )
            if winner is None:
                raise
            if not _same_identity(winner, request_mapping):
                raise ValueError(
                    "idempotency key collision with different replay identity"
                ) from exc
            return winner
        self.db.refresh(run)
        return run


class LocalDocumentSnapshotStore:
    """Content-addressed JSON store whose public refs never expose file paths."""

    _PREFIX = "document-snapshot:sha256:"

    def _path(self, snapshot_hash: str) -> Path:
        return (
            settings.STORAGE_ROOT
            / "document_snapshots"
            / "sha256"
            / f"{snapshot_hash}.json"
        )

    def put(self, snapshot) -> str:
        value = DocumentSnapshot.from_mapping(
            _mapping(snapshot, label="document snapshot")
        ).to_mapping()
        snapshot_hash = value["document_snapshot_hash"]
        path = self._path(snapshot_hash)
        if path.exists():
            existing = DocumentSnapshot.from_mapping(read_json(path)).to_mapping()
            if existing != value:
                raise ValueError("document snapshot hash collision in local store")
        else:
            write_json(path, value)
        return self._PREFIX + snapshot_hash

    def resolve(self, *, ref: str):
        if not isinstance(ref, str) or not ref.startswith(self._PREFIX):
            raise ValueError("document snapshot ref uses an unsupported scheme")
        snapshot_hash = ref[len(self._PREFIX) :]
        if (
            len(snapshot_hash) != 64
            or any(character not in "0123456789abcdef" for character in snapshot_hash)
        ):
            raise ValueError("document snapshot ref contains an invalid hash")
        path = self._path(snapshot_hash)
        if not path.exists():
            raise KeyError(f"document snapshot ref does not exist: {ref}")
        value = DocumentSnapshot.from_mapping(read_json(path)).to_mapping()
        if value["document_snapshot_hash"] != snapshot_hash:
            raise ValueError("document snapshot ref/hash mismatch")
        return value


__all__ = ["CoreRunPersistence", "LocalDocumentSnapshotStore"]
