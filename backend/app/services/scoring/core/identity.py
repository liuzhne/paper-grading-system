"""Pure content-addressed identities for immutable scoring inputs.

The functions in this module deliberately accept detached mappings instead of
database or transport objects.  Each identity has a closed, versioned
projection: fields outside that projection are rejected rather than silently
ignored, so adapter-local state can never accidentally become part of a Core
cache or replay key.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import re
import unicodedata

from .canonical import canonical_sha256


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _mapping(value, *, label: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    return value


def _closed_mapping(value, expected_keys, *, label: str) -> Mapping:
    result = _mapping(value, label=label)
    actual = set(result)
    expected = set(expected_keys)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected, key=str)
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("unknown: " + ", ".join(str(item) for item in extra))
        raise ValueError(f"{label} has invalid fields ({'; '.join(details)})")
    if any(not isinstance(key, str) for key in result):
        raise TypeError(f"{label} field names must be strings")
    return result


def _array(value, *, label: str):
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{label} must be an array")
    return value


def _text(value, *, label: str, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _normalized_unit_text(value, *, label: str) -> str:
    text = _text(value, label=label, allow_empty=False)
    if not text.strip():
        raise ValueError(f"{label} must contain non-whitespace normalized text")
    return text


def _ordinal(value, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < 0:
        raise ValueError(f"{label} must be non-negative")
    return value


def _sha256(value, *, label: str) -> str:
    _text(value, label=label)
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _section_path(value, *, label: str) -> list[str]:
    path = _array(value, label=label)
    if not path:
        raise ValueError(f"{label} must not be empty")
    result = []
    for index, part in enumerate(path):
        text = _text(part, label=f"{label}[{index}]")
        normalized = unicodedata.normalize("NFC", text).strip()
        if not normalized:
            raise ValueError(f"{label}[{index}] must not be empty after NFC+trim")
        result.append(normalized)
    return result


def _canonical_mapping(value, *, label: str) -> dict:
    """Detach an open business-value mapping and validate canonical values."""

    source = _mapping(value, label=label)
    result = dict(source)
    # Canonicalizing here is validation as well as a guard against ORM/path/
    # UUID fallbacks.  ``canonical_sha256`` rejects every unsupported value.
    canonical_sha256(result)
    return result


def hash_source_artifact(raw_bytes: bytes) -> str:
    """Return the SHA-256 identity of the exact uploaded artifact bytes."""

    if not isinstance(raw_bytes, bytes):
        raise TypeError("source artifact must be raw bytes")
    return hashlib.sha256(raw_bytes).hexdigest()


def scoring_request_idempotency_projection(value: Mapping) -> dict:
    """Return the shared replay identity used by every Core adapter."""

    request = _mapping(value, label="scoring request")
    submission = _mapping(request["submission"], label="submission snapshot")
    document = _mapping(request["document"], label="document snapshot")
    plan = _mapping(request["plan"], label="rule execution plan")
    return {
        "scheme": "scoring-request-idempotency-v1",
        "submission_snapshot_hash": canonical_sha256(submission),
        "source_artifact_hash": submission["source_artifact_hash"],
        "document_snapshot_hash": document["document_snapshot_hash"],
        "normalized_content_hash": document["content_hash"],
        "rubric_source_kind": plan["rubric_source_kind"],
        "rubric_version_id": plan["rubric_version_id"],
        "rubric_version_hash": plan["rubric_version_hash"],
        "rubric_hash_scheme": plan["rubric_hash_scheme"],
        "rubric_snapshot_hash": plan["rubric_snapshot_hash"],
        "execution_plan_hash": plan["plan_hash"],
        "policy_hash": plan["policy_hash"],
        "business_profile_key": submission["profile_key"],
        "business_profile_version": document["profile_version"],
        "runtime_identity": request["runtime_identity"],
        "rescore_generation": request["rescore_generation"],
    }


def _normalized_content_projection(value) -> dict:
    source = _closed_mapping(
        value,
        {"normalizer_version", "sections"},
        label="normalized content",
    )
    normalizer_version = _text(
        source["normalizer_version"],
        label="normalized content.normalizer_version",
        allow_empty=False,
    )
    sections = []
    for section_index, section_value in enumerate(
        _array(source["sections"], label="normalized content.sections")
    ):
        label = f"normalized content.sections[{section_index}]"
        section = _closed_mapping(
            section_value,
            {"section_path", "heading", "normalized_text", "units"},
            label=label,
        )
        units = []
        for unit_index, unit_value in enumerate(
            _array(section["units"], label=f"{label}.units")
        ):
            unit_label = f"{label}.units[{unit_index}]"
            unit = _closed_mapping(
                unit_value,
                {"normalized_text"},
                label=unit_label,
            )
            units.append(
                {
                    "normalized_text": _normalized_unit_text(
                        unit["normalized_text"],
                        label=f"{unit_label}.normalized_text",
                    )
                }
            )
        sections.append(
            {
                "section_path": _section_path(
                    section["section_path"], label=f"{label}.section_path"
                ),
                "heading": _text(section["heading"], label=f"{label}.heading"),
                "normalized_text": _text(
                    section["normalized_text"], label=f"{label}.normalized_text"
                ),
                "units": units,
            }
        )
    return {
        "scheme": "normalized-content-v1",
        "normalizer_version": normalizer_version,
        "sections": sections,
    }


def hash_normalized_content(value: Mapping) -> str:
    """Hash the closed ``normalized-content-v1`` authoritative projection."""

    return canonical_sha256(_normalized_content_projection(value))


def _evidence_unit_projection(value) -> dict:
    source = _closed_mapping(
        value,
        {
            "normalized_content_hash",
            "section_path",
            "section_ordinal",
            "unit_ordinal",
            "normalized_text",
        },
        label="evidence unit identity input",
    )
    normalized_text = _normalized_unit_text(
        source["normalized_text"],
        label="evidence unit identity input.normalized_text",
    )
    return {
        "scheme": "evidence-unit-id-v1",
        "normalized_content_hash": _sha256(
            source["normalized_content_hash"],
            label="evidence unit identity input.normalized_content_hash",
        ),
        "section_path": _section_path(
            source["section_path"],
            label="evidence unit identity input.section_path",
        ),
        "section_ordinal": _ordinal(
            source["section_ordinal"],
            label="evidence unit identity input.section_ordinal",
        ),
        "unit_ordinal": _ordinal(
            source["unit_ordinal"],
            label="evidence unit identity input.unit_ordinal",
        ),
        "unit_text_hash": hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
    }


def derive_evidence_unit_id(value: Mapping) -> str:
    """Derive a stable global evidence-unit identity from normalized content."""

    return canonical_sha256(_evidence_unit_projection(value))


def _section_locator(
    value,
    *,
    label: str,
    section_path: list[str],
    section_ordinal: int,
) -> dict:
    locator = _closed_mapping(
        value,
        {"kind", "section_path", "section_ordinal"},
        label=label,
    )
    kind = _text(locator["kind"], label=f"{label}.kind", allow_empty=False)
    locator_path = _section_path(
        locator["section_path"], label=f"{label}.section_path"
    )
    locator_ordinal = _ordinal(
        locator["section_ordinal"], label=f"{label}.section_ordinal"
    )
    if kind != "section":
        raise ValueError(f"{label}.kind must be section")
    if locator_path != section_path or locator_ordinal != section_ordinal:
        raise ValueError(f"{label} does not identify its section")
    return {
        "kind": kind,
        "section_path": locator_path,
        "section_ordinal": locator_ordinal,
    }


def _unit_locator(
    value,
    *,
    label: str,
    evidence_unit_id: str,
    text_length: int,
) -> dict:
    locator = _closed_mapping(
        value,
        {"kind", "evidence_unit_id", "start", "end"},
        label=label,
    )
    kind = _text(locator["kind"], label=f"{label}.kind", allow_empty=False)
    locator_id = _sha256(
        locator["evidence_unit_id"], label=f"{label}.evidence_unit_id"
    )
    start = _ordinal(locator["start"], label=f"{label}.start")
    end = _ordinal(locator["end"], label=f"{label}.end")
    if kind != "text_span":
        raise ValueError(f"{label}.kind must be text_span")
    if locator_id != evidence_unit_id:
        raise ValueError(f"{label} does not identify its evidence unit")
    if start >= end or end > text_length:
        raise ValueError(f"{label} span is outside its evidence text")
    return {
        "kind": kind,
        "evidence_unit_id": locator_id,
        "start": start,
        "end": end,
    }


def _document_snapshot_projection(value) -> dict:
    source = _closed_mapping(
        value,
        {
            "schema_version",
            "profile_key",
            "profile_version",
            "parser_version",
            "normalizer_version",
            "content_hash",
            "full_text",
            "sections",
            "evidence_units",
            "metrics",
            "format_facts",
            "parse_quality",
            "parser_diagnostics",
            "profile_extensions",
        },
        label="document snapshot",
    )

    # These content fields are bound transitively through ``content_hash`` and
    # therefore do not appear twice in the document-snapshot-v1 projection.
    # They still need full consistency validation before the projection may be
    # treated as an authoritative replay identity.
    full_text = _text(source["full_text"], label="document snapshot.full_text")
    content_hash = _sha256(
        source["content_hash"], label="document snapshot.content_hash"
    )
    normalizer_version = _text(
        source["normalizer_version"],
        label="document snapshot.normalizer_version",
        allow_empty=False,
    )

    sections = []
    for section_index, section_value in enumerate(
        _array(source["sections"], label="document snapshot.sections")
    ):
        label = f"document snapshot.sections[{section_index}]"
        section = _closed_mapping(
            section_value,
            {
                "section_path",
                "section_ordinal",
                "heading",
                "normalized_text",
                "evidence_unit_ids",
                "locator",
            },
            label=label,
        )
        section_path = _section_path(
            section["section_path"], label=f"{label}.section_path"
        )
        section_ordinal = _ordinal(
            section["section_ordinal"], label=f"{label}.section_ordinal"
        )
        normalized_text = _text(
            section["normalized_text"], label=f"{label}.normalized_text"
        )
        evidence_unit_ids = [
            _sha256(item, label=f"{label}.evidence_unit_ids[{index}]")
            for index, item in enumerate(
                _array(section["evidence_unit_ids"], label=f"{label}.evidence_unit_ids")
            )
        ]
        if len(evidence_unit_ids) != len(set(evidence_unit_ids)):
            raise ValueError(f"{label}.evidence_unit_ids contains duplicates")
        sections.append(
            {
                "section_path": section_path,
                "section_ordinal": section_ordinal,
                "heading": _text(section["heading"], label=f"{label}.heading"),
                "normalized_text": normalized_text,
                "evidence_unit_ids": evidence_unit_ids,
                "locator": _section_locator(
                    section["locator"],
                    label=f"{label}.locator",
                    section_path=section_path,
                    section_ordinal=section_ordinal,
                ),
            }
        )

    evidence_units = []
    for unit_index, unit_value in enumerate(
        _array(source["evidence_units"], label="document snapshot.evidence_units")
    ):
        label = f"document snapshot.evidence_units[{unit_index}]"
        unit = _closed_mapping(
            unit_value,
            {
                "normalized_content_hash",
                "section_path",
                "section_ordinal",
                "unit_ordinal",
                "normalized_text",
                "evidence_unit_id",
                "unit_text_hash",
                "locator",
            },
            label=label,
        )
        normalized_content_hash = _sha256(
            unit["normalized_content_hash"],
            label=f"{label}.normalized_content_hash",
        )
        section_path = _section_path(
            unit["section_path"], label=f"{label}.section_path"
        )
        section_ordinal = _ordinal(
            unit["section_ordinal"], label=f"{label}.section_ordinal"
        )
        unit_ordinal = _ordinal(
            unit["unit_ordinal"], label=f"{label}.unit_ordinal"
        )
        normalized_text = _normalized_unit_text(
            unit["normalized_text"],
            label=f"{label}.normalized_text",
        )
        evidence_unit_id = _sha256(
            unit["evidence_unit_id"], label=f"{label}.evidence_unit_id"
        )
        unit_text_hash = _sha256(
            unit["unit_text_hash"], label=f"{label}.unit_text_hash"
        )
        expected_text_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
        if unit_text_hash != expected_text_hash:
            raise ValueError(f"{label}.unit_text_hash does not match normalized_text")
        expected_evidence_unit_id = derive_evidence_unit_id(
            {
                "normalized_content_hash": normalized_content_hash,
                "section_path": section_path,
                "section_ordinal": section_ordinal,
                "unit_ordinal": unit_ordinal,
                "normalized_text": normalized_text,
            }
        )
        if evidence_unit_id != expected_evidence_unit_id:
            raise ValueError(f"{label}.evidence_unit_id does not match its content")
        evidence_units.append(
            {
                "normalized_content_hash": normalized_content_hash,
                "evidence_unit_id": evidence_unit_id,
                "section_path": section_path,
                "section_ordinal": section_ordinal,
                "unit_ordinal": unit_ordinal,
                "normalized_text": normalized_text,
                "unit_text_hash": unit_text_hash,
                "locator": _unit_locator(
                    unit["locator"],
                    label=f"{label}.locator",
                    evidence_unit_id=evidence_unit_id,
                    text_length=len(normalized_text),
                ),
            }
        )

    if full_text != "\n".join(section["normalized_text"] for section in sections):
        raise ValueError(
            "document snapshot.full_text does not match canonical section content"
        )
    if [section["section_ordinal"] for section in sections] != list(
        range(len(sections))
    ):
        raise ValueError(
            "document snapshot section ordinals must be contiguous document order"
        )

    global_unit_order = [
        (unit["section_ordinal"], unit["unit_ordinal"])
        for unit in evidence_units
    ]
    if global_unit_order != sorted(global_unit_order):
        raise ValueError(
            "document snapshot evidence units must use canonical document order"
        )
    section_ordinals = set(range(len(sections)))
    units_by_section: dict[int, list[dict]] = {}
    seen_evidence_unit_ids = set()
    for unit in evidence_units:
        if unit["section_ordinal"] not in section_ordinals:
            raise ValueError("document snapshot contains an orphan evidence unit")
        if unit["normalized_content_hash"] != content_hash:
            raise ValueError(
                "document snapshot evidence normalized content hash mismatch"
            )
        if unit["evidence_unit_id"] in seen_evidence_unit_ids:
            raise ValueError("document snapshot contains duplicate evidence identity")
        seen_evidence_unit_ids.add(unit["evidence_unit_id"])
        units_by_section.setdefault(unit["section_ordinal"], []).append(unit)

    normalized_sections = []
    for section in sections:
        units = units_by_section.get(section["section_ordinal"], [])
        if [unit["unit_ordinal"] for unit in units] != list(range(len(units))):
            raise ValueError(
                "document snapshot unit ordinals must be contiguous section order"
            )
        if any(unit["section_path"] != section["section_path"] for unit in units):
            raise ValueError("document snapshot evidence section path mismatch")
        unit_ids = [unit["evidence_unit_id"] for unit in units]
        if unit_ids != section["evidence_unit_ids"]:
            raise ValueError(
                "document snapshot section evidence identities do not match units"
            )
        normalized_sections.append(
            {
                "section_path": section["section_path"],
                "heading": section["heading"],
                "normalized_text": section["normalized_text"],
                "units": [
                    {"normalized_text": unit["normalized_text"]} for unit in units
                ],
            }
        )

    calculated_content_hash = hash_normalized_content(
        {
            "normalizer_version": normalizer_version,
            "sections": normalized_sections,
        }
    )
    if calculated_content_hash != content_hash:
        raise ValueError(
            "document snapshot.content_hash does not match normalized content"
        )

    diagnostics = []
    for diagnostic_index, diagnostic_value in enumerate(
        _array(
            source["parser_diagnostics"],
            label="document snapshot.parser_diagnostics",
        )
    ):
        label = f"document snapshot.parser_diagnostics[{diagnostic_index}]"
        diagnostic = _closed_mapping(
            diagnostic_value,
            {"code", "severity", "message", "scoring_relevant"},
            label=label,
        )
        scoring_relevant = diagnostic["scoring_relevant"]
        if not isinstance(scoring_relevant, bool):
            raise TypeError(f"{label}.scoring_relevant must be a boolean")
        code = _text(diagnostic["code"], label=f"{label}.code", allow_empty=False)
        severity = _text(
            diagnostic["severity"], label=f"{label}.severity", allow_empty=False
        )
        message = _text(diagnostic["message"], label=f"{label}.message")
        if scoring_relevant:
            diagnostics.append(
                {"code": code, "severity": severity, "message": message}
            )

    return {
        "scheme": "document-snapshot-v1",
        "schema_version": _text(
            source["schema_version"],
            label="document snapshot.schema_version",
            allow_empty=False,
        ),
        "business_profile_key": _text(
            source["profile_key"],
            label="document snapshot.profile_key",
            allow_empty=False,
        ),
        "business_profile_version": _text(
            source["profile_version"],
            label="document snapshot.profile_version",
            allow_empty=False,
        ),
        "parser_version": _text(
            source["parser_version"],
            label="document snapshot.parser_version",
            allow_empty=False,
        ),
        "normalizer_version": normalizer_version,
        "content_hash": content_hash,
        "sections": [
            {
                "section_path": section["section_path"],
                "section_ordinal": section["section_ordinal"],
                "heading": section["heading"],
                "evidence_unit_ids": section["evidence_unit_ids"],
                "locator": section["locator"],
            }
            for section in sections
        ],
        "evidence_units": [
            {
                "evidence_unit_id": unit["evidence_unit_id"],
                "section_path": unit["section_path"],
                "section_ordinal": unit["section_ordinal"],
                "unit_ordinal": unit["unit_ordinal"],
                "locator": unit["locator"],
            }
            for unit in evidence_units
        ],
        "metrics": _canonical_mapping(
            source["metrics"], label="document snapshot.metrics"
        ),
        "format_facts": _canonical_mapping(
            source["format_facts"], label="document snapshot.format_facts"
        ),
        "parse_quality": source["parse_quality"],
        "profile_extensions": _canonical_mapping(
            source["profile_extensions"],
            label="document snapshot.profile_extensions",
        ),
        "diagnostics": diagnostics,
    }


def hash_document_snapshot(value: Mapping) -> str:
    """Hash the closed replay-relevant ``document-snapshot-v1`` projection."""

    return canonical_sha256(_document_snapshot_projection(value))


__all__ = [
    "derive_evidence_unit_id",
    "hash_document_snapshot",
    "hash_normalized_content",
    "hash_source_artifact",
    "scoring_request_idempotency_projection",
]
