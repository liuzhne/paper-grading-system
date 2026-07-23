"""Translate legacy paper/parser rows into immutable Core snapshots.

The legacy database contains storage paths and generated row identifiers.  None
of those values is a content identity, so this adapter deliberately projects
only business metadata, the caller supplied source-artifact digest and
normalized parser content into the Core contracts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import unicodedata

from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.contracts import (
    DocumentSnapshot,
    SubmissionSnapshot,
)
from backend.app.services.scoring.core.identity import (
    derive_evidence_unit_id,
    hash_document_snapshot,
    hash_normalized_content,
)


def _field(value, name, default=None):
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value, *, default="") -> str:
    if value is None:
        return default
    return unicodedata.normalize("NFC", str(value)).strip()


def _sha256(value, *, label: str) -> str:
    text = _text(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _decimal_text(value, *, default="0") -> str:
    try:
        result = Decimal(str(value if value is not None else default))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("legacy parse_quality must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError("legacy parse_quality must be a finite decimal")
    normalized = result.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _business_metadata(paper, parsed) -> dict:
    """Keep stable, user-facing metadata and exclude every storage identity."""

    fields = (
        "student_id",
        "student_name",
        "title",
        "department",
        "major",
        "advisor",
    )
    result = {}
    for name in fields:
        value = _field(paper, name)
        if value in (None, ""):
            value = _field(parsed, name)
        if value not in (None, ""):
            result[name] = _text(value)
    return result


def _section_units(raw_section) -> list[str]:
    paragraphs = _field(raw_section, "paragraphs", ()) or ()
    units = [
        _text(_field(paragraph, "text"))
        for paragraph in paragraphs
        if _text(_field(paragraph, "text"))
    ]
    if units:
        return units
    section_text = _text(
        _field(raw_section, "normalized_text", _field(raw_section, "text", ""))
    )
    return [section_text] if section_text else []


def _normalized_sections(parsed) -> list[dict]:
    sections = []
    parent_headings: list[str] = []
    for raw_section in _field(parsed, "sections", ()) or ():
        heading = _text(_field(raw_section, "title", _field(raw_section, "heading", "")))
        if not heading:
            heading = "正文"
        level_value = _field(raw_section, "level", 1)
        try:
            level = max(1, int(level_value))
        except (TypeError, ValueError):
            level = 1
        parent_headings = parent_headings[: level - 1]
        parent_headings.append(heading)
        units = _section_units(raw_section)
        # Empty legacy headings cannot carry evidence and are omitted.  Their
        # generated database IDs must never become a fallback identity.
        if not units:
            continue
        sections.append(
            {
                "section_path": list(parent_headings),
                "heading": heading,
                "normalized_text": "\n".join(units),
                "units": [{"normalized_text": item} for item in units],
            }
        )

    if sections:
        return sections

    full_text = _text(_field(parsed, "full_text", ""))
    if not full_text:
        raise ValueError("legacy parsed paper contains no normalized content")
    units = [line.strip() for line in full_text.splitlines() if line.strip()]
    return [
        {
            "section_path": ["正文"],
            "heading": "正文",
            "normalized_text": "\n".join(units),
            "units": [{"normalized_text": item} for item in units],
        }
    ]


def _diagnostics(parsed) -> list[dict]:
    result = []
    for check in _field(parsed, "structure_checks", ()) or ():
        if not isinstance(check, Mapping):
            continue
        passed = bool(check.get("passed"))
        result.append(
            {
                "code": _text(check.get("code"), default="LEGACY_STRUCTURE_CHECK"),
                "severity": "info" if passed else "warning",
                "message": _text(check.get("message"), default=""),
                "scoring_relevant": not passed,
            }
        )
    return result


def _metrics(parsed, normalized_sections) -> dict:
    full_text = "\n".join(item["normalized_text"] for item in normalized_sections)
    return {
        "character_count": len(full_text),
        "section_count": len(normalized_sections),
        "reference_count": len(_field(parsed, "references", ()) or ()),
    }


def _format_facts(parsed) -> dict:
    value = _field(parsed, "format_facts", {}) or {}
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class LegacyPaperSnapshots:
    submission: SubmissionSnapshot
    document: DocumentSnapshot


class LegacyPaperAdapter:
    """Build stable Core DTOs from already parsed legacy paper content."""

    def adapt(
        self,
        *,
        paper,
        parsed,
        chunks: Sequence[object] = (),
        source_artifact_hash: str,
        profile_key: str,
        profile_version: str,
        parser_version: str,
        normalizer_version: str,
        submission_instance_key: str | None = None,
    ) -> LegacyPaperSnapshots:
        # ``chunks`` are intentionally not an identity source.  Legacy chunk
        # row IDs and boundaries are mutable retrieval artifacts; normalized
        # parser sections are the authoritative replay input.
        del chunks
        artifact_hash = _sha256(
            source_artifact_hash, label="source_artifact_hash"
        )
        profile_key = _text(profile_key)
        profile_version = _text(profile_version)
        parser_version = _text(parser_version)
        normalizer_version = _text(normalizer_version)
        if not all((profile_key, profile_version, parser_version, normalizer_version)):
            raise ValueError("legacy adapter versions and profile must be non-empty")

        metadata = _business_metadata(paper, parsed)
        if submission_instance_key is None:
            submission_identity_input = {
                "scheme": "legacy-submission-id-v1",
                "profile_key": profile_key,
                "metadata": metadata,
            }
        else:
            instance_key = _text(submission_instance_key)
            if not instance_key:
                raise ValueError("submission instance key must be non-empty")
            # Multiple uploaded Paper rows may intentionally contain identical
            # bytes and metadata.  Keep their request idempotency scopes
            # distinct without leaking the database identifier into the Core
            # snapshot or turning it into a document content identity.
            submission_identity_input = {
                "scheme": "legacy-submission-instance-id-v1",
                "profile_key": profile_key,
                "instance_key": instance_key,
            }
        submission_identity = canonical_sha256(submission_identity_input)
        submission = SubmissionSnapshot.from_mapping(
            {
                "schema_version": "submission-snapshot@1",
                "submission_id": "legacy:" + submission_identity,
                "profile_key": profile_key,
                "source_artifact_hash": artifact_hash,
                "metadata": metadata,
                "artifact_refs": [
                    {
                        "kind": "source",
                        "ref": "blob:sha256:" + artifact_hash,
                        "content_hash": artifact_hash,
                    }
                ],
            }
        )

        normalized_sections = _normalized_sections(parsed)
        normalized_input = {
            "normalizer_version": normalizer_version,
            "sections": normalized_sections,
        }
        content_hash = hash_normalized_content(normalized_input)
        sections = []
        evidence_units = []
        for section_ordinal, section in enumerate(normalized_sections):
            section_unit_ids = []
            for unit_ordinal, unit in enumerate(section["units"]):
                unit_text = unit["normalized_text"]
                identity_input = {
                    "normalized_content_hash": content_hash,
                    "section_path": list(section["section_path"]),
                    "section_ordinal": section_ordinal,
                    "unit_ordinal": unit_ordinal,
                    "normalized_text": unit_text,
                }
                evidence_unit_id = derive_evidence_unit_id(identity_input)
                section_unit_ids.append(evidence_unit_id)
                evidence_units.append(
                    {
                        **identity_input,
                        "evidence_unit_id": evidence_unit_id,
                        "unit_text_hash": hashlib.sha256(
                            unit_text.encode("utf-8")
                        ).hexdigest(),
                        "locator": {
                            "kind": "text_span",
                            "evidence_unit_id": evidence_unit_id,
                            "start": 0,
                            "end": len(unit_text),
                        },
                    }
                )
            sections.append(
                {
                    "section_path": list(section["section_path"]),
                    "section_ordinal": section_ordinal,
                    "heading": section["heading"],
                    "normalized_text": section["normalized_text"],
                    "evidence_unit_ids": section_unit_ids,
                    "locator": {
                        "kind": "section",
                        "section_path": list(section["section_path"]),
                        "section_ordinal": section_ordinal,
                    },
                }
            )

        document_content = {
            "schema_version": "document-snapshot@1",
            "profile_key": profile_key,
            "profile_version": profile_version,
            "parser_version": parser_version,
            "normalizer_version": normalizer_version,
            "content_hash": content_hash,
            "full_text": "\n".join(
                section["normalized_text"] for section in normalized_sections
            ),
            "sections": sections,
            "evidence_units": evidence_units,
            "metrics": _metrics(parsed, normalized_sections),
            "format_facts": _format_facts(parsed),
            "parse_quality": _decimal_text(
                _field(parsed, "parse_quality", _field(paper, "parse_quality", 0))
            ),
            "parser_diagnostics": _diagnostics(parsed),
            "profile_extensions": {},
        }
        document = DocumentSnapshot.from_mapping(
            {
                **document_content,
                "document_snapshot_hash": hash_document_snapshot(document_content),
            }
        )
        return LegacyPaperSnapshots(submission=submission, document=document)


__all__ = ["LegacyPaperAdapter", "LegacyPaperSnapshots"]
