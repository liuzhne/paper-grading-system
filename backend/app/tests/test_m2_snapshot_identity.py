"""M2 content-addressed document identity contracts.

The target module deliberately accepts plain, detached mappings.  These tests
freeze the identity projection before ``DocumentSnapshot`` persistence exists:
database IDs, storage paths, and business metadata are adapter concerns, while
only the fields named by ADR-0001 D09 may affect a Core document identity.

Until ``scoring.core.identity`` exposes the complete public API, every test is
a strict expected failure.  Once all four functions exist, the marker turns
off and every identity vector becomes an ordinary regression test.
"""

from __future__ import annotations

import builtins
from copy import deepcopy
import hashlib
from importlib import import_module
from importlib.util import find_spec
import json
import os
from pathlib import Path
import re
import socket
import sqlite3

import pytest

from backend.app.tests.m2_contract_fixtures import (
    document_snapshot_projection,
    evidence_unit_projection,
    normalized_content_projection,
    technical_document_payload,
    technical_normalized_content_input,
)


IDENTITY_MODULE = "backend.app.services.scoring.core.identity"
IDENTITY_API_NAMES = (
    "hash_source_artifact",
    "hash_normalized_content",
    "derive_evidence_unit_id",
    "hash_document_snapshot",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class M2CapabilityUnavailable(RuntimeError):
    """Raised only when the planned M2 identity API has not been implemented."""


# Capability discovery uses only importlib.  If the target module exists but
# raises during import, that error is intentionally allowed to fail collection;
# an implementation defect must not be disguised as an unavailable capability.
_IDENTITY_SPEC = find_spec(IDENTITY_MODULE)
_IDENTITY_MODULE_OBJECT = (
    import_module(IDENTITY_MODULE) if _IDENTITY_SPEC is not None else None
)
_IDENTITY_API = {
    name: (
        vars(_IDENTITY_MODULE_OBJECT).get(name)
        if _IDENTITY_MODULE_OBJECT is not None
        else None
    )
    for name in IDENTITY_API_NAMES
}
_MISSING_IDENTITY_API = tuple(
    name
    for name in IDENTITY_API_NAMES
    if _IDENTITY_MODULE_OBJECT is None or name not in vars(_IDENTITY_MODULE_OBJECT)
)


def _requires_identity(*names: str):
    return pytest.mark.xfail(
        condition=any(name in _MISSING_IDENTITY_API for name in names),
        raises=M2CapabilityUnavailable,
        reason=f"M2 scoring.core.identity API is missing: {', '.join(names)}",
        strict=True,
    )


def _require_identity_api(*names: str):
    """Return selected APIs or raise the one capability-only exception."""

    missing = tuple(name for name in names if name in _MISSING_IDENTITY_API)
    if missing:
        raise M2CapabilityUnavailable(
            f"{IDENTITY_MODULE} does not expose required M2 API: {', '.join(missing)}"
        )
    values = tuple(_IDENTITY_API[name] for name in names)
    # A present but non-callable symbol is an implementation defect, not an
    # unavailable capability, and therefore must fail rather than xfail.
    assert all(callable(value) for value in values)
    return values


def _canonical_sha256(value) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _assert_sha256(value):
    assert isinstance(value, str)
    assert SHA256_RE.fullmatch(value)


def _reverse_mapping_order(value):
    """Reverse object insertion order without changing any array order."""

    if isinstance(value, dict):
        return {
            key: _reverse_mapping_order(value[key])
            for key in reversed(tuple(value))
        }
    if isinstance(value, list):
        return [_reverse_mapping_order(item) for item in value]
    return value


def _replace_path(value, path, replacement):
    result = deepcopy(value)
    cursor = result
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = replacement
    return result


def _normalized_content_input():
    """A closed, adapter-detached normalized-content-v1 input."""

    return technical_normalized_content_input()


def _normalized_content_projection(value):
    return normalized_content_projection(value)


def _evidence_unit_input(normalized_content_hash=None):
    return {
        "normalized_content_hash": normalized_content_hash
        or _canonical_sha256(
            _normalized_content_projection(_normalized_content_input())
        ),
        "section_path": ["总体方案", "风险控制"],
        "section_ordinal": 1,
        "unit_ordinal": 0,
        "normalized_text": "风险负责人为项目经理。",
    }


def _evidence_unit_projection(value):
    return evidence_unit_projection(value)


# This literal is independently frozen from ADR-0001 D09's canonical payload.
EVIDENCE_UNIT_VECTOR_SHA256 = (
    "57eda0a4ccb2e541e3ee6d54bdb1145fe68bb435ca87c55555fef37034cb95ff"
)


def _document_snapshot_input():
    payload = technical_document_payload()
    payload.pop("document_snapshot_hash")
    return payload


def _document_snapshot_projection(value):
    return document_snapshot_projection(value)


@_requires_identity("hash_source_artifact")
def test_source_artifact_hash_is_sha256_of_raw_bytes_only():
    (hash_source_artifact,) = _require_identity_api("hash_source_artifact")
    raw = b"PK\x03\x04\x00technical-proposal\x00\xff"

    actual = hash_source_artifact(raw)

    assert actual == hashlib.sha256(raw).hexdigest()
    _assert_sha256(actual)
    with pytest.raises(TypeError):
        hash_source_artifact("not raw bytes")


@_requires_identity("hash_normalized_content")
def test_normalized_content_hash_matches_the_frozen_v1_projection():
    (hash_normalized_content,) = _require_identity_api("hash_normalized_content")
    source = _normalized_content_input()

    actual = hash_normalized_content(source)

    assert actual == _canonical_sha256(_normalized_content_projection(source))
    _assert_sha256(actual)


@_requires_identity("hash_normalized_content")
def test_normalized_content_hash_is_independent_of_mapping_insertion_order():
    (hash_normalized_content,) = _require_identity_api("hash_normalized_content")
    baseline = _normalized_content_input()
    variant = _reverse_mapping_order(deepcopy(baseline))

    assert hash_normalized_content(variant) == hash_normalized_content(baseline)


@_requires_identity("hash_normalized_content")
def test_normalized_content_hash_rejects_adapter_ids_paths_and_metadata():
    (hash_normalized_content,) = _require_identity_api("hash_normalized_content")
    baseline = _normalized_content_input()
    variants = []
    for field, value in (
        ("source_artifact_hash", "b" * 64),
        ("database_id", "document-row-b"),
        ("storage_ref", "/another/host/proposal.docx"),
        ("metadata", {"vendor_name": "not-content"}),
    ):
        variant = deepcopy(baseline)
        variant[field] = value
        variants.append(variant)
    nested = deepcopy(baseline)
    nested["sections"][0]["units"][0]["legacy_chunk_id"] = "chunk-other"
    variants.append(nested)

    for variant in variants:
        with pytest.raises((TypeError, ValueError)):
            hash_normalized_content(variant)


@_requires_identity("hash_normalized_content")
@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("normalizer_version",), "proposal-normalizer@2"),
        (("sections", 0, "section_path"), ["项目背景", "需求理解"]),
        (("sections", 0, "heading"), "需求与约束"),
        (("sections", 0, "normalized_text"), "正文内容发生改变。"),
        (("sections", 0, "units", 0, "normalized_text"), "单元正文发生改变。"),
    ],
    ids=(
        "normalizer-version",
        "section-path",
        "heading",
        "section-text",
        "unit-text",
    ),
)
def test_normalized_content_hash_changes_for_every_authoritative_field(
    path, replacement
):
    (hash_normalized_content,) = _require_identity_api("hash_normalized_content")
    baseline = _normalized_content_input()
    changed = _replace_path(baseline, path, replacement)

    assert hash_normalized_content(changed) != hash_normalized_content(baseline)


