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
import json
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.db.models import (
    DocumentSnapshot as StoredDocumentSnapshot,
    Paper,
    ScoreItem,
    ScoringRun,
    Submission,
)
from backend.app.services.storage.local import (
    artifact_not_found,
    artifact_ref,
    read_json,
    store_json,
)
from backend.app.services.scoring.core.contracts import (
    DocumentSnapshot,
    ScoringRequest,
)
from backend.app.services.scoring.core.results import ScoringOutcome
from backend.app.services.ai_connections import record_usage_ledger


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


def _finding_list(value, *, label: str) -> list:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{label} must be an array")
    findings = deepcopy(list(value))
    if any(not isinstance(item, Mapping) for item in findings):
        raise TypeError(f"{label} entries must be objects")
    return sorted(
        findings,
        key=lambda item: (
            str(item.get("severity") or ""),
            str(item.get("kind") or item.get("field") or ""),
            str(item.get("location") or ""),
            str(item.get("message") or ""),
            json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        ),
    )


def _contribution_sort_key(value: Mapping):
    kind_order = {"base": 0, "band": 1, "deduction": 2, "bonus": 3}
    return (
        str(value.get("criterion_code") or ""),
        kind_order.get(value.get("kind"), 99),
        str(value.get("rule_code") or ""),
        str(value.get("occurrence_id") or ""),
        str(value.get("amount") or ""),
    )


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
        "business_profile_version": runtime["profile_version"],
        "prompt_version": runtime["prompt_version"],
        "runtime_identity": deepcopy(runtime),
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
            "business_profile_version",
            "prompt_version",
            "runtime_identity",
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
    criterion_codes = set(criterion_by_rule.values())
    rule_order = {
        rule_code: index
        for index, rule_code in enumerate(request["plan"]["dependency_order"])
    }
    m4 = outcome["schema_version"] == "scoring-outcome@2"
    decisions_by_rule = {}
    expected_version_hash = (
        request["plan"].get("rubric_version_hash")
        or request["plan"]["rubric_snapshot_hash"]
    )
    for decision in outcome["rule_decisions"]:
        rule_code = decision["rule_code"]
        expected_criterion = criterion_by_rule.get(rule_code)
        if expected_criterion is None:
            raise ValueError(f"outcome contains unknown rule identity: {rule_code}")
        if rule_code in decisions_by_rule:
            raise ValueError(f"outcome contains duplicate rule identity: {rule_code}")
        if m4 and decision["criterion_code"] != expected_criterion:
            raise ValueError("outcome rule/criterion identity does not match scoring plan")
        if m4 and decision["version_hash"] != expected_version_hash:
            raise ValueError("outcome rule version identity does not match scoring plan")
        decisions_by_rule[rule_code] = decision
    if m4 and set(decisions_by_rule) != set(criterion_by_rule):
        raise ValueError("outcome rule identities do not match scoring plan")

    contributions_by_rule: dict[str | None, list[dict]] = {}
    for contribution in outcome["score_contributions"]:
        if m4:
            rule_code = contribution.get("rule_code")
            occurrence_id = contribution.get("occurrence_id")
            if rule_code is None:
                if occurrence_id is not None:
                    raise ValueError("rule-less contribution cannot name an occurrence")
                if contribution["criterion_code"] not in criterion_codes:
                    raise ValueError("outcome contribution criterion is unknown")
            else:
                decision = decisions_by_rule.get(rule_code)
                if decision is None:
                    raise ValueError("outcome contribution rule identity is unknown")
                if contribution["criterion_code"] != decision["criterion_code"]:
                    raise ValueError("outcome contribution criterion does not match its rule")
                occurrence_ids = {
                    item["occurrence_id"] for item in decision["occurrences"]
                }
                if occurrence_id is not None and occurrence_id not in occurrence_ids:
                    raise ValueError("outcome contribution occurrence is not audited")
                if contribution["kind"] == "deduction" and occurrence_id is None:
                    raise ValueError("deduction contribution requires an audited occurrence")
        contributions_by_rule.setdefault(contribution.get("rule_code"), []).append(
            deepcopy(contribution)
        )
    for values in contributions_by_rule.values():
        values.sort(key=_contribution_sort_key)
    grouped: dict[str, list[dict]] = {}
    for decision in outcome["rule_decisions"]:
        rule_code = decision["rule_code"]
        criterion_code = criterion_by_rule[rule_code]
        if m4:
            result = {
                "schema_version": "rule-result@2",
                "version_hash": decision["version_hash"],
                "rule_code": rule_code,
                "criterion_code": decision["criterion_code"],
                "direction": decision["direction"],
                "effect_type": decision["effect_type"],
                "status": decision["status"],
                "selected_level_code": decision["selected_level_code"],
                "evidence_refs": deepcopy(decision["evidence_refs"]),
                "occurrences": deepcopy(decision["occurrences"]),
                "calculated_effect": decision["calculated_effect"],
                "score_contributions": contributions_by_rule.get(rule_code, []),
            }
        else:
            result = {
                "schema_version": "rule-result@1",
                "rule_code": rule_code,
                "status": decision["status"],
                "evidence_refs": deepcopy(decision["evidence_refs"]),
                "score_contributions": contributions_by_rule.get(rule_code, []),
            }
        grouped.setdefault(criterion_code, []).append(result)
    for values in grouped.values():
        values.sort(key=lambda item: rule_order[item["rule_code"]])
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
    for values in contributions_by_criterion.values():
        values.sort(key=_contribution_sort_key)

    items = []
    m4 = outcome["schema_version"] == "scoring-outcome@2"
    rule_results_schema = "rule-results@2" if m4 else "rule-results@1"
    aggregation_schema = "criterion-aggregation@2" if m4 else "criterion-aggregation@1"
    for criterion in sorted(
        outcome["criterion_outcomes"],
        key=lambda item: item["criterion_code"],
    ):
        code = criterion["criterion_code"]
        criterion_id = criterion_id_by_code.get(code)
        if criterion_id is None:
            raise ValueError(f"outcome criterion identity is unknown: {code}")
        status = criterion["status"]
        # ``review_required`` is a Core criterion outcome, not a persisted
        # score-calculation failure.  The automatic score remains valid and
        # reviewability is represented by ``need_manual_review`` plus a null
        # final score.  The legacy table deliberately limits this column to
        # calculated/invalid/blocked.
        auto_score_status = (
            status if status in {"invalid", "blocked"} else "calculated"
        )
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
            "schema_version": aggregation_schema,
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
                aggregation_schema_version=aggregation_schema,
                auto_score_status=auto_score_status,
                rule_results=rule_results.get(code, []),
                rule_results_schema_version=rule_results_schema,
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
        paper_id: str | None = None,
        submission_id: str | None = None,
        document_snapshot_id: str | None = None,
        rubric_id: str,
        criterion_id_by_code: Mapping[str, str],
        workflow_profile: str,
        document_snapshot_ref: str,
        coherence_findings=None,
        format_findings=None,
        ai_connection_snapshot=None,
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

        if (paper_id is None) == (submission_id is None):
            raise ValueError("exactly one paper or submission target is required")

        plan = request_mapping["plan"]
        request_submission = request_mapping["submission"]
        request_document = request_mapping["document"]
        runtime = request_mapping["runtime_identity"]
        paper = None
        stored_submission = None
        stored_document = None
        owner_id = None

        if paper_id is not None:
            if document_snapshot_id is not None:
                raise ValueError("paper target cannot bind a generic document snapshot")
            paper = self.db.get(Paper, paper_id)
            if paper is None:
                raise ValueError("paper identity does not exist")
            if paper.batch.rubric_id != rubric_id:
                raise ValueError("paper/rubric identity mismatch")
            locked_version_id = getattr(paper.batch, "rubric_version_id", None)
            if plan["rubric_source_kind"] == "published_version":
                if (
                    locked_version_id is not None
                    and locked_version_id != plan["rubric_version_id"]
                ):
                    raise ValueError(
                        "paper batch lock does not match scoring rubric version"
                    )
            elif locked_version_id is not None:
                raise ValueError(
                    "version-locked paper cannot persist a legacy scoring plan"
                )
            owner_id = getattr(paper, "owner_id", None)
            organization_id = getattr(paper, "organization_id", None)
        else:
            if document_snapshot_id is None:
                raise ValueError("submission target requires a document snapshot")
            stored_submission = self.db.get(Submission, submission_id)
            if stored_submission is None:
                raise ValueError("submission identity does not exist")
            if request_submission["submission_id"] != stored_submission.id:
                raise ValueError("request/submission identity mismatch")
            if (
                request_submission["source_artifact_hash"]
                != stored_submission.source_artifact_hash
            ):
                raise ValueError("request/submission source artifact mismatch")
            artifact_refs = {
                item["ref"] for item in request_submission["artifact_refs"]
            }
            if stored_submission.source_artifact_ref not in artifact_refs:
                raise ValueError("request/submission source artifact ref mismatch")
            if request_submission["metadata"] != stored_submission.submission_metadata:
                raise ValueError("request/submission metadata mismatch")

            evaluation_batch = stored_submission.evaluation_batch
            if evaluation_batch.rubric_id != rubric_id:
                raise ValueError("submission batch/rubric identity mismatch")
            if plan["rubric_source_kind"] != "published_version":
                raise ValueError(
                    "evaluation batch requires a published scoring plan"
                )
            if evaluation_batch.rubric_version_id != plan["rubric_version_id"]:
                raise ValueError(
                    "submission batch lock does not match scoring rubric version"
                )
            if (
                evaluation_batch.business_profile_key
                != request_submission["profile_key"]
                or evaluation_batch.business_profile_key
                != plan["business_profile_key"]
                or evaluation_batch.business_profile_key
                != request_document["profile_key"]
            ):
                raise ValueError("submission batch business profile key mismatch")
            if (
                evaluation_batch.business_profile_version
                != runtime["profile_version"]
                or evaluation_batch.business_profile_version
                != plan["business_profile_version"]
                or evaluation_batch.business_profile_version
                != request_document["profile_version"]
            ):
                raise ValueError("submission batch business profile version mismatch")

            stored_document = self.db.get(
                StoredDocumentSnapshot,
                document_snapshot_id,
            )
            if stored_document is None:
                raise ValueError("document snapshot identity does not exist")
            if stored_document.submission_id != stored_submission.id:
                raise ValueError("document snapshot belongs to a different submission")
            expected_document_identity = {
                "schema_version": request_document["schema_version"],
                "business_profile_key": request_document["profile_key"],
                "business_profile_version": request_document["profile_version"],
                "parser_version": request_document["parser_version"],
                "normalizer_version": request_document["normalizer_version"],
                "content_hash": request_document["content_hash"],
                "snapshot_hash": request_document["document_snapshot_hash"],
                "snapshot_ref": document_snapshot_ref,
                "snapshot_payload": request_document,
            }
            actual_document_identity = {
                field: getattr(stored_document, field)
                for field in expected_document_identity
            }
            if actual_document_identity != expected_document_identity:
                raise ValueError("stored document snapshot identity mismatch")
            owner_id = evaluation_batch.owner_id
            organization_id = getattr(evaluation_batch, "organization_id", None)

        existing = self.db.scalar(
            select(ScoringRun).where(
                ScoringRun.idempotency_key == request_mapping["idempotency_key"]
            )
        )
        if existing is not None:
            if not _same_identity(existing, request_mapping):
                raise ValueError("idempotency key collision with different replay identity")
            if (
                existing.paper_id != paper_id
                or existing.submission_id != submission_id
                or existing.document_snapshot_id != document_snapshot_id
            ):
                raise ValueError("idempotency key collision with different target identity")
            return existing

        document = request_mapping["document"]
        submission = request_submission
        provider = runtime["provider"]
        stored_coherence_findings = _finding_list(
            coherence_findings, label="coherence findings"
        )
        stored_format_findings = _finding_list(
            format_findings, label="format findings"
        )
        now = _utcnow()
        run = ScoringRun(
            paper_id=paper_id,
            submission_id=submission_id,
            document_snapshot_id=document_snapshot_id,
            owner_id=owner_id,
            organization_id=organization_id,
            rubric_id=rubric_id,
            model_provider=provider["name"],
            model_name=provider["model"],
            model_version=provider["model_version"],
            ai_connection_id=(ai_connection_snapshot or {}).get("ai_connection_id"),
            ai_connection_key_version=(ai_connection_snapshot or {}).get("key_version"),
            ai_connection_snapshot=deepcopy(ai_connection_snapshot),
            status="scored",
            started_at=now,
            finished_at=now,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            coherence_findings=stored_coherence_findings,
            format_findings=stored_format_findings,
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
            business_profile_version=runtime["profile_version"],
            prompt_version=runtime["prompt_version"],
            runtime_identity=deepcopy(runtime),
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
        self.db.flush()
        record_usage_ledger(self.db, run)
        target = paper if paper is not None else stored_submission
        target.status = "pending_review" if run.need_manual_review else "scored"
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
            if (
                winner.paper_id != paper_id
                or winner.submission_id != submission_id
                or winner.document_snapshot_id != document_snapshot_id
            ):
                raise ValueError(
                    "idempotency key collision with different target identity"
                ) from exc
            return winner
        self.db.refresh(run)
        return run


class LocalDocumentSnapshotStore:
    """Content-addressed JSON store whose public refs never expose file paths."""

    _PREFIX = "document-snapshot:sha256:"

    def _ref(self, snapshot_hash: str):
        return artifact_ref(
            "document_snapshots/sha256", f"{snapshot_hash}.json"
        )

    def put(self, snapshot) -> str:
        value = DocumentSnapshot.from_mapping(
            _mapping(snapshot, label="document snapshot")
        ).to_mapping()
        snapshot_hash = value["document_snapshot_hash"]
        artifact = self._ref(snapshot_hash)
        try:
            existing = DocumentSnapshot.from_mapping(read_json(artifact)).to_mapping()
            if existing != value:
                raise ValueError("document snapshot hash collision in local store")
        except Exception as exc:
            if not artifact_not_found(exc):
                raise
            store_json(
                "document_snapshots/sha256",
                f"{snapshot_hash}.json",
                value,
            )
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
        artifact = self._ref(snapshot_hash)
        try:
            value = DocumentSnapshot.from_mapping(read_json(artifact)).to_mapping()
        except Exception as exc:
            if not artifact_not_found(exc):
                raise
            raise KeyError(f"document snapshot ref does not exist: {ref}")
        if value["document_snapshot_hash"] != snapshot_hash:
            raise ValueError("document snapshot ref/hash mismatch")
        return value


__all__ = ["CoreRunPersistence", "LocalDocumentSnapshotStore"]
