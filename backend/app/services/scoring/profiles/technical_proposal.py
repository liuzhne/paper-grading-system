"""Production TechnicalProposalProfile for software solution reviews.

The Profile interprets the generic document extraction, owns its deterministic
checker packages and semantic prompt/runtime adapter, and never creates a
``Paper`` or imports thesis parsing behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
from types import MappingProxyType

from backend.app.services.llm.base import LLMScoringError
from backend.app.services.llm.core_adapter import CORE_PROVIDER_PROMPT_VERSION
from backend.app.services.llm.core_adapter import core_runtime_provider_contract
from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.checker_registry import (
    VersionedCheckerRegistry,
)
from backend.app.services.scoring.core.contracts import DocumentSnapshot
from backend.app.services.scoring.core.identity import derive_evidence_unit_id
from backend.app.services.scoring.core.identity import hash_document_snapshot
from backend.app.services.scoring.core.identity import hash_normalized_content


PROFILE_KEY = "technical_proposal"
TECHNICAL_PROPOSAL_PROFILE_VERSION = "technical-proposal-profile@1"
TECHNICAL_PROPOSAL_PROMPT_VERSION = "technical-proposal-prompt@1"
CHECKER_VERSION = "1.0.0"
TECHNICAL_PROPOSAL_CHECKER_KEYS = MappingProxyType(
    {
        "required_sections": "core.required_sections.v1",
        "text_length": "core.text_length_range.v1",
        "hybrid_required": "generic.hybrid.required.v1",
        "required_fields": "technical_proposal.required_fields.v1",
    }
)
REQUIRED_METADATA_FIELDS = frozenset(
    {"proposal_id", "vendor_name", "project_name"}
)

_RISK_FIELDS = ("probability", "impact", "mitigation", "owner")
_FIELD_MARKERS = {
    "probability": ("概率", "可能性", "probability", "likelihood"),
    "impact": ("影响", "impact"),
    "mitigation": ("措施", "应对", "缓解", "mitigation", "response"),
    "owner": ("负责人", "责任人", "owner", "accountable"),
}
_MILESTONE_FIELDS = {
    "milestone": ("里程碑", "阶段", "milestone"),
    "deadline": ("截止", "日期", "时间", "deadline", "date"),
    "deliverable": ("交付物", "产出", "deliverable"),
    "owner": _FIELD_MARKERS["owner"],
}


def _exact_params(params, fields, label):
    if not isinstance(params, Mapping) or set(params) != set(fields):
        raise ValueError("%s requires exactly %s" % (label, sorted(fields)))


def _string_array(value, label):
    if (
        not isinstance(value, (list, tuple))
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError("%s must be a non-empty string array" % label)
    normalized = [item.strip() for item in value]
    if len(normalized) != len(set(normalized)):
        raise ValueError("%s must not contain duplicates" % label)
    return normalized


def _positive_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("%s must be a positive integer" % label)
    return value


def _validate_required_sections(params):
    _exact_params(params, {"required_sections"}, "required sections params")
    _string_array(params["required_sections"], "required_sections")


def _validate_text_length(params):
    _exact_params(
        params,
        {"minimum_chars", "maximum_chars"},
        "text length params",
    )
    minimum = _positive_int(params["minimum_chars"], "minimum_chars")
    maximum = _positive_int(params["maximum_chars"], "maximum_chars")
    if maximum < minimum:
        raise ValueError("maximum_chars must be at least minimum_chars")


def _validate_hybrid_required(params):
    _exact_params(params, set(), "hybrid required params")


def _validate_required_fields(params):
    _exact_params(params, {"required_fields"}, "required fields params")
    fields = _string_array(params["required_fields"], "required_fields")
    unknown = set(fields) - set(_RISK_FIELDS)
    if unknown:
        raise ValueError("required_fields contains unsupported fields")


def _observation(*, status, code, measured, expected, locator):
    return {
        "status": status,
        "observation_code": code,
        "finding_code": code,
        "measured_value": deepcopy(measured),
        "expected_value": deepcopy(expected),
        "locator": deepcopy(locator),
    }


def _result(*observations):
    return {
        "schema_version": "deterministic-checker-result@1",
        "observations": list(observations),
    }


def _required_sections_checker(*, document, params):
    headings = [
        str(item.get("heading") or "")
        for item in document.get("sections", ())
        if isinstance(item, Mapping)
    ]
    required = list(params["required_sections"])
    missing = [
        marker
        for marker in required
        if not any(marker.casefold() in item.casefold() for item in headings)
    ]
    return _result(
        _observation(
            status="triggered" if missing else "not_triggered",
            code="PROPOSAL_REQUIRED_SECTION_MISSING",
            measured={"headings": headings, "missing": missing},
            expected={"required_sections": required},
            locator={
                "kind": "document_structure",
                "structure_code": "technical_proposal.required_sections",
                "ordinal": 0,
            },
        )
    )


def _text_length_checker(*, document, params):
    measured = int(
        (document.get("metrics") or {}).get(
            "character_count",
            len(str(document.get("full_text") or "")),
        )
    )
    minimum = params["minimum_chars"]
    maximum = params["maximum_chars"]
    outside = measured < minimum or measured > maximum
    return _result(
        _observation(
            status="triggered" if outside else "not_triggered",
            code="PROPOSAL_TEXT_LENGTH_OUT_OF_RANGE",
            measured=measured,
            expected={"minimum_chars": minimum, "maximum_chars": maximum},
            locator={
                "kind": "document_structure",
                "structure_code": "technical_proposal.character_count",
                "ordinal": 0,
            },
        )
    )


def _extension(document):
    return (
        (document.get("profile_extensions") or {}).get(PROFILE_KEY, {})
        if isinstance(document, Mapping)
        else {}
    )


def _hybrid_required_checker(*, document, params):
    del params
    complete = _extension(document).get("milestone_complete") is True
    return _result(
        _observation(
            status="not_triggered" if complete else "triggered",
            code="PROPOSAL_MILESTONE_INCOMPLETE",
            measured=complete,
            expected=True,
            locator={
                "kind": "document_structure",
                "structure_code": "technical_proposal.milestones",
                "ordinal": 0,
            },
        )
    )


def _required_fields_checker(*, document, params):
    required = set(params["required_fields"])
    risk_items = list(_extension(document).get("risk_items", ()) or ())
    if not risk_items:
        risk_items = [
            {"item_ordinal": 0, "fields_present": [], "source_text": ""}
        ]
    observations = []
    for item in risk_items:
        present = set(item.get("fields_present", ()) or ())
        missing = sorted(required - present)
        observations.append(
            _observation(
                status="triggered" if missing else "not_triggered",
                code="PROPOSAL_REQUIRED_FIELD_MISSING",
                measured={"present": sorted(present), "missing": missing},
                expected={"required_fields": sorted(required)},
                locator={
                    "kind": "document_structure",
                    "structure_code": "technical_proposal.risk_item",
                    "ordinal": int(item.get("item_ordinal", 0)),
                },
            )
        )
    return _result(*observations)


def _registration(
    *,
    checker_key,
    behavior,
    params_schema,
    observation_codes,
):
    artifact_manifest = [
        {
            "path": "services/scoring/profiles/technical_proposal.py",
            "sha256": canonical_sha256(
                {
                    "scheme": "technical-proposal-checker-artifact-v1",
                    "behavior": behavior,
                }
            ),
        }
    ]
    package = {
        "scheme": "checker-package-sha256-v1",
        "checker_key": checker_key,
        "checker_version": CHECKER_VERSION,
        "entrypoint": "scoring.profiles.technical_proposal:" + behavior,
        "runtime_contract_version": "checker-runtime@1",
        "dependency_manifest_hash": canonical_sha256([]),
        "artifact_manifest": artifact_manifest,
    }
    return {
        "schema_version": "checker-registration@1",
        **package,
        "implementation_hash": canonical_sha256(package),
        "params_schema": params_schema,
        "supported_document_schemas": ["document-snapshot@1"],
        "supported_profiles": [PROFILE_KEY],
        "observation_schema": {
            "schema_version": "deterministic-observation-schema@1",
            "allowed_observation_codes": list(observation_codes),
            "allowed_finding_codes": list(observation_codes),
        },
    }


_CHECKERS = (
    (
        TECHNICAL_PROPOSAL_CHECKER_KEYS["required_sections"],
        _required_sections_checker,
        _validate_required_sections,
        "required_sections",
        "core-required-sections-params@1",
        ("PROPOSAL_REQUIRED_SECTION_MISSING",),
    ),
    (
        TECHNICAL_PROPOSAL_CHECKER_KEYS["text_length"],
        _text_length_checker,
        _validate_text_length,
        "text_length_range",
        "core-text-length-range-params@1",
        ("PROPOSAL_TEXT_LENGTH_OUT_OF_RANGE",),
    ),
    (
        TECHNICAL_PROPOSAL_CHECKER_KEYS["hybrid_required"],
        _hybrid_required_checker,
        _validate_hybrid_required,
        "hybrid_milestone_required",
        "generic-hybrid-required-params@1",
        ("PROPOSAL_MILESTONE_INCOMPLETE",),
    ),
    (
        TECHNICAL_PROPOSAL_CHECKER_KEYS["required_fields"],
        _required_fields_checker,
        _validate_required_fields,
        "risk_required_fields",
        "technical-proposal-required-fields-params@1",
        ("PROPOSAL_REQUIRED_FIELD_MISSING",),
    ),
)


def _metadata(value):
    metadata = dict(value or {})
    if set(metadata) != REQUIRED_METADATA_FIELDS or any(
        not isinstance(metadata[field], str) or not metadata[field].strip()
        for field in REQUIRED_METADATA_FIELDS
    ):
        raise ValueError(
            "technical proposal metadata requires exactly non-empty "
            "proposal_id, vendor_name and project_name"
        )
    return {field: metadata[field].strip() for field in sorted(metadata)}


def _contains(value, markers):
    text = str(value).casefold()
    return any(marker.casefold() in text for marker in markers)


def _table_facts(section, field_markers):
    table_rows = [
        block for block in section["blocks"] if block.kind == "table_row"
    ]
    if len(table_rows) < 2:
        return []
    headers = list(table_rows[0].cell_texts)
    positions = {}
    for field, markers in field_markers.items():
        positions[field] = next(
            (
                index
                for index, header in enumerate(headers)
                if _contains(header, markers)
            ),
            None,
        )
    result = []
    for ordinal, row in enumerate(table_rows[1:]):
        cells = list(row.cell_texts)
        present = sorted(
            field
            for field, index in positions.items()
            if index is not None and index < len(cells) and cells[index].strip()
        )
        result.append(
            {
                "item_ordinal": ordinal,
                "fields_present": present,
                "source_text": row.text,
            }
        )
    return result


def _normalized_sections(extracted_document):
    headings = set(extracted_document.heading_candidates)
    raw_sections = []
    current = None
    for block in sorted(extracted_document.blocks, key=lambda item: item.ordinal):
        if block.kind != "table_row" and block.text in headings:
            current = {"heading": block.text, "blocks": []}
            raw_sections.append(current)
            continue
        if current is None:
            current = {"heading": "正文", "blocks": []}
            raw_sections.append(current)
        current["blocks"].append(block)
    if not raw_sections:
        raise ValueError("technical proposal contains no extractable content")
    normalized = []
    for section in raw_sections:
        units = [block.text.strip() for block in section["blocks"] if block.text.strip()]
        normalized.append(
            {
                "section_path": [section["heading"]],
                "heading": section["heading"],
                "normalized_text": "\n".join(units),
                "units": [{"normalized_text": unit} for unit in units],
                "blocks": section["blocks"],
            }
        )
    return normalized


def _document_snapshot(extracted_document, *, profile_version):
    normalized = _normalized_sections(extracted_document)
    normalized_input = {
        "normalizer_version": "technical-proposal-normalizer@1",
        "sections": [
            {
                key: deepcopy(section[key])
                for key in (
                    "section_path",
                    "heading",
                    "normalized_text",
                    "units",
                )
            }
            for section in normalized
        ],
    }
    content_hash = hash_normalized_content(normalized_input)
    sections = []
    units = []
    for section_ordinal, section in enumerate(normalized):
        unit_ids = []
        for unit_ordinal, raw in enumerate(section["units"]):
            text = raw["normalized_text"]
            identity = {
                "normalized_content_hash": content_hash,
                "section_path": deepcopy(section["section_path"]),
                "section_ordinal": section_ordinal,
                "unit_ordinal": unit_ordinal,
                "normalized_text": text,
            }
            unit_id = derive_evidence_unit_id(identity)
            unit_ids.append(unit_id)
            units.append(
                {
                    **identity,
                    "evidence_unit_id": unit_id,
                    "unit_text_hash": hashlib.sha256(
                        text.encode("utf-8")
                    ).hexdigest(),
                    "locator": {
                        "kind": "text_span",
                        "evidence_unit_id": unit_id,
                        "start": 0,
                        "end": len(text),
                    },
                }
            )
        sections.append(
            {
                "section_path": deepcopy(section["section_path"]),
                "section_ordinal": section_ordinal,
                "heading": section["heading"],
                "normalized_text": section["normalized_text"],
                "evidence_unit_ids": unit_ids,
                "locator": {
                    "kind": "section",
                    "section_path": deepcopy(section["section_path"]),
                    "section_ordinal": section_ordinal,
                },
            }
        )

    risk_section = next(
        (item for item in normalized if "风险控制" in item["heading"]),
        None,
    )
    risk_items = (
        _table_facts(risk_section, _FIELD_MARKERS) if risk_section else []
    )
    implementation = next(
        (item for item in normalized if "实施计划" in item["heading"]),
        None,
    )
    milestones = (
        _table_facts(implementation, _MILESTONE_FIELDS)
        if implementation
        else []
    )
    milestone_complete = bool(milestones) and all(
        set(item["fields_present"]) == set(_MILESTONE_FIELDS)
        for item in milestones
    )
    full_text = "\n".join(item["normalized_text"] for item in normalized)
    content = {
        "schema_version": "document-snapshot@1",
        "profile_key": PROFILE_KEY,
        "profile_version": profile_version,
        "parser_version": "generic-document-extractor@1",
        "normalizer_version": normalized_input["normalizer_version"],
        "content_hash": content_hash,
        "full_text": full_text,
        "sections": sections,
        "evidence_units": units,
        "metrics": {
            "character_count": len(full_text),
            "section_count": len(sections),
            "risk_item_count": len(risk_items),
            "milestone_count": len(milestones),
        },
        "format_facts": {
            "source_media_type": extracted_document.media_type,
            "heading_count": len(extracted_document.heading_candidates),
        },
        "parse_quality": "1" if extracted_document.heading_candidates else "0.6",
        "parser_diagnostics": [],
        "profile_extensions": {
            PROFILE_KEY: {
                "section_headings": [item["heading"] for item in normalized],
                "risk_items": risk_items,
                "milestones": milestones,
                "milestone_complete": milestone_complete,
            }
        },
    }
    return DocumentSnapshot.from_mapping(
        {
            **content,
            "document_snapshot_hash": hash_document_snapshot(content),
        }
    )


def _evidence_for_sections(envelope, markers):
    units = envelope["evidence_units"]
    for unit in units:
        path = " / ".join(unit["section_path"])
        if any(marker.casefold() in path.casefold() for marker in markers):
            return unit
    return units[0] if units else None


class TechnicalProposalLLMRuntime:
    def __init__(self, scorer):
        self.scorer = scorer

    def score(self, *, envelope):
        explicit = getattr(self.scorer, "score_core_envelope", None)
        if callable(explicit):
            return explicit(envelope=envelope)
        if getattr(self.scorer, "provider", None) != "mock":
            raise LLMScoringError(
                "selected provider does not implement PromptEnvelopeV3 Core scoring"
            )
        value = envelope.to_mapping()
        rule = value["atomic_rule_snapshot"]
        criterion = value["criterion_snapshot"]
        extensions = value["profile_prompt_extensions"]["profile_extensions"]
        if rule["direction"] == "band":
            levels = sorted(
                rule["levels"],
                key=lambda item: (item["display_order"], item["level_code"]),
            )
            if not levels:
                return {}
            criterion_text = (
                criterion["criterion_code"] + " " + criterion["name"]
            ).casefold()
            if "feasibility" in criterion_text or "可行" in criterion_text:
                high = extensions.get("milestone_complete") is True
                markers = ("实施计划",)
            else:
                headings = extensions.get("section_headings", ())
                high = all(
                    any(marker in heading for heading in headings)
                    for marker in ("需求理解", "总体方案")
                ) and "逐项对应" in "\n".join(
                    unit["normalized_text"] for unit in value["evidence_units"]
                )
                markers = ("需求理解", "总体方案")
            unit = _evidence_for_sections(value, markers)
            if unit is None:
                return {}
            level = levels[0] if high else levels[-1]
            return {
                "schema_version": "semantic-rule-response@2",
                "rule_code": rule["rule_code"],
                "status": "triggered",
                "level_code": level["level_code"],
                "occurrences": [
                    {
                        "finding_code": rule["evidence_policy"][
                            "allowed_finding_codes"
                        ][0],
                        "evidence": [
                            {
                                "type": "source_quote",
                                "evidence_unit_id": unit["evidence_unit_id"],
                                "quote": unit["normalized_text"],
                            }
                        ],
                        "locator": deepcopy(unit["locator"]),
                    }
                ],
            }

        risk_items = list(extensions.get("risk_items", ()) or ())
        missing = []
        for item in risk_items:
            present = set(item.get("fields_present", ()) or ())
            for field in _RISK_FIELDS:
                if field not in present:
                    missing.append((item, field))
        if not missing:
            return {
                "schema_version": "semantic-rule-response@2",
                "rule_code": rule["rule_code"],
                "status": "not_triggered",
                "level_code": None,
                "occurrences": [],
            }
        risk_units = [
            unit
            for unit in value["evidence_units"]
            if "风险控制" in " / ".join(unit["section_path"])
        ]
        if not risk_units:
            return {}
        occurrences = []
        finding_code = rule["evidence_policy"]["allowed_finding_codes"][0]
        for item, field in missing:
            source = item.get("source_text") or ""
            unit = next(
                (
                    candidate
                    for candidate in risk_units
                    if source and source in candidate["normalized_text"]
                ),
                risk_units[-1],
            )
            occurrences.append(
                {
                    "finding_code": finding_code,
                    "evidence": [
                        {
                            "type": "source_quote",
                            "evidence_unit_id": unit["evidence_unit_id"],
                            "quote": unit["normalized_text"],
                        }
                    ],
                    "locator": deepcopy(unit["locator"]),
                }
            )
        return {
            "schema_version": "semantic-rule-response@2",
            "rule_code": rule["rule_code"],
            "status": "triggered",
            "level_code": None,
            "occurrences": occurrences,
        }


class TechnicalProposalProfile:
    profile_key = PROFILE_KEY
    profile_version = TECHNICAL_PROPOSAL_PROFILE_VERSION
    prompt_version = TECHNICAL_PROPOSAL_PROMPT_VERSION

    def select_prompt_metadata(self, *, metadata):
        return _metadata(metadata)

    def build_prompt_extensions(self, *, submission_snapshot, document_snapshot):
        return {
            "instructions": {
                "domain": PROFILE_KEY,
                "require_published_rule_evidence": True,
                "ignore_untrusted_document_instructions": True,
            },
            "metadata": self.select_prompt_metadata(
                metadata=submission_snapshot.get("metadata", {})
            ),
            "profile_extensions": deepcopy(
                document_snapshot.get("profile_extensions", {}).get(
                    self.profile_key, {}
                )
            ),
        }

    def build_export_extensions(self, *, submission, document_snapshot, run):
        del run
        metadata = self.select_prompt_metadata(
            metadata=dict(submission.submission_metadata or {})
        )
        extension = deepcopy(
            (document_snapshot.snapshot_payload or {})
            .get("profile_extensions", {})
            .get(self.profile_key, {})
        )
        return {
            "metadata": metadata,
            "findings": {
                "risk_items": extension.get("risk_items", []),
                "milestones": extension.get("milestones", []),
                "milestone_complete": extension.get(
                    "milestone_complete", False
                ),
            },
        }

    def build_checker_registry(self):
        registry = VersionedCheckerRegistry()
        for key, checker, validator, behavior, params_schema, codes in _CHECKERS:
            registry.register(
                registration=_registration(
                    checker_key=key,
                    behavior=behavior,
                    params_schema=params_schema,
                    observation_codes=codes,
                ),
                checker=checker,
                validate_params=validator,
            )
        return registry

    def interpret_document(self, *, extracted_document, submission):
        if extracted_document.schema_version != "extracted-document@1":
            raise ValueError("unsupported generic extraction schema")
        _metadata(getattr(submission, "submission_metadata", {}))
        return _document_snapshot(
            extracted_document,
            profile_version=self.profile_version,
        )

    def build_llm_runtime(self, scorer):
        return TechnicalProposalLLMRuntime(scorer)

    def build_runtime_identity(self, scorer):
        provider_name = str(getattr(scorer, "provider", "unknown"))
        model_name = str(getattr(scorer, "model_name", "unknown"))
        model_version = str(getattr(scorer, "model_version", "unknown"))
        artifact_projection = {
            "scheme": "technical-proposal-provider-artifact-v1",
            "provider": provider_name,
            "model": model_name,
            "model_version": model_version,
            "adapter": type(scorer).__module__
            + "."
            + type(scorer).__qualname__,
        }
        if provider_name != "mock":
            artifact_projection["provider_prompt_version"] = (
                CORE_PROVIDER_PROMPT_VERSION
            )
        artifact_hash = canonical_sha256(artifact_projection)
        return {
            "engine_contract_version": "scoring-core@1",
            "engine_version": "technical-proposal-core-adapter@1",
            "profile_key": self.profile_key,
            "profile_version": self.profile_version,
            "prompt_version": self.prompt_version,
            "provider": core_runtime_provider_contract(
                scorer,
                artifact_hash=artifact_hash,
            ),
            "calibration_anchors_hash": canonical_sha256([]),
        }


__all__ = [
    "CHECKER_VERSION",
    "PROFILE_KEY",
    "TECHNICAL_PROPOSAL_CHECKER_KEYS",
    "TECHNICAL_PROPOSAL_PROFILE_VERSION",
    "TECHNICAL_PROPOSAL_PROMPT_VERSION",
    "TechnicalProposalLLMRuntime",
    "TechnicalProposalProfile",
]