@_requires_identity("hash_normalized_content")
def test_normalized_content_hash_preserves_canonical_document_order():
    (hash_normalized_content,) = _require_identity_api("hash_normalized_content")
    baseline = _normalized_content_input()
    reversed_sections = deepcopy(baseline)
    reversed_sections["sections"] = list(reversed(reversed_sections["sections"]))

    assert hash_normalized_content(reversed_sections) != hash_normalized_content(
        baseline
    )


@_requires_identity("derive_evidence_unit_id")
def test_evidence_unit_id_matches_independent_frozen_v1_vector():
    (derive_evidence_unit_id,) = _require_identity_api("derive_evidence_unit_id")
    source = _evidence_unit_input()
    expected = _canonical_sha256(_evidence_unit_projection(source))

    # Guard the test-side vector itself; an accidental fixture edit must be
    # reviewed and cannot silently redefine evidence-unit-id-v1.
    assert expected == EVIDENCE_UNIT_VECTOR_SHA256
    actual = derive_evidence_unit_id(source)

    assert actual == expected
    _assert_sha256(actual)


@_requires_identity("derive_evidence_unit_id")
def test_evidence_unit_id_is_independent_of_mapping_insertion_order():
    (derive_evidence_unit_id,) = _require_identity_api("derive_evidence_unit_id")
    baseline = _evidence_unit_input()
    variant = _reverse_mapping_order(deepcopy(baseline))

    assert derive_evidence_unit_id(variant) == derive_evidence_unit_id(baseline)


