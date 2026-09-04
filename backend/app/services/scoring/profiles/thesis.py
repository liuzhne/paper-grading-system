"""Production ThesisProfile adapters, checkers, prompt extensions and Mock.

The generic scoring Core knows only immutable snapshots, plans and ports.  All
thesis-specific metadata, parser projections and deterministic semantics live
here and are selected exclusively by versioned checker keys.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import re
from types import SimpleNamespace
from types import MappingProxyType

from backend.app.services.llm.base import LLMScoringError
from backend.app.services.llm.core_adapter import CORE_PROVIDER_PROMPT_VERSION
from backend.app.services.llm.core_adapter import core_runtime_provider_contract
from backend.app.services.checkers import run_deterministic_checker
from backend.app.services.scoring.adapters.legacy_paper import LegacyPaperAdapter
from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.checker_registry import (
    VersionedCheckerRegistry,
)
from backend.app.services.scoring.profiles.thesis_artifacts import (
    ThesisArtifactFacade,
)
from backend.app.services.document_parser.parser import interpret_thesis_document


THESIS_PROFILE_VERSION = "thesis-legacy-profile@1"
THESIS_PROMPT_VERSION = "2026-09-03-10"
CHECKER_VERSION = "1.0.0"
THESIS_CHECKER_KEYS = MappingProxyType(
    {
        "structure": "thesis.structure.required_sections.v1",
        "text_length": "thesis.text_length.range.v1",
        "figure_references": "thesis.figures.references.v1",
        "citations": "thesis.citations.bibliography.v1",
        "format": "thesis.format.constraints.v1",
        "research_conclusion": "thesis.coherence.research_conclusion.v1",
    }
)
_LEGACY_CHECKER_KEY = "thesis.legacy_required_fields.v1"
_FIGURE_REFERENCE = re.compile(r"(?:图|表)\s*[0-9]+", re.IGNORECASE)
_NUMBERED_CITATION = re.compile(r"\[[0-9]+(?:\s*[-,，]\s*[0-9]+)*\]")


def _exact_params(params, fields, label):
    if not isinstance(params, Mapping) or set(params) != set(fields):
        raise ValueError("%s requires exactly %s" % (label, sorted(fields)))


def _positive_int(value, label, *, allow_zero=False):
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError("%s must be a %s integer" % (label, qualifier))
    return value


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


def _validate_structure_params(params):
    _exact_params(params, {"required_sections"}, "structure checker params")
    _string_array(params["required_sections"], "required_sections")


def _validate_text_length_params(params):
    _exact_params(
        params,
        {"minimum_chars", "maximum_chars"},
        "text length checker params",
    )
    minimum = _positive_int(params["minimum_chars"], "minimum_chars")
    maximum = _positive_int(params["maximum_chars"], "maximum_chars")
    if maximum < minimum:
        raise ValueError("maximum_chars must be greater than or equal to minimum_chars")


def _validate_figure_params(params):
    _exact_params(
        params,
        {"minimum_references"},
        "figure reference checker params",
    )
    _positive_int(
        params["minimum_references"],
        "minimum_references",
        allow_zero=True,
    )


def _validate_citation_params(params):
    _exact_params(
        params,
        {"require_bibliography"},
        "citation checker params",
    )
    if not isinstance(params["require_bibliography"], bool):
        raise ValueError("require_bibliography must be boolean")


def _validate_format_params(params):
    _exact_params(params, {"required_facts"}, "format checker params")
    facts = params["required_facts"]
    if not isinstance(facts, Mapping) or not facts:
        raise ValueError("required_facts must be a non-empty object")
    if any(not isinstance(key, str) or not key.strip() for key in facts):
        raise ValueError("required_facts keys must be non-empty strings")


def _validate_coherence_params(params):
    fields = {"research_section_markers", "conclusion_section_markers"}
    _exact_params(params, fields, "research-conclusion checker params")
    for field in sorted(fields):
        _string_array(params[field], field)


def _validate_legacy_params(params):
    _exact_params(
        params,
        {"criterion_code", "applies_to"},
        "legacy checker params",
    )
    if any(not isinstance(value, str) or not value.strip() for value in params.values()):
        raise ValueError("criterion_code and applies_to must be non-empty strings")


def _observation(
    *,
    status,
    code,
    measured,
    expected,
    locator,
):
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


def _headings(document):
    return [
        str(item.get("heading") or "")
        for item in document.get("sections", ())
        if isinstance(item, Mapping)
    ]


def _structure_checker(*, document, params):
    required = list(params["required_sections"])
    headings = _headings(document)
    missing = [
        marker
        for marker in required
        if not any(marker.casefold() in heading.casefold() for heading in headings)
    ]
    return _result(
        _observation(
            status="triggered" if missing else "not_triggered",
            code="THESIS_REQUIRED_SECTION_MISSING",
            measured={"headings": headings, "missing": missing},
            expected={"required_sections": required},
            locator={"kind": "document_structure", "scope": "all_sections"},
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
            code="THESIS_TEXT_LENGTH_OUT_OF_RANGE",
            measured=measured,
            expected={"minimum_chars": minimum, "maximum_chars": maximum},
            locator={"kind": "document_metric", "metric": "character_count"},
        )
    )


def _figure_reference_checker(*, document, params):
    text = str(document.get("full_text") or "")
    references = sorted(set(_FIGURE_REFERENCE.findall(text)))
    minimum = params["minimum_references"]
    return _result(
        _observation(
            status="triggered" if len(references) < minimum else "not_triggered",
            code="THESIS_FIGURE_REFERENCE_INSUFFICIENT",
            measured={"count": len(references), "references": references},
            expected={"minimum_references": minimum},
            locator={"kind": "document_text", "scope": "full_text"},
        )
    )


def _citation_checker(*, document, params):
    if not params["require_bibliography"]:
        return _result(
            _observation(
                status="not_applicable",
                code="THESIS_CITATION_BIBLIOGRAPHY_MISSING",
                measured=None,
                expected={"require_bibliography": False},
                locator={"kind": "document_text", "scope": "full_text"},
            )
        )
    text = str(document.get("full_text") or "")
    has_citation = bool(_NUMBERED_CITATION.search(text))
    has_bibliography = any(
        marker in text.casefold()
        for marker in ("参考文献", "bibliography", "references")
    )
    valid = has_citation and has_bibliography
    return _result(
        _observation(
            status="not_triggered" if valid else "triggered",
            code="THESIS_CITATION_BIBLIOGRAPHY_MISSING",
            measured={
                "has_numbered_citation": has_citation,
                "has_bibliography": has_bibliography,
            },
            expected={
                "has_numbered_citation": True,
                "has_bibliography": True,
            },
            locator={"kind": "document_text", "scope": "full_text"},
        )
    )


def _format_checker(*, document, params):
    actual = document.get("format_facts") or {}
    observations = []
    for key, expected in sorted(params["required_facts"].items()):
        measured = actual.get(key)
        observations.append(
            _observation(
                status=(
                    "not_triggered" if measured == expected else "triggered"
                ),
                code="THESIS_FORMAT_FACT_MISMATCH",
                measured={"field": key, "value": measured},
                expected={"field": key, "value": expected},
                locator={"kind": "format_fact", "field": key},
            )
        )
    return _result(*observations)


def _research_conclusion_checker(*, document, params):
    headings = _headings(document)

    def found(markers):
        return any(
            marker.casefold() in heading.casefold()
            for marker in markers
            for heading in headings
        )

    research = found(params["research_section_markers"])
    conclusion = found(params["conclusion_section_markers"])
    complete = research and conclusion
    return _result(
        _observation(
            status="not_triggered" if complete else "triggered",
            code="THESIS_RESEARCH_CONCLUSION_STRUCTURE_INCOMPLETE",
            measured={
                "research_section_present": research,
                "conclusion_section_present": conclusion,
            },
            expected={
                "research_section_present": True,
                "conclusion_section_present": True,
            },
            locator={"kind": "document_structure", "scope": "all_sections"},
        )
    )


def _legacy_required_fields_checker(*, document, params):
    text = str(document.get("full_text") or "")
    present = any(marker in text for marker in ("负责人", "责任人", "owner"))
    return {
        "observation_code": "REQUIRED_FIELD_MISSING",
        "measured_value": present,
        "expected_value": True,
        "triggered": not present,
        "locator": {
            "kind": "document_structure",
            "structure_code": "required_owner:%s" % params["applies_to"],
            "ordinal": 0,
        },
    }


def _registration(checker_key, behavior, params_schema, observation_schema):
    artifacts = [
        {
            "path": "services/scoring/profiles/thesis.py",
            "sha256": canonical_sha256(
                {"scheme": "thesis-checker-artifact-v1", "behavior": behavior}
            ),
        }
    ]
    package = {
        "scheme": "checker-package-sha256-v1",
        "checker_key": checker_key,
        "checker_version": CHECKER_VERSION,
        "entrypoint": "scoring.profiles.thesis:" + behavior,
        "runtime_contract_version": "checker-runtime@1",
        "dependency_manifest_hash": canonical_sha256([]),
        "artifact_manifest": artifacts,
    }
    return {
        "schema_version": "checker-registration@1",
        **package,
        "implementation_hash": canonical_sha256(package),
        "params_schema": params_schema,
        "supported_document_schemas": ["document-snapshot@1"],
        "supported_profiles": ["thesis"],
        "observation_schema": observation_schema,
    }


_CHECKERS = (
    (
        THESIS_CHECKER_KEYS["structure"],
        _structure_checker,
        _validate_structure_params,
        "required_sections",
        "thesis-required-sections-params@1",
        "thesis-required-sections-observation@1",
    ),
    (
        THESIS_CHECKER_KEYS["text_length"],
        _text_length_checker,
        _validate_text_length_params,
        "text_length_range",
        "thesis-text-length-params@1",
        "thesis-text-length-observation@1",
    ),
    (
        THESIS_CHECKER_KEYS["figure_references"],
        _figure_reference_checker,
        _validate_figure_params,
        "figure_references",
        "thesis-figure-reference-params@1",
        "thesis-figure-reference-observation@1",
    ),
    (
        THESIS_CHECKER_KEYS["citations"],
        _citation_checker,
        _validate_citation_params,
        "citation_bibliography",
        "thesis-citation-params@1",
        "thesis-citation-observation@1",
    ),
    (
        THESIS_CHECKER_KEYS["format"],
        _format_checker,
        _validate_format_params,
        "format_constraints",
        "thesis-format-params@1",
        "thesis-format-observation@1",
    ),
    (
        THESIS_CHECKER_KEYS["research_conclusion"],
        _research_conclusion_checker,
        _validate_coherence_params,
        "research_conclusion",
        "thesis-research-conclusion-params@1",
        "thesis-research-conclusion-observation@1",
    ),
    (
        _LEGACY_CHECKER_KEY,
        _legacy_required_fields_checker,
        _validate_legacy_params,
        "legacy_required_fields",
        "legacy-required-fields-params@1",
        "legacy-required-fields-observation@1",
    ),
)


class ThesisLLMRuntime:
    """Profile-owned bridge from legacy scorers to the Core LLM port."""

    def __init__(self, scorer):
        self.scorer = scorer

    def score(self, *, envelope):
        from backend.app.services.llm_observability import observation

        value = envelope.to_mapping()
        rule = value["atomic_rule_snapshot"]
        with observation(
            "rule_scoring_task",
            as_type="chain",
            metadata={
                "criterion_code": rule.get("criterion_code"),
                "atomic_rule_code": rule["rule_code"],
                "prompt_schema_version": value.get("schema_version"),
                "document_snapshot_hash": (value.get("submission") or {}).get(
                    "document_snapshot_hash"
                ),
                "coverage_mode": value.get("coverage_mode"),
                "evidence_selection_hash": (
                    value.get("evidence_selection_identity") or {}
                ).get("selection_hash"),
                "token_policy_hash": (
                    value.get("token_budget_identity") or {}
                ).get("policy_hash"),
            },
        ):
            return self._score(envelope=envelope, value=value, rule=rule)

    def _score(self, *, envelope, value, rule):
        explicit = getattr(self.scorer, "score_core_envelope", None)
        if callable(explicit):
            return explicit(envelope=envelope)
        if getattr(self.scorer, "provider", None) != "mock":
            raise LLMScoringError(
                "selected provider does not implement PromptEnvelopeV3 Core scoring"
            )
        levels = sorted(
            rule["levels"],
            key=lambda item: (item["display_order"], item["level_code"]),
        )
        evidence_units = value["evidence_units"]
        if not levels or not evidence_units:
            return {}
        evidence = evidence_units[0]
        return {
            "schema_version": "semantic-rule-response@1",
            "rule_code": rule["rule_code"],
            "status": "triggered",
            "level_code": levels[0]["level_code"],
            "evidence": [
                {
                    "type": "source_quote",
                    "evidence_unit_id": evidence["evidence_unit_id"],
                    "quote": evidence["normalized_text"],
                    "location": " / ".join(evidence["section_path"]),
                }
            ],
        }

    def score_legacy_criterion(self, *, request, criterion):
        """Run the frozen legacy criterion through the old scorer interface.

        This is an observation port only: the compatibility executor validates
        ``direct_score`` and evidence, ignores provider point/effect fields and
        performs every authoritative calculation itself.
        """

        document = request["document"]
        candidates = [
            {
                "evidence_unit_id": unit["evidence_unit_id"],
                "chunk_id": unit["evidence_unit_id"],
                "text": unit["normalized_text"],
                "location": " / ".join(unit["section_path"]),
                "section_title": (
                    unit["section_path"][-1] if unit["section_path"] else ""
                ),
            }
            for unit in document["evidence_units"]
        ]
        extensions = (
            document.get("profile_extensions", {}).get("thesis", {})
        )
        structure_checks = list(extensions.get("structure_checks", []) or [])
        references = list(extensions.get("references", []) or [])
        criterion_object = SimpleNamespace(
            id=criterion["code"],
            code=criterion["code"],
            name=criterion["name"],
            max_score=criterion["max_score"],
            description=criterion["description"],
            evidence_hints=list(criterion["evidence_hints"]),
            criterion_type=criterion["criterion_type"],
            scoring_mode=criterion["scoring_mode"],
            applies_to=criterion["applies_to"],
            rubric_levels=[],
            authorized_rules=[],
            secure_execution=False,
        )
        paper = SimpleNamespace(
            id=request["submission"]["submission_id"],
            title=request["submission"].get("metadata", {}).get("title") or "",
            document_snapshot_hash=document["document_snapshot_hash"],
            normalized_content_hash=document["content_hash"],
        )
        if criterion["criterion_type"] == "deterministic":
            raw = run_deterministic_checker(
                criterion_object,
                {
                    "full_text": document["full_text"],
                    "sections": list(document["sections"]),
                    "structure_checks": structure_checks,
                    "references": references,
                },
            )
        else:
            raw = self.scorer.score_criterion(
                paper,
                criterion_object,
                candidates,
                structure_checks,
                [],
            )
        if not isinstance(raw, Mapping):
            return {}
        evidence = []
        by_id = {item["evidence_unit_id"]: item for item in candidates}
        for item in raw.get("evidence", ()) or ():
            if not isinstance(item, Mapping):
                continue
            unit_id = item.get("evidence_unit_id") or item.get("chunk_id")
            candidate = by_id.get(unit_id)
            quote = item.get("quote")
            if candidate is None or not isinstance(quote, str) or not quote.strip():
                continue
            evidence.append(
                {
                    "type": "source_quote",
                    "evidence_unit_id": unit_id,
                    "quote": quote,
                }
            )
        if criterion["criterion_type"] == "deterministic" and not evidence and candidates:
            evidence.append(
                {
                    "type": "source_quote",
                    "evidence_unit_id": candidates[0]["evidence_unit_id"],
                    "quote": candidates[0]["text"],
                }
            )
        return {
            "direct_score": raw.get("score"),
            "evidence": evidence,
            "need_manual_review": raw.get("need_manual_review") is True,
        }


class ThesisProfile:
    profile_key = "thesis"
    profile_version = THESIS_PROFILE_VERSION
    prompt_version = THESIS_PROMPT_VERSION

    def __init__(self):
        self._artifacts = ThesisArtifactFacade()

    def build_artifact_projection(self, **kwargs):
        return self._artifacts.build_artifact_projection(**kwargs)

    def build_spreadsheet_projection(self, **kwargs):
        return self._artifacts.build_spreadsheet_projection(**kwargs)

    def build_eval_run_identity(self, **kwargs):
        return self._artifacts.build_eval_run_identity(**kwargs)

    def assert_ordinary_item_override_allowed(self, **kwargs):
        return self._artifacts.assert_ordinary_item_override_allowed(**kwargs)

    def assert_review_submission_allowed(self, **kwargs):
        return self._artifacts.assert_review_submission_allowed(**kwargs)

    def select_prompt_metadata(self, *, metadata):
        allowed = {
            "student_id",
            "student_name",
            "title",
            "department",
            "major",
            "advisor",
        }
        return {
            key: deepcopy(value)
            for key, value in metadata.items()
            if key in allowed
        }

    def build_prompt_extensions(self, *, submission_snapshot, document_snapshot):
        return {
            "instructions": {
                "domain": "thesis",
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

    def build_provider_envelope(self, *, base_envelope):
        from backend.app.core.config import settings
        from backend.app.services.llm_observability import observation
        from backend.app.services.scoring.retrieval.selection import build_v4_envelope

        if settings.SCORING_PROMPT_ENVELOPE_VERSION == "v3":
            return base_envelope
        value = base_envelope.to_mapping()
        top_k = (
            len(value["evidence_units"])
            if settings.SCORING_EVIDENCE_SELECTION_MODE == "all"
            else settings.SCORING_EVIDENCE_TOP_K
        )
        with observation(
            "evidence_retrieval",
            metadata={
                "criterion_code": value["criterion_snapshot"]["criterion_code"],
                "rule_code": value["atomic_rule_snapshot"]["rule_code"],
                "candidate_count": len(value["evidence_units"]),
            },
        ) as retrieval:
            envelope = build_v4_envelope(
                base_envelope,
                context_window_tokens=settings.SCORING_CONTEXT_WINDOW_TOKENS,
                reserved_output_tokens=value["runtime_identity"]["provider"]["sampling"]["max_tokens"],
                safety_margin_tokens=settings.SCORING_CONTEXT_SAFETY_MARGIN_TOKENS,
                top_k=top_k,
            )
            selected = envelope.to_mapping()["evidence_selection_identity"]
            retrieval.update(
                output={
                    "selected_evidence_unit_ids": selected[
                        "selected_evidence_unit_ids"
                    ],
                    "selection_hash": selected["selection_hash"],
                }
            )
        with observation("token_preflight") as preflight:
            preflight.update(
                output=envelope.to_mapping()["token_budget_identity"]
            )
        return envelope

    def build_export_extensions(self, *, submission, document_snapshot, run):
        metadata = self.select_prompt_metadata(
            metadata=dict(submission.submission_metadata or {})
        )
        document_extensions = (
            (document_snapshot.snapshot_payload or {})
            .get("profile_extensions", {})
            .get(self.profile_key, {})
        )
        coherence = list(run.coherence_findings or []) or deepcopy(
            document_extensions.get("coherence_findings", [])
        )
        format_findings = list(run.format_findings or []) or deepcopy(
            document_extensions.get("format_findings", [])
        )
        return {
            "metadata": metadata,
            "findings": {
                "coherence": coherence,
                "format": format_findings,
                "references": deepcopy(
                    document_extensions.get("references", [])
                ),
            },
        }

    def build_checker_registry(self):
        registry = VersionedCheckerRegistry()
        for (
            key,
            checker,
            validator,
            behavior,
            params_schema,
            observation_schema,
        ) in _CHECKERS:
            registry.register(
                registration=_registration(
                    key,
                    behavior,
                    params_schema,
                    observation_schema,
                ),
                checker=checker,
                validate_params=validator,
            )
        return registry

    def adapt_paper(
        self,
        *,
        paper,
        parsed,
        chunks=(),
        source_artifact_hash,
        submission_instance_key=None,
    ):
        profile_extensions = {
            self.profile_key: {
                "structure_checks": deepcopy(
                    parsed.get("structure_checks", [])
                ),
                "references": deepcopy(parsed.get("references", [])),
                "coherence_findings": deepcopy(
                    parsed.get("coherence_findings", [])
                ),
                "format_findings": deepcopy(parsed.get("format_findings", [])),
            }
        }
        return LegacyPaperAdapter().adapt(
            paper=paper,
            parsed=parsed,
            chunks=chunks,
            source_artifact_hash=source_artifact_hash,
            profile_key=self.profile_key,
            profile_version=self.profile_version,
            parser_version="legacy-document-parser@1",
            normalizer_version="legacy-normalizer@1",
            submission_instance_key=submission_instance_key,
            profile_extensions=profile_extensions,
        )

    def interpret_document(self, *, extracted_document, submission):
        """Interpret a generic v2 extraction without constructing a Paper row."""

        parsed = interpret_thesis_document(extracted_document)
        parsed_mapping = parsed.to_dict()
        metadata = dict(getattr(submission, "submission_metadata", {}) or {})
        paper_projection = SimpleNamespace(
            **{
                field: metadata.get(field)
                for field in (
                    "student_id",
                    "student_name",
                    "title",
                    "department",
                    "major",
                    "advisor",
                )
            }
        )
        profile_extensions = {
            self.profile_key: {
                "structure_checks": deepcopy(
                    parsed_mapping.get("structure_checks", [])
                ),
                "references": deepcopy(parsed_mapping.get("references", [])),
                "coherence_findings": deepcopy(
                    parsed_mapping.get("coherence_findings", [])
                ),
                "format_findings": [],
            }
        }
        snapshots = LegacyPaperAdapter().adapt(
            paper=paper_projection,
            parsed=parsed_mapping,
            source_artifact_hash=submission.source_artifact_hash,
            profile_key=self.profile_key,
            profile_version=self.profile_version,
            parser_version="generic-document-extractor@1",
            normalizer_version="thesis-document-interpreter@1",
            submission_instance_key=submission.id,
            profile_extensions=profile_extensions,
        )
        return snapshots.document.to_mapping()

    def build_llm_runtime(self, scorer):
        return ThesisLLMRuntime(scorer)

    def build_runtime_identity(self, scorer):
        provider_name = str(getattr(scorer, "provider", "unknown"))
        model_name = str(getattr(scorer, "model_name", "unknown"))
        model_version = str(getattr(scorer, "model_version", "unknown"))
        artifact_projection = {
            "scheme": "thesis-core-provider-artifact-v1",
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
            artifact_projection["service_tier"] = getattr(
                scorer, "service_tier", None
            )
        provider_artifact = canonical_sha256(artifact_projection)
        return {
            "engine_contract_version": "scoring-core@1",
            "engine_version": "thesis-core-adapter@1",
            "profile_key": self.profile_key,
            "profile_version": self.profile_version,
            "prompt_version": self.prompt_version,
            "provider": core_runtime_provider_contract(
                scorer,
                artifact_hash=provider_artifact,
            ),
            "calibration_anchors_hash": canonical_sha256([]),
        }


__all__ = [
    "CHECKER_VERSION",
    "THESIS_CHECKER_KEYS",
    "THESIS_PROFILE_VERSION",
    "THESIS_PROMPT_VERSION",
    "ThesisLLMRuntime",
    "ThesisProfile",
]
