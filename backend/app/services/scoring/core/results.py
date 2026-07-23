"""Immutable, transport-neutral result objects produced by the scoring Core."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
import re

from backend.app.services.scoring.core.contracts import (
    _ImmutableContract,
    _assert_closed_mapping,
    _assert_json_value,
    _assert_optional_decimal,
    _assert_optional_text,
    _assert_sha256,
    _assert_text,
    _assert_mapping_array,
    _normalize_runtime_identity,
)


_RULE_DECISION_STATUSES = frozenset(
    {"triggered", "not_triggered", "not_applicable", "invalid", "skipped"}
)
_CRITERION_STATUSES = frozenset({"calculated", "invalid", "blocked"})
_CRITERION_STATUSES_V2 = _CRITERION_STATUSES | {"review_required"}
_OUTCOME_STATUSES = frozenset({"completed", "review_required", "blocked"})
_REVIEW_SEVERITIES = frozenset({"info", "warning", "review", "block", "error"})
_CONTRIBUTION_KINDS = frozenset(
    {"base", "band", "deduction", "weight", "override"}
)
_LOCATOR_KINDS = frozenset(
    {"text_span", "section", "document_structure", "page_region", "metadata"}
)


def _assert_enum(value, path, allowed):
    normalized = _assert_text(value, path)
    if normalized not in allowed:
        raise ValueError("%s is unsupported: %s" % (path, normalized))
    return normalized


def _required_decimal(value, path):
    normalized = _assert_optional_decimal(value, path)
    if normalized is None:
        raise ValueError("%s must not be null" % path)
    return normalized


def _assert_score_in_range(value, path, *, max_score):
    normalized = _assert_optional_decimal(value, path)
    if normalized is None:
        return None
    score = Decimal(normalized)
    if score < 0 or score > Decimal(max_score):
        raise ValueError("%s must be between 0 and max_score" % path)
    return normalized


def _assert_nonnegative_optional_decimal(value, path):
    normalized = _assert_optional_decimal(value, path)
    if normalized is not None and Decimal(normalized) < 0:
        raise ValueError("%s cannot be negative" % path)
    return normalized


def _assert_nonnegative_integer(value, path):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("%s must be an integer" % path)
    if value < 0:
        raise ValueError("%s cannot be negative" % path)
    return value


def _normalize_locator(value, path, *, evidence_unit_id):
    if not isinstance(value, Mapping):
        raise TypeError("%s must be an object" % path)
    if "kind" not in value:
        raise ValueError("%s is missing fields: ['kind']" % path)
    kind = _assert_enum(value["kind"], path + ".kind", _LOCATOR_KINDS)

    if kind == "text_span":
        fields = {"kind", "evidence_unit_id", "start", "end"}
        _assert_closed_mapping(value, fields=fields, path=path)
        locator_evidence_id = _assert_sha256(
            value["evidence_unit_id"], path + ".evidence_unit_id"
        )
        if evidence_unit_id is None or evidence_unit_id != locator_evidence_id:
            raise ValueError("%s evidence_unit_id does not match EvidenceRef" % path)
        start = _assert_nonnegative_integer(value["start"], path + ".start")
        end = _assert_nonnegative_integer(value["end"], path + ".end")
        if end <= start:
            raise ValueError("%s text span must be a non-empty half-open range" % path)
        return {
            "kind": kind,
            "evidence_unit_id": locator_evidence_id,
            "start": start,
            "end": end,
        }

    if kind == "section":
        fields = {"kind", "section_path", "section_ordinal"}
        _assert_closed_mapping(value, fields=fields, path=path)
        raw_path = value["section_path"]
        if not isinstance(raw_path, (list, tuple)) or not raw_path:
            raise ValueError("%s.section_path must be a non-empty array" % path)
        section_path = [
            _assert_text(item, "%s.section_path[%s]" % (path, index))
            for index, item in enumerate(raw_path)
        ]
        return {
            "kind": kind,
            "section_path": section_path,
            "section_ordinal": _assert_nonnegative_integer(
                value["section_ordinal"], path + ".section_ordinal"
            ),
        }

    if kind == "document_structure":
        fields = {"kind", "structure_code", "ordinal"}
        _assert_closed_mapping(value, fields=fields, path=path)
        return {
            "kind": kind,
            "structure_code": _assert_text(
                value["structure_code"], path + ".structure_code"
            ),
            "ordinal": _assert_nonnegative_integer(
                value["ordinal"], path + ".ordinal"
            ),
        }

    if kind == "page_region":
        fields = {"kind", "page_index", "bbox"}
        _assert_closed_mapping(value, fields=fields, path=path)
        raw_bbox = value["bbox"]
        if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
            raise ValueError("%s.bbox must contain x0, y0, x1, y1" % path)
        bbox = [
            _required_decimal(item, "%s.bbox[%s]" % (path, index))
            for index, item in enumerate(raw_bbox)
        ]
        coordinates = [Decimal(item) for item in bbox]
        if any(item < 0 or item > 1 for item in coordinates):
            raise ValueError("%s.bbox coordinates must be between 0 and 1" % path)
        x0, y0, x1, y1 = coordinates
        if x0 >= x1 or y0 >= y1:
            raise ValueError("%s.bbox must satisfy x0 < x1 and y0 < y1" % path)
        return {
            "kind": kind,
            "page_index": _assert_nonnegative_integer(
                value["page_index"], path + ".page_index"
            ),
            "bbox": bbox,
        }

    fields = {"kind", "field_path"}
    _assert_closed_mapping(value, fields=fields, path=path)
    field_path = _assert_text(value["field_path"], path + ".field_path")
    if not field_path.startswith("/") or re.search(r"~(?:[^01]|$)", field_path):
        raise ValueError("%s.field_path must be an RFC 6901 JSON Pointer" % path)
    return {"kind": kind, "field_path": field_path}


def _normalize_request_identity(value, path):
    fields = {
        "idempotency_key",
        "document_snapshot_hash",
        "rubric_snapshot_hash",
        "plan_hash",
        "policy_hash",
        "profile_key",
    }
    _assert_closed_mapping(value, fields=fields, path=path)
    return {
        "idempotency_key": _assert_sha256(value["idempotency_key"], path + ".idempotency_key"),
        "document_snapshot_hash": _assert_sha256(value["document_snapshot_hash"], path + ".document_snapshot_hash"),
        "rubric_snapshot_hash": _assert_sha256(value["rubric_snapshot_hash"], path + ".rubric_snapshot_hash"),
        "plan_hash": _assert_sha256(value["plan_hash"], path + ".plan_hash"),
        "policy_hash": _assert_sha256(value["policy_hash"], path + ".policy_hash"),
        "profile_key": _assert_text(value["profile_key"], path + ".profile_key"),
    }


def _normalize_criterion_outcome(value, path, *, v2=False):
    fields = {"criterion_code", "status", "auto_score", "final_score", "max_score"}
    _assert_closed_mapping(value, fields=fields, path=path)
    status = _assert_enum(
        value["status"],
        path + ".status",
        _CRITERION_STATUSES_V2 if v2 else _CRITERION_STATUSES,
    )
    max_score = _required_decimal(value["max_score"], path + ".max_score")
    if Decimal(max_score) <= 0:
        raise ValueError("%s.max_score must be positive" % path)
    auto_score = _assert_score_in_range(
        value["auto_score"], path + ".auto_score", max_score=max_score
    )
    final_score = _assert_score_in_range(
        value["final_score"], path + ".final_score", max_score=max_score
    )
    if status in {"invalid", "blocked"} and (
        auto_score is not None or final_score is not None
    ):
        raise ValueError("%s criterion must keep auto_score and final_score null" % status)
    if status == "review_required" and final_score is not None:
        raise ValueError("review_required criterion must keep final_score null")
    return {
        "criterion_code": _assert_text(value["criterion_code"], path + ".criterion_code"),
        "status": status,
        "auto_score": auto_score,
        "final_score": final_score,
        "max_score": max_score,
    }


def _normalize_evidence_ref(value, path):
    fields = {"evidence_unit_id", "evidence_type", "locator", "payload_hash"}
    _assert_closed_mapping(value, fields=fields, path=path)
    evidence_unit_id = _assert_optional_text(
        value["evidence_unit_id"], path + ".evidence_unit_id"
    )
    if evidence_unit_id is not None:
        evidence_unit_id = _assert_sha256(
            evidence_unit_id, path + ".evidence_unit_id"
        )
    return {
        "evidence_unit_id": evidence_unit_id,
        "evidence_type": _assert_text(value["evidence_type"], path + ".evidence_type"),
        "locator": _normalize_locator(
            value["locator"],
            path + ".locator",
            evidence_unit_id=evidence_unit_id,
        ),
        "payload_hash": _assert_sha256(value["payload_hash"], path + ".payload_hash"),
    }


def _normalize_rule_decision(value, path):
    fields = {"rule_code", "status", "evidence_refs"}
    _assert_closed_mapping(value, fields=fields, path=path)
    return {
        "rule_code": _assert_text(value["rule_code"], path + ".rule_code"),
        "status": _assert_enum(
            value["status"], path + ".status", _RULE_DECISION_STATUSES
        ),
        "evidence_refs": _assert_mapping_array(
            value["evidence_refs"], path + ".evidence_refs", _normalize_evidence_ref
        ),
    }


def _normalize_occurrence(value, path):
    fields = {
        "occurrence_id",
        "occurrence_payload",
        "evidence_refs",
        "calculated_effect",
    }
    _assert_closed_mapping(value, fields=fields, path=path)
    payload = value["occurrence_payload"]
    if not isinstance(payload, Mapping):
        raise TypeError(path + ".occurrence_payload must be an object")
    normalized_payload = _assert_json_value(payload, path + ".occurrence_payload")
    return {
        "occurrence_id": _assert_sha256(
            value["occurrence_id"], path + ".occurrence_id"
        ),
        "occurrence_payload": normalized_payload,
        "evidence_refs": _assert_mapping_array(
            value["evidence_refs"], path + ".evidence_refs", _normalize_evidence_ref
        ),
        "calculated_effect": _assert_optional_decimal(
            value["calculated_effect"], path + ".calculated_effect"
        ),
    }


def _normalize_rule_decision_v2(value, path):
    fields = {
        "version_hash",
        "rule_code",
        "criterion_code",
        "direction",
        "effect_type",
        "status",
        "selected_level_code",
        "evidence_refs",
        "occurrences",
        "calculated_effect",
    }
    _assert_closed_mapping(value, fields=fields, path=path)
    return {
        "version_hash": _assert_sha256(value["version_hash"], path + ".version_hash"),
        "rule_code": _assert_text(value["rule_code"], path + ".rule_code"),
        "criterion_code": _assert_text(
            value["criterion_code"], path + ".criterion_code"
        ),
        "direction": _assert_text(value["direction"], path + ".direction"),
        "effect_type": _assert_text(value["effect_type"], path + ".effect_type"),
        "status": _assert_enum(
            value["status"], path + ".status", _RULE_DECISION_STATUSES
        ),
        "selected_level_code": _assert_optional_text(
            value["selected_level_code"], path + ".selected_level_code"
        ),
        "evidence_refs": _assert_mapping_array(
            value["evidence_refs"], path + ".evidence_refs", _normalize_evidence_ref
        ),
        "occurrences": _assert_mapping_array(
            value["occurrences"], path + ".occurrences", _normalize_occurrence
        ),
        "calculated_effect": _assert_optional_decimal(
            value["calculated_effect"], path + ".calculated_effect"
        ),
    }


def _normalize_score_contribution(value, path):
    fields = {"criterion_code", "rule_code", "kind", "amount"}
    _assert_closed_mapping(value, fields=fields, path=path)
    return {
        "criterion_code": _assert_text(value["criterion_code"], path + ".criterion_code"),
        "rule_code": _assert_optional_text(value["rule_code"], path + ".rule_code"),
        "kind": _assert_enum(value["kind"], path + ".kind", _CONTRIBUTION_KINDS),
        "amount": _assert_optional_decimal(value["amount"], path + ".amount"),
    }


def _normalize_score_contribution_v2(value, path):
    fields = {"criterion_code", "rule_code", "occurrence_id", "kind", "amount"}
    _assert_closed_mapping(value, fields=fields, path=path)
    occurrence_id = _assert_optional_text(
        value["occurrence_id"], path + ".occurrence_id"
    )
    if occurrence_id is not None:
        occurrence_id = _assert_sha256(occurrence_id, path + ".occurrence_id")
    return {
        "criterion_code": _assert_text(
            value["criterion_code"], path + ".criterion_code"
        ),
        "rule_code": _assert_optional_text(value["rule_code"], path + ".rule_code"),
        "occurrence_id": occurrence_id,
        "kind": _assert_enum(value["kind"], path + ".kind", _CONTRIBUTION_KINDS),
        "amount": _assert_optional_decimal(value["amount"], path + ".amount"),
    }


def _normalize_review_issue(value, path):
    required = {"code", "severity", "criterion_code"}
    fields = required | {"rule_code", "message"}
    _assert_closed_mapping(value, fields=fields, required=required, path=path)
    normalized = {
        "code": _assert_text(value["code"], path + ".code"),
        "severity": _assert_enum(
            value["severity"], path + ".severity", _REVIEW_SEVERITIES
        ),
        "criterion_code": _assert_optional_text(value["criterion_code"], path + ".criterion_code"),
    }
    if "rule_code" in value:
        normalized["rule_code"] = _assert_optional_text(value["rule_code"], path + ".rule_code")
    if "message" in value:
        normalized["message"] = _assert_text(value["message"], path + ".message", allow_empty=True)
    return normalized


def _normalize_scoring_outcome(value):
    fields = {
        "schema_version",
        "request_identity",
        "criterion_outcomes",
        "rule_decisions",
        "score_contributions",
        "review_issues",
        "unrounded_total",
        "final_total",
        "grade",
        "status",
        "audit_identity",
    }
    _assert_closed_mapping(value, fields=fields, path="ScoringOutcome")
    schema_version = value["schema_version"]
    if schema_version not in {"scoring-outcome@1", "scoring-outcome@2"}:
        raise ValueError("unsupported ScoringOutcome schema_version")
    v2 = schema_version == "scoring-outcome@2"
    status = _assert_enum(value["status"], "ScoringOutcome.status", _OUTCOME_STATUSES)
    unrounded_total = _assert_nonnegative_optional_decimal(
        value["unrounded_total"], "ScoringOutcome.unrounded_total"
    )
    final_total = _assert_nonnegative_optional_decimal(
        value["final_total"], "ScoringOutcome.final_total"
    )
    grade = _assert_optional_text(value["grade"], "ScoringOutcome.grade")
    request_identity = _normalize_request_identity(
        value["request_identity"], "ScoringOutcome.request_identity"
    )
    audit_identity = _normalize_runtime_identity(
        value["audit_identity"], "ScoringOutcome.audit_identity"
    )
    if request_identity["profile_key"] != audit_identity["profile_key"]:
        raise ValueError("ScoringOutcome profile identities do not match")
    criterion_outcomes = _assert_mapping_array(
        value["criterion_outcomes"],
        "ScoringOutcome.criterion_outcomes",
        lambda item, path: _normalize_criterion_outcome(item, path, v2=v2),
    )
    rule_decisions = _assert_mapping_array(
        value["rule_decisions"],
        "ScoringOutcome.rule_decisions",
        _normalize_rule_decision_v2 if v2 else _normalize_rule_decision,
    )
    score_contributions = _assert_mapping_array(
        value["score_contributions"],
        "ScoringOutcome.score_contributions",
        _normalize_score_contribution_v2 if v2 else _normalize_score_contribution,
    )
    review_issues = _assert_mapping_array(
        value["review_issues"],
        "ScoringOutcome.review_issues",
        _normalize_review_issue,
    )

    if status == "blocked":
        if unrounded_total is not None or final_total is not None or grade is not None:
            raise ValueError("blocked ScoringOutcome must keep totals and grade null")
        # Valid decisions and contributions from unaffected criteria remain
        # immutable audit facts even when another criterion blocks the run.
        # Only the aggregate total/grade is prohibited.
    if status == "completed" and (
        unrounded_total is None or final_total is None
    ):
        raise ValueError("completed ScoringOutcome requires both totals")
    if v2 and status == "review_required" and (
        unrounded_total is not None or final_total is not None or grade is not None
    ):
        raise ValueError("review_required ScoringOutcome must keep totals and grade null")
    if any(
        item["status"] in {"invalid", "blocked"}
        for item in criterion_outcomes
    ) and status != "blocked":
        raise ValueError("invalid or blocked criterion requires blocked ScoringOutcome")

    return {
        "schema_version": schema_version,
        "request_identity": request_identity,
        "criterion_outcomes": criterion_outcomes,
        "rule_decisions": rule_decisions,
        "score_contributions": score_contributions,
        "review_issues": review_issues,
        "unrounded_total": unrounded_total,
        "final_total": final_total,
        "grade": grade,
        "status": status,
        "audit_identity": audit_identity,
    }


def _normalize_rule_execution_result(value):
    fields = {
        "schema_version",
        "status",
        "criterion_outcomes",
        "rule_decisions",
        "score_contributions",
        "review_issues",
    }
    _assert_closed_mapping(value, fields=fields, path="RuleExecutionResult")
    if value["schema_version"] != "rule-execution-result@1":
        raise ValueError("unsupported RuleExecutionResult schema_version")
    status = _assert_enum(
        value["status"], "RuleExecutionResult.status", _OUTCOME_STATUSES
    )
    criteria = _assert_mapping_array(
        value["criterion_outcomes"],
        "RuleExecutionResult.criterion_outcomes",
        lambda item, path: _normalize_criterion_outcome(item, path, v2=True),
    )
    decisions = _assert_mapping_array(
        value["rule_decisions"],
        "RuleExecutionResult.rule_decisions",
        _normalize_rule_decision_v2,
    )
    contributions = _assert_mapping_array(
        value["score_contributions"],
        "RuleExecutionResult.score_contributions",
        _normalize_score_contribution_v2,
    )
    issues = _assert_mapping_array(
        value["review_issues"],
        "RuleExecutionResult.review_issues",
        _normalize_review_issue,
    )
    if any(item["status"] in {"invalid", "blocked"} for item in criteria):
        if status != "blocked":
            raise ValueError("invalid or blocked criterion requires blocked result")
    if any(item["status"] == "review_required" for item in criteria):
        if status == "completed":
            raise ValueError("review_required criterion requires review result")
    return {
        "schema_version": "rule-execution-result@1",
        "status": status,
        "criterion_outcomes": criteria,
        "rule_decisions": decisions,
        "score_contributions": contributions,
        "review_issues": issues,
    }


class ScoringOutcome(_ImmutableContract):
    """Complete replay/audit result without transport or thesis-specific fields."""

    __slots__ = ()
    _normalizer = staticmethod(_normalize_scoring_outcome)


class RuleExecutionResult(_ImmutableContract):
    """Complete M4 rule audit before policy aggregation/persistence."""

    __slots__ = ()
    _normalizer = staticmethod(_normalize_rule_execution_result)


__all__ = ["RuleExecutionResult", "ScoringOutcome"]