@_requires_identity("derive_evidence_unit_id")
def test_evidence_unit_id_rejects_database_chunk_path_and_existing_id():
    (derive_evidence_unit_id,) = _require_identity_api("derive_evidence_unit_id")
    for field, value in (
        ("database_id", "different-database-row"),
        ("legacy_chunk_id", "different-legacy-chunk"),
        ("local_path", "/different/machine/chunk.json"),
        ("evidence_unit_id", "1" * 64),
    ):
        variant = _evidence_unit_input()
        variant[field] = value
        with pytest.raises((TypeError, ValueError)):
            derive_evidence_unit_id(variant)


@_requires_identity("derive_evidence_unit_id")
@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("normalized_content_hash",), "c" * 64),
        (("section_path",), ["总体方案", "项目风险"]),
        (("section_ordinal",), 4),
        (("unit_ordinal",), 2),
        (("normalized_text",), "风险负责人变更为交付经理。"),
    ],
    ids=(
        "normalized-content",
        "section-path",
        "section-ordinal",
        "unit-ordinal",
        "unit-text",
    ),
)
def test_evidence_unit_id_changes_for_every_authoritative_field(path, replacement):
    (derive_evidence_unit_id,) = _require_identity_api("derive_evidence_unit_id")
    baseline = _evidence_unit_input()
    changed = _replace_path(baseline, path, replacement)

    assert derive_evidence_unit_id(changed) != derive_evidence_unit_id(baseline)


@_requires_identity("derive_evidence_unit_id")
def test_duplicate_section_titles_are_disambiguated_by_global_ordinal():
    (derive_evidence_unit_id,) = _require_identity_api("derive_evidence_unit_id")
    first = _evidence_unit_input()
    second = deepcopy(first)
    second["section_ordinal"] = first["section_ordinal"] + 1

    assert first["section_path"] == second["section_path"]
    assert first["normalized_text"] == second["normalized_text"]
    assert derive_evidence_unit_id(first) != derive_evidence_unit_id(second)


@_requires_identity("hash_document_snapshot")
def test_document_snapshot_hash_matches_the_explicit_v1_projection():
    (hash_document_snapshot,) = _require_identity_api("hash_document_snapshot")
    snapshot = _document_snapshot_input()

    actual = hash_document_snapshot(snapshot)

    assert actual == _canonical_sha256(_document_snapshot_projection(snapshot))
    _assert_sha256(actual)


@_requires_identity("hash_document_snapshot")
def test_document_snapshot_hash_ignores_order_and_non_scoring_diagnostics():
    (hash_document_snapshot,) = _require_identity_api("hash_document_snapshot")
    baseline = _document_snapshot_input()
    variant = _reverse_mapping_order(deepcopy(baseline))
    variant["parser_diagnostics"][1]["code"] = "DIFFERENT_DEBUG_HINT"
    variant["parser_diagnostics"][1]["message"] = "changed non-scoring detail"

    assert hash_document_snapshot(variant) == hash_document_snapshot(baseline)


