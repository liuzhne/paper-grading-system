"""Shared, internally consistent M2 contract fixtures.

The factories mirror ADR-0001 D09 projections without importing ORM models or
opening an artifact.  All M2 test modules consume the same Snapshot and checker
manifest field names so the executable specification cannot drift by file.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib

from backend.app.services.scoring.core.canonical import canonical_sha256


PROFILE_KEY = "technical_proposal"
PROFILE_VERSION = "technical-proposal-test-profile@1"
PARSER_VERSION = "technical-proposal-parser@1"
NORMALIZER_VERSION = "technical-proposal-normalizer@1"


def technical_submission_payload(*, metadata=None) -> dict:
    artifact_hash = "a" * 64
    return {
        "schema_version": "submission-snapshot@1",
        "submission_id": "proposal-2026-001",
        "profile_key": PROFILE_KEY,
        "source_artifact_hash": artifact_hash,
        "metadata": dict(
            metadata
            or {
                "proposal_id": "TP-2026-001",
                "vendor_name": "Acme Solutions",
                "project_name": "智能排产平台",
                "uploader_email": "operator@example.invalid",
                "internal_tracking_tag": "north-region",
                "student_id": "external-stakeholder-reference",
            }
        ),
        "artifact_refs": [
            {
                "kind": "source",
                "ref": "blob:sha256:" + artifact_hash,
                "content_hash": artifact_hash,
            }
        ],
    }


def technical_normalized_content_input() -> dict:
    return {
        "normalizer_version": NORMALIZER_VERSION,
        "sections": [
            {
                "section_path": ["需求理解"],
                "heading": "需求理解",
                "normalized_text": "本方案覆盖吞吐、时延与可用性目标。",
                "units": [
                    {"normalized_text": "本方案覆盖吞吐、时延与可用性目标。"}
                ],
            },
            {
                "section_path": ["总体方案", "风险控制"],
                "heading": "风险控制",
                "normalized_text": "风险负责人为项目经理。\n升级窗口为十五分钟。",
                "units": [
                    {"normalized_text": "风险负责人为项目经理。"},
                    {"normalized_text": "升级窗口为十五分钟。"},
                ],
            },
        ],
    }


def normalized_content_projection(value: dict) -> dict:
    return {
        "scheme": "normalized-content-v1",
        "normalizer_version": value["normalizer_version"],
        "sections": [
            {
                "section_path": section["section_path"],
                "heading": section["heading"],
                "normalized_text": section["normalized_text"],
                "units": [
                    {"normalized_text": unit["normalized_text"]}
                    for unit in section["units"]
                ],
            }
            for section in value["sections"]
        ],
    }


def evidence_unit_projection(value: dict) -> dict:
    return {
        "scheme": "evidence-unit-id-v1",
        "normalized_content_hash": value["normalized_content_hash"],
        "section_path": value["section_path"],
        "section_ordinal": value["section_ordinal"],
        "unit_ordinal": value["unit_ordinal"],
        "unit_text_hash": hashlib.sha256(
            value["normalized_text"].encode("utf-8")
        ).hexdigest(),
    }


def document_snapshot_projection(value: dict) -> dict:
    return {
        "scheme": "document-snapshot-v1",
        "schema_version": value["schema_version"],
        "business_profile_key": value["profile_key"],
        "business_profile_version": value["profile_version"],
        "parser_version": value["parser_version"],
        "normalizer_version": value["normalizer_version"],
        "content_hash": value["content_hash"],
        "sections": [
            {
                "section_path": section["section_path"],
                "section_ordinal": section["section_ordinal"],
                "heading": section["heading"],
                "evidence_unit_ids": section["evidence_unit_ids"],
                "locator": section["locator"],
            }
            for section in value["sections"]
        ],
        "evidence_units": [
            {
                "evidence_unit_id": unit["evidence_unit_id"],
                "section_path": unit["section_path"],
                "section_ordinal": unit["section_ordinal"],
                "unit_ordinal": unit["unit_ordinal"],
                "locator": unit["locator"],
            }
            for unit in value["evidence_units"]
        ],
        "metrics": value["metrics"],
        "format_facts": value["format_facts"],
        "parse_quality": value["parse_quality"],
        "profile_extensions": value["profile_extensions"],
        "diagnostics": [
            {
                "code": diagnostic["code"],
                "severity": diagnostic["severity"],
                "message": diagnostic["message"],
            }
            for diagnostic in value["parser_diagnostics"]
            if diagnostic["scoring_relevant"]
        ],
    }


def technical_document_payload(*, profile_version: str = PROFILE_VERSION) -> dict:
    normalized = technical_normalized_content_input()
    content_hash = canonical_sha256(normalized_content_projection(normalized))

    unit_records = []
    for section_ordinal, section in enumerate(normalized["sections"]):
        for unit_ordinal, unit in enumerate(section["units"]):
            identity_input = {
                "normalized_content_hash": content_hash,
                "section_path": deepcopy(section["section_path"]),
                "section_ordinal": section_ordinal,
                "unit_ordinal": unit_ordinal,
                "normalized_text": unit["normalized_text"],
            }
            unit_records.append(
                {
                    **identity_input,
                    "evidence_unit_id": canonical_sha256(
                        evidence_unit_projection(identity_input)
                    ),
                }
            )

    sections = []
    for section_ordinal, section in enumerate(normalized["sections"]):
        sections.append(
            {
                "section_path": deepcopy(section["section_path"]),
                "section_ordinal": section_ordinal,
                "heading": section["heading"],
                "normalized_text": section["normalized_text"],
                "evidence_unit_ids": [
                    unit["evidence_unit_id"]
                    for unit in unit_records
                    if unit["section_ordinal"] == section_ordinal
                ],
                "locator": {
                    "kind": "section",
                    "section_path": deepcopy(section["section_path"]),
                    "section_ordinal": section_ordinal,
                },
            }
        )

    evidence_units = [
        {
            **deepcopy(unit),
            "unit_text_hash": hashlib.sha256(
                unit["normalized_text"].encode("utf-8")
            ).hexdigest(),
            "locator": {
                "kind": "text_span",
                "evidence_unit_id": unit["evidence_unit_id"],
                "start": 0,
                "end": len(unit["normalized_text"]),
            },
        }
        for unit in unit_records
    ]

    payload = {
        "schema_version": "document-snapshot@1",
        "profile_key": PROFILE_KEY,
        "profile_version": profile_version,
        "parser_version": PARSER_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "content_hash": content_hash,
        "full_text": "\n".join(
            section["normalized_text"] for section in normalized["sections"]
        ),
        "sections": sections,
        "evidence_units": evidence_units,
        "metrics": {"word_count": 28, "risk_item_count": 4},
        "format_facts": {"page_size": "A4", "heading_levels": 2},
        "parse_quality": "0.98",
        "parser_diagnostics": [
            {
                "code": "TABLE_TEXT_PARTIAL",
                "severity": "warning",
                "message": "one risk table cell required fallback extraction",
                "scoring_relevant": True,
            },
            {
                "code": "PAGE_HINT",
                "severity": "info",
                "message": "page number inferred",
                "scoring_relevant": False,
            },
        ],
        "profile_extensions": {
            PROFILE_KEY: {
                "risk_owner_required": True,
                "service_window_minutes": 15,
            }
        },
    }
    payload["document_snapshot_hash"] = canonical_sha256(
        document_snapshot_projection(payload)
    )
    return payload


def checker_manifest_payload(*, version: str = "1.0.0") -> dict:
    return {
        "technical_proposal.required_sections.v1": {
            "checker_version": version,
            "implementation_hash": "c" * 64,
            "params_schema": "required-sections-params@1",
            "supported_document_schemas": ["document-snapshot@1"],
            "supported_profiles": [PROFILE_KEY],
            "observation_schema": "deterministic-observation@1",
        }
    }


def technical_policy_snapshot_payload(
    *,
    total_score: str = "100",
    rounding_digits: int = 2,
) -> dict:
    payload = {
        "schema_version": "scoring-policy@1",
        "policy_key": "technical_proposal_contract_policy",
        "usage": "authoritative_new_runs",
        "aggregation": {"mode": "points", "total_score": total_score},
        "rounding": {"mode": "half_up", "digits": rounding_digits},
        "grade_scale": {
            "basis": "percentage",
            "bands": [
                {"label": "A", "minimum": "90"},
                {"label": "B", "minimum": "75"},
                {"label": "C", "minimum": "60"},
                {"label": "D", "minimum": "0"},
            ],
        },
        "review": {
            "total_below": "60",
            "grade_boundary_tolerance": {
                "value": "2",
                "unit": "percentage_points",
            },
            "confidence_below": "0.7",
            "parse_quality_below": "0.7",
            "on_invalid_evidence": "required_only",
        },
        "evidence": {
            "schema_version": "evidence-policy-v1",
            "default_policy": "required",
            "requirement": "required",
            "minimum_valid_items": 1,
            "allowed_types": [
                "source_quote",
                "deterministic_observation",
                "scoped_absence",
            ],
            "review_on_optional_invalid": False,
            "absence": {
                "enabled": True,
                "policy_version": "proposal-absence-v1",
                "allowed_targets": ["risk_owner", "mitigation"],
                "complete_scope_selectors": ["document://all-units"],
            },
        },
        "failure": {
            "unknown_checker": "block",
            "rule_conflict": "block",
            "semantic_error": "review",
        },
        "legacy": {
            "allow_legacy_direct_compat": False,
            "force_review": False,
        },
    }
    payload["policy_hash"] = canonical_sha256(payload)
    return payload


def runtime_identity_payload() -> dict:
    return {
        "engine_contract_version": "scoring-core@1",
        "engine_version": "core-contract-fixture@1",
        "profile_key": PROFILE_KEY,
        "profile_version": PROFILE_VERSION,
        "prompt_version": "technical-proposal-prompt@1",
        "provider": {
            "name": "mock",
            "model": "m2-contract-fixture",
            "model_version": "1.0.0",
            "sampling": {
                "temperature": "0",
                "top_p": "1",
                "seed": 20260719,
                "max_tokens": 512,
            },
            "thinking": {"enabled": False, "type": None},
            "response_format": "json_schema",
            "response_schema": "atomic-rule-decisions@1",
            "artifact_hash": "8" * 64,
        },
        "calibration_anchors_hash": "9" * 64,
    }


__all__ = [
    "NORMALIZER_VERSION",
    "PARSER_VERSION",
    "PROFILE_KEY",
    "PROFILE_VERSION",
    "checker_manifest_payload",
    "document_snapshot_projection",
    "evidence_unit_projection",
    "normalized_content_projection",
    "runtime_identity_payload",
    "technical_document_payload",
    "technical_normalized_content_input",
    "technical_policy_snapshot_payload",
    "technical_submission_payload",
]
