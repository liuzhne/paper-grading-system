"""Self-tests for the shared M2 executable contract fixtures."""

from __future__ import annotations

import hashlib

from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.tests.m2_contract_fixtures import (
    NORMALIZER_VERSION,
    PROFILE_KEY,
    PROFILE_VERSION,
    checker_manifest_payload,
    document_snapshot_projection,
    evidence_unit_projection,
    normalized_content_projection,
    runtime_identity_payload,
    technical_document_payload,
    technical_normalized_content_input,
    technical_policy_snapshot_payload,
    technical_submission_payload,
)


def _all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_keys(item)


def test_shared_document_fixture_has_valid_content_and_snapshot_hashes():
    normalized = technical_normalized_content_input()
    document = technical_document_payload()

    assert normalized["normalizer_version"] == NORMALIZER_VERSION
    assert document["content_hash"] == canonical_sha256(
        normalized_content_projection(normalized)
    )
    assert document["document_snapshot_hash"] == canonical_sha256(
        document_snapshot_projection(document)
    )
    assert document["profile_key"] == PROFILE_KEY
    assert document["profile_version"] == PROFILE_VERSION


def test_shared_evidence_units_are_contiguous_content_addressed_and_in_bounds():
    document = technical_document_payload()
    units_by_section = {}
    for unit in document["evidence_units"]:
        units_by_section.setdefault(unit["section_ordinal"], []).append(unit)
        identity_input = {
            "normalized_content_hash": document["content_hash"],
            "section_path": unit["section_path"],
            "section_ordinal": unit["section_ordinal"],
            "unit_ordinal": unit["unit_ordinal"],
            "normalized_text": unit["normalized_text"],
        }
        assert unit["evidence_unit_id"] == canonical_sha256(
            evidence_unit_projection(identity_input)
        )
        assert unit["unit_text_hash"] == hashlib.sha256(
            unit["normalized_text"].encode("utf-8")
        ).hexdigest()
        assert set(unit["locator"]) == {
            "kind",
            "evidence_unit_id",
            "start",
            "end",
        }
        assert unit["locator"]["evidence_unit_id"] == unit["evidence_unit_id"]
        assert unit["locator"]["start"] == 0
        assert unit["locator"]["end"] == len(unit["normalized_text"])

    assert [section["section_ordinal"] for section in document["sections"]] == list(
        range(len(document["sections"]))
    )
    for section in document["sections"]:
        units = units_by_section[section["section_ordinal"]]
        assert [unit["unit_ordinal"] for unit in units] == list(range(len(units)))
        assert section["evidence_unit_ids"] == [
            unit["evidence_unit_id"] for unit in units
        ]
        assert all(unit["section_path"] == section["section_path"] for unit in units)


def test_shared_snapshot_contract_contains_no_adapter_or_orm_fields():
    forbidden = {
        "database_id",
        "file_name",
        "legacy_chunk_id",
        "local_path",
        "orm_id",
        "storage_ref",
        "uploaded_at",
    }
    keys = set(_all_keys(technical_submission_payload())) | set(
        _all_keys(technical_document_payload())
    )
    assert not (forbidden & keys)


def test_shared_checker_manifest_uses_one_namespaced_supported_scope_schema():
    manifest = checker_manifest_payload()
    assert set(manifest) == {"technical_proposal.required_sections.v1"}
    entry = manifest["technical_proposal.required_sections.v1"]
    assert entry["supported_document_schemas"] == ["document-snapshot@1"]
    assert entry["supported_profiles"] == [PROFILE_KEY]
    assert set(entry) == {
        "checker_version",
        "implementation_hash",
        "observation_schema",
        "params_schema",
        "supported_document_schemas",
        "supported_profiles",
    }


def test_shared_policy_snapshot_hash_binds_the_complete_replayable_mapping():
    policy = technical_policy_snapshot_payload()
    supplied_hash = policy.pop("policy_hash")

    assert policy["policy_key"] == "technical_proposal_contract_policy"
    assert policy["usage"] == "authoritative_new_runs"
    assert supplied_hash == canonical_sha256(policy)


def test_shared_runtime_identity_freezes_provider_sampling_and_anchor_artifacts():
    identity = runtime_identity_payload()

    assert identity["profile_key"] == PROFILE_KEY
    assert identity["provider"]["model_version"]
    assert identity["provider"]["sampling"] == {
        "temperature": "0",
        "top_p": "1",
        "seed": 20260719,
        "max_tokens": 512,
    }
    assert len(identity["provider"]["artifact_hash"]) == 64
    assert len(identity["calibration_anchors_hash"]) == 64