@_requires_identity("hash_document_snapshot")
def test_document_snapshot_hash_rejects_storage_orm_upload_and_business_metadata():
    (hash_document_snapshot,) = _require_identity_api("hash_document_snapshot")
    baseline = _document_snapshot_input()
    variants = []
    for field, value in (
        ("source_artifact_hash", "d" * 64),
        ("database_id", "different-document-row"),
        ("storage_ref", "/another/host/proposal.docx"),
        ("file_name", "renamed-proposal.docx"),
        ("created_at", "2032-01-01T00:00:00Z"),
        ("metadata", {"proposal_id": "TP-NON-SCORING-CHANGE"}),
    ):
        variant = deepcopy(baseline)
        variant[field] = value
        variants.append(variant)
    nested = deepcopy(baseline)
    nested["evidence_units"][0]["legacy_chunk_id"] = "different-chunk-row"
    variants.append(nested)

    for variant in variants:
        with pytest.raises((TypeError, ValueError)):
            hash_document_snapshot(variant)


@_requires_identity("hash_document_snapshot")
@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("schema_version",), "document-snapshot@2"),
        (("profile_key",), "technical_proposal_variant"),
        (("profile_version",), "technical-proposal-contract@2"),
        (("parser_version",), "proposal-parser@2"),
        (("evidence_units", 0, "locator", "start"), 1),
        (("metrics", "word_count"), 1821),
        (("format_facts", "page_size"), "Letter"),
        (("parse_quality",), "0.97"),
        (("profile_extensions", "technical_proposal", "risk_owner_required"), False),
        (("parser_diagnostics", 0, "code"), "TABLE_TEXT_UNAVAILABLE"),
    ],
    ids=(
        "snapshot-schema",
        "profile-key",
        "profile-version",
        "parser-version",
        "unit-locator",
        "metrics",
        "format-facts",
        "parse-quality",
        "profile-extension",
        "diagnostic",
    ),
)
def test_document_snapshot_hash_changes_for_every_authoritative_field(
    path, replacement
):
    (hash_document_snapshot,) = _require_identity_api("hash_document_snapshot")
    baseline = _document_snapshot_input()
    changed = _replace_path(baseline, path, replacement)

    assert hash_document_snapshot(changed) != hash_document_snapshot(baseline)


@_requires_identity("hash_document_snapshot")
@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("normalizer_version",), "proposal-normalizer@2"),
        (("content_hash",), "e" * 64),
        (("sections", 0, "section_path"), ["总体方案", "项目风险"]),
        (("sections", 0, "section_ordinal"), 8),
        (("sections", 0, "evidence_unit_ids"), ["2" * 64]),
        (("sections", 0, "locator", "section_ordinal"), 8),
        (("evidence_units", 0, "evidence_unit_id"), "3" * 64),
    ],
    ids=(
        "normalizer-version-with-stale-content-hash",
        "content-hash-with-stale-unit-identities",
        "section-path-with-stale-unit-paths",
        "noncontiguous-section-ordinal",
        "section-unit-manifest-mismatch",
        "section-locator-mismatch",
        "unit-id-with-stale-locator-and-manifest",
    ),
)
def test_document_snapshot_hash_rejects_cross_identity_tampering(path, replacement):
    (hash_document_snapshot,) = _require_identity_api("hash_document_snapshot")
    changed = _replace_path(_document_snapshot_input(), path, replacement)

    with pytest.raises((TypeError, ValueError)):
        hash_document_snapshot(changed)


@_requires_identity(*IDENTITY_API_NAMES)
def test_identity_functions_do_not_touch_database_filesystem_or_network(monkeypatch):
    (
        hash_source_artifact,
        hash_normalized_content,
        derive_evidence_unit_id,
        hash_document_snapshot,
    ) = _require_identity_api(*IDENTITY_API_NAMES)

    def unexpected_io(*_args, **_kwargs):
        raise AssertionError("Core identity hashing must be pure and in-memory")

    monkeypatch.setattr(builtins, "open", unexpected_io)
    monkeypatch.setattr(os, "open", unexpected_io)
    monkeypatch.setattr(Path, "open", unexpected_io)
    monkeypatch.setattr(sqlite3, "connect", unexpected_io)
    monkeypatch.setattr(socket, "socket", unexpected_io)
    monkeypatch.setattr(socket, "create_connection", unexpected_io)

    normalized = _normalized_content_input()
    unit = _evidence_unit_input()
    document = _document_snapshot_input()
    _assert_sha256(hash_source_artifact(b"pure in-memory artifact"))
    _assert_sha256(hash_normalized_content(normalized))
    _assert_sha256(derive_evidence_unit_id(unit))
    _assert_sha256(hash_document_snapshot(document))
