"""Regression tests for fail-closed M2 production contracts.

These cases cover identity and semantic invariants that are stronger than the
initial capability-oriented M2 executable specification.  They remain pure
Core tests: no database, filesystem, clock, or provider is involved.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib

import pytest

from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.contracts import (
    CompiledRubricSnapshot,
    DocumentSnapshot,
    SubmissionSnapshot,
)
from backend.app.services.scoring.core.identity import (
    derive_evidence_unit_id,
    hash_document_snapshot,
    hash_normalized_content,
)
from backend.app.services.scoring.core.results import ScoringOutcome
from backend.app.tests.m2_contract_fixtures import (
    document_snapshot_projection,
    evidence_unit_projection,
    technical_document_payload,
    technical_normalized_content_input,
    technical_submission_payload,
)
from backend.app.tests.test_m2_core_dtos import (
    _compiled_rubric_payload,
    _outcome_payload,
)


def _refresh_policy_hash(policy: dict) -> None:
    policy.pop("policy_hash", None)
    policy["policy_hash"] = canonical_sha256(policy)


def _refresh_document_hash(document: dict) -> None:
    document["document_snapshot_hash"] = canonical_sha256(
        document_snapshot_projection(document)
    )


def test_document_snapshot_rejects_full_text_not_derived_from_sections():
    payload = technical_document_payload()
    payload["full_text"] += "\n未绑定到 section 的评分文本"

    with pytest.raises(ValueError, match=r"(?i)(full_text|content|section)"):
        DocumentSnapshot.from_mapping(payload)


def test_document_snapshot_rejects_orphan_evidence_unit():
    payload = technical_document_payload()
    identity_input = {
        "normalized_content_hash": payload["content_hash"],
        "section_path": ["孤立章节"],
        "section_ordinal": len(payload["sections"]),
        "unit_ordinal": 0,
        "normalized_text": "该证据没有对应的 section。",
    }
    evidence_unit_id = canonical_sha256(evidence_unit_projection(identity_input))
    payload["evidence_units"].append(
        {
            **identity_input,
            "evidence_unit_id": evidence_unit_id,
            "unit_text_hash": hashlib.sha256(
                identity_input["normalized_text"].encode("utf-8")
            ).hexdigest(),
            "locator": {
                "kind": "text_span",
                "evidence_unit_id": evidence_unit_id,
                "start": 0,
                "end": len(identity_input["normalized_text"]),
            },
        }
    )
    _refresh_document_hash(payload)

    with pytest.raises(ValueError, match=r"(?i)(orphan|section|evidence)"):
        DocumentSnapshot.from_mapping(payload)


def test_document_snapshot_rejects_noncanonical_evidence_unit_order():
    payload = technical_document_payload()
    payload["evidence_units"] = list(reversed(payload["evidence_units"]))
    _refresh_document_hash(payload)

    with pytest.raises(ValueError, match=r"(?i)(order|ordinal|evidence)"):
        DocumentSnapshot.from_mapping(payload)


def test_compiled_rubric_rejects_hash_consistent_but_semantically_invalid_policy():
    payload = _compiled_rubric_payload()
    payload["global_policy"]["aggregation"]["mode"] = "nonsense"
    _refresh_policy_hash(payload["global_policy"])

    with pytest.raises(ValueError, match=r"(?i)(aggregation|policy|unsupported)"):
        CompiledRubricSnapshot.from_mapping(payload)


def test_compiled_rubric_rejects_policy_total_that_differs_from_rubric_total():
    payload = _compiled_rubric_payload()
    payload["global_policy"]["aggregation"]["total_score"] = "19"
    _refresh_policy_hash(payload["global_policy"])

    with pytest.raises(ValueError, match=r"(?i)(policy|total_score|rubric)"):
        CompiledRubricSnapshot.from_mapping(payload)


@pytest.mark.parametrize(
    "ref",
    (
        "/tmp/private/proposal.docx",
        "file:///tmp/private/proposal.docx",
        r"C:\\private\\proposal.docx",
        "relative/private/proposal.docx",
    ),
)
def test_submission_snapshot_rejects_local_or_scheme_less_artifact_paths(ref):
    payload = technical_submission_payload()
    payload["artifact_refs"][0]["ref"] = ref

    with pytest.raises(ValueError, match=r"(?i)(artifact|ref|scheme|path|file)"):
        SubmissionSnapshot.from_mapping(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("status", "nonsense"),
        ("criterion_status", "nonsense"),
        ("rule_status", "nonsense"),
        ("severity", "nonsense"),
    ),
)
def test_scoring_outcome_rejects_unknown_state_machine_values(field, value):
    payload = _outcome_payload()
    if field == "status":
        payload["status"] = value
    elif field == "criterion_status":
        payload["criterion_outcomes"][0]["status"] = value
    elif field == "rule_status":
        payload["rule_decisions"][0]["status"] = value
    else:
        payload["review_issues"][0]["severity"] = value

    with pytest.raises(ValueError, match=r"(?i)(status|severity|unsupported)"):
        ScoringOutcome.from_mapping(payload)


@pytest.mark.parametrize("mutation", ("grade", "criterion-score"))
def test_blocked_outcome_cannot_carry_authoritative_score_effects(mutation):
    payload = _outcome_payload()
    if mutation == "grade":
        payload["grade"] = "A"
    else:
        payload["criterion_outcomes"][0]["auto_score"] = "20"

    with pytest.raises(ValueError, match=r"(?i)(blocked|score|grade|null)"):
        ScoringOutcome.from_mapping(payload)


def test_blocked_outcome_preserves_valid_partial_audit_contributions():
    payload = _outcome_payload()
    payload["criterion_outcomes"].insert(
        0,
        {
            "criterion_code": "SOLUTION_QUALITY",
            "status": "calculated",
            "auto_score": "10",
            "final_score": "10",
            "max_score": "10",
        },
    )
    payload["score_contributions"] = [
        {
            "criterion_code": "SOLUTION_QUALITY",
            "rule_code": None,
            "kind": "base",
            "amount": "10",
        }
    ]

    outcome = ScoringOutcome.from_mapping(payload)

    assert outcome.final_total is None
    assert outcome.score_contributions[0]["amount"] == "10"


def test_section_path_uses_nfc_trim_normalization_for_identity():
    baseline = {
        "normalized_content_hash": "a" * 64,
        "section_path": ["风险控制"],
        "section_ordinal": 0,
        "unit_ordinal": 0,
        "normalized_text": "负责人为项目经理。",
    }
    equivalent = deepcopy(baseline)
    equivalent["section_path"] = ["  风险控制  "]

    assert derive_evidence_unit_id(equivalent) == derive_evidence_unit_id(baseline)


@pytest.mark.parametrize("mutation", ("empty-path", "empty-segment", "empty-unit"))
def test_evidence_unit_identity_rejects_noncanonical_empty_structure(mutation):
    payload = {
        "normalized_content_hash": "a" * 64,
        "section_path": ["风险控制"],
        "section_ordinal": 0,
        "unit_ordinal": 0,
        "normalized_text": "负责人为项目经理。",
    }
    if mutation == "empty-path":
        payload["section_path"] = []
    elif mutation == "empty-segment":
        payload["section_path"] = ["   "]
    else:
        payload["normalized_text"] = ""

    with pytest.raises((TypeError, ValueError)):
        derive_evidence_unit_id(payload)


def test_normalized_content_rejects_empty_evidence_unit_after_normalization():
    payload = technical_normalized_content_input()
    payload["sections"][0]["units"][0]["normalized_text"] = ""

    with pytest.raises((TypeError, ValueError), match=r"(?i)(unit|empty|normalized)"):
        hash_normalized_content(payload)


@pytest.mark.parametrize(
    "mutation",
    ("section-kind", "span-kind", "empty-span", "span-bounds", "full-text"),
)
def test_public_document_identity_api_rejects_invalid_replay_structure(mutation):
    payload = technical_document_payload()
    payload.pop("document_snapshot_hash")
    if mutation == "section-kind":
        payload["sections"][0]["locator"]["kind"] = "page_region"
    elif mutation == "span-kind":
        payload["evidence_units"][0]["locator"]["kind"] = "section"
    elif mutation == "empty-span":
        payload["evidence_units"][0]["locator"]["end"] = 0
    elif mutation == "span-bounds":
        payload["evidence_units"][0]["locator"]["end"] = 999999
    else:
        payload["full_text"] += "\n身份之外的正文"

    with pytest.raises((TypeError, ValueError)):
        hash_document_snapshot(payload)
