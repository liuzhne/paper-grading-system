from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from decimal import Decimal

from backend.app.core.config import settings
from backend.app.services.cache import llm_cache
from backend.app.services.scoring.core.canonical import canonical_json
from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.contracts import PromptEnvelopeV1


_MISSING = object()


def _field(value, *names, default=_MISSING):
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    if default is not _MISSING:
        return default
    raise ValueError("missing required PromptEnvelope field: %s" % "/".join(names))


def _plain(value):
    if value is None or isinstance(value, (str, bool, int, Decimal)):
        return value
    if isinstance(value, float):
        return format(Decimal(str(value)).normalize(), "f")
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "to_mapping"):
        return _plain(value.to_mapping())
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump(mode="python"))
    if hasattr(value, "__dataclass_fields__"):
        return {name: _plain(getattr(value, name)) for name in value.__dataclass_fields__}
    raise TypeError("cannot project %s into PromptEnvelope" % type(value).__name__)


def _decimal_text(value):
    decimal = Decimal(str(value))
    if decimal.is_zero():
        return "0"
    return format(decimal.normalize(), "f")


def _sha_or_derive(value, payload):
    if isinstance(value, str) and len(value) == 64:
        return value
    return canonical_sha256(payload)


def _criterion_from_snapshot(snapshot, criterion_code):
    criteria = _field(snapshot, "criteria", default=()) or ()
    for candidate in criteria:
        if str(_field(candidate, "code", default="")) == str(criterion_code):
            return candidate
    return None


def _authorized_rules(criterion, snapshot_criterion):
    rules = _field(criterion, "authorized_rules", default=None)
    if rules is None and snapshot_criterion is not None:
        rules = _field(snapshot_criterion, "authorized_rules", default=None)
    result = []
    for rule in rules or ():
        raw_evidence_mode = _field(rule, "evidence_mode", default=None)
        evidence_mode = (
            "review_only"
            if raw_evidence_mode in (None, "")
            else str(raw_evidence_mode).strip()
        )
        raw_absence_target = _field(rule, "absence_target", default=None)
        absence_target = (
            None
            if raw_absence_target in (None, "")
            else str(raw_absence_target).strip()
        )
        result.append(
            {
                "code": str(_field(rule, "code", "rule_ref")),
                "points": _decimal_text(_field(rule, "points")),
                "description": str(
                    _field(rule, "description", "match", "reason", default="") or ""
                ),
                "evidence_mode": evidence_mode,
                "absence_target": absence_target,
            }
        )
    return result


def _evidence_units(paper, candidates):
    units = []
    document_hint = _field(
        paper,
        "document_snapshot_hash",
        "normalized_content_hash",
        default="",
    )
    for index, candidate in enumerate(candidates or (), start=1):
        if not isinstance(candidate, Mapping):
            continue
        text = str(candidate.get("text") or "")
        location = candidate.get("location") or candidate.get("section_title") or ""
        if not isinstance(location, str):
            location = canonical_json(_plain(location))
        section_title = str(candidate.get("section_title") or location or "")
        unit_id = candidate.get("evidence_unit_id")
        unit_id = _sha_or_derive(
            unit_id,
            {
                "document_snapshot_hash": str(document_hint),
                "unit_ordinal": index - 1,
                "text": text,
                "location": location,
                "section_title": section_title,
            },
        )
        units.append(
            {
                "evidence_unit_id": unit_id,
                "text": text,
                "location": location,
                "section_title": section_title,
            }
        )
    return units


def _observations(structure_checks):
    result = []
    required = {
        "checker_key",
        "checker_version",
        "locator",
        "observation_code",
        "measured_value",
        "expected_value",
    }
    for index, check in enumerate(structure_checks or (), start=1):
        if not isinstance(check, Mapping):
            continue
        if required.issubset(check):
            content = {name: _plain(check[name]) for name in required}
        else:
            content = {
                "checker_key": str(check.get("checker_key") or "legacy.structure-check"),
                "checker_version": str(check.get("checker_version") or "legacy-v1"),
                "locator": _plain(check.get("locator") or {"kind": "legacy", "ordinal": index - 1}),
                "observation_code": str(
                    check.get("observation_code") or check.get("code") or "LEGACY_STRUCTURE_CHECK"
                ),
                "measured_value": _plain(
                    check.get("measured_value", check.get("actual", check.get("passed")))
                ),
                "expected_value": _plain(check.get("expected_value", check.get("expected", True))),
            }
        supplied_hash = check.get("payload_hash")
        payload_hash = supplied_hash if supplied_hash == canonical_sha256(content) else canonical_sha256(content)
        result.append({**content, "payload_hash": payload_hash})
    return result


def _calibration_anchors(anchors):
    result = []
    for anchor in anchors or ():
        if not isinstance(anchor, Mapping):
            raise TypeError("calibration anchor must be an object")
        result.append(
            {
                "label": str(anchor.get("label") or ""),
                "score": _decimal_text(anchor.get("score")),
                "max_score": _decimal_text(anchor.get("max_score")),
                "excerpt": str(anchor.get("excerpt") or ""),
                "rationale": str(anchor.get("rationale") or ""),
            }
        )
    return result


def _provider_contract(scorer):
    provider_name = str(getattr(scorer, "provider_name", None) or getattr(scorer, "provider", ""))
    is_openai = provider_name == "openai"
    if is_openai:
        temperature = settings.OPENAI_TEMPERATURE
        max_tokens = settings.OPENAI_MAX_OUTPUT_TOKENS
        thinking_type = None
        thinking_enabled = False
        response_format = "json_schema"
    else:
        temperature = settings.OPENAI_COMPATIBLE_TEMPERATURE
        max_tokens = settings.OPENAI_COMPATIBLE_MAX_TOKENS
        thinking_type = str(settings.OPENAI_COMPATIBLE_THINKING_TYPE or "").strip() or None
        thinking_enabled = str(thinking_type).lower() not in {
            "none",
            "disabled",
            "false",
            "off",
        }
        response_format = (
            "json_object" if settings.OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON else "none"
        )
    return {
        "name": provider_name,
        "model": str(getattr(scorer, "model_name", "")),
        "model_version": str(getattr(scorer, "model_version", "v1")),
        "sampling": {
            "temperature": _decimal_text(temperature),
            "top_p": "1",
            "seed": None,
            "max_tokens": int(max_tokens),
        },
        "thinking": {"enabled": thinking_enabled, "type": thinking_type},
        "response_format": response_format,
        "response_schema": "criterion-score-v2",
    }


def validated_envelope_provider(scorer, envelope):
    """Validate that an immutable envelope belongs to this exact adapter."""

    envelope = PromptEnvelopeV1.from_mapping(envelope)
    provider = envelope.to_mapping()["provider"]
    expected = {
        "name": str(
            getattr(scorer, "provider_name", None) or getattr(scorer, "provider", "")
        ),
        "model": str(getattr(scorer, "model_name", "")),
        "model_version": str(getattr(scorer, "model_version", "")),
    }
    mismatches = [name for name, value in expected.items() if provider[name] != value]
    if mismatches:
        raise ValueError(
            "PromptEnvelope provider identity does not match adapter: %s"
            % ", ".join(mismatches)
        )
    return envelope, provider


class LLMScorer(ABC):
    provider = "abstract"
    model_name = "abstract"
    model_version = "v1"

    @abstractmethod
    def score_criterion(self, paper, criterion, evidence_candidates, structure_checks, anchors=None):
        raise NotImplementedError

    def build_prompt_envelope(
        self,
        paper,
        criterion,
        evidence_candidates,
        structure_checks,
        rubric_snapshot,
        policy,
        anchors=None,
    ):
        """Build the one immutable object shared by cache and provider code."""

        criterion_code = str(_field(criterion, "code"))
        snapshot_criterion = _criterion_from_snapshot(rubric_snapshot, criterion_code)
        units = _evidence_units(paper, evidence_candidates)
        unit_ids = [unit["evidence_unit_id"] for unit in units]
        title = str(_field(paper, "title", default="") or "")
        normalized_content_hash = _sha_or_derive(
            _field(paper, "normalized_content_hash", default=None),
            [{"text": unit["text"], "location": unit["location"]} for unit in units],
        )
        source_artifact_hash = _sha_or_derive(
            _field(paper, "source_artifact_hash", "file_hash", default=None),
            {
                "title": title,
                "normalized_content_hash": normalized_content_hash,
                "source_kind": "legacy-paper-adapter",
            },
        )
        document_snapshot_hash = _sha_or_derive(
            _field(paper, "document_snapshot_hash", default=None),
            {
                "source_artifact_hash": source_artifact_hash,
                "normalized_content_hash": normalized_content_hash,
                "title": title,
            },
        )
        profile_key = str(_field(paper, "profile_key", default="thesis") or "thesis")
        profile_prompt_version = str(
            _field(
                paper,
                "profile_prompt_version",
                "profile_version",
                default="thesis-criterion-v1",
            )
            or "thesis-criterion-v1"
        )
        rubric_snapshot_hash = _sha_or_derive(
            _field(rubric_snapshot, "rubric_snapshot_hash", default=None),
            _plain(rubric_snapshot),
        )
        policy_hash = _sha_or_derive(
            _field(policy, "policy_hash", default=None),
            _plain(policy),
        )
        declared_scope = []
        for unit in units:
            scope = unit["section_title"] or unit["location"]
            if scope and scope not in declared_scope:
                declared_scope.append(scope)
        evidence_policy = _field(policy, "evidence", default={}) or {}
        evidence_policy_version = str(
            _field(
                evidence_policy,
                "policy_version",
                "version",
                "schema_version",
                default="evidence-policy-v1",
            )
        )
        calibration_anchors = _calibration_anchors(anchors)
        payload = {
            "schema_version": "prompt-envelope@1",
            "prompt_version": llm_cache.PROMPT_VERSION,
            "profile": {"key": profile_key, "prompt_version": profile_prompt_version},
            "engine": {"version": "legacy-m1-adapter-v1"},
            "provider": _provider_contract(self),
            "rubric_snapshot_hash": rubric_snapshot_hash,
            "policy_hash": policy_hash,
            "criterion": {
                "code": criterion_code,
                "name": str(_field(criterion, "name")),
                "max_score": _decimal_text(_field(criterion, "max_score")),
                "scoring_mode": str(_field(criterion, "scoring_mode", default="llm_direct")),
                "description": str(_field(criterion, "description", default="") or ""),
                "evidence_hints": [
                    str(item)
                    for item in (_field(criterion, "evidence_hints", default=()) or ())
                ],
                "deduction_rules": [
                    str(item)
                    for item in (_field(criterion, "deduction_rules", default=()) or ())
                ],
                "rubric_levels": _plain(
                    list(_field(criterion, "rubric_levels", default=()) or ())
                ),
                "authorized_rules": _authorized_rules(criterion, snapshot_criterion),
            },
            "submission": {
                "title": title,
                "source_artifact_hash": source_artifact_hash,
                "normalized_content_hash": normalized_content_hash,
                "document_snapshot_hash": document_snapshot_hash,
            },
            "evidence_units": units,
            "observations": _observations(structure_checks),
            "calibration_anchors": calibration_anchors,
            "calibration_anchors_hash": canonical_sha256(calibration_anchors),
            "coverage": {
                "declared_scope": declared_scope,
                "scope_selector": "retrieval://candidate-units",
                "authoritative_for_absence": False,
                "expected_evidence_unit_ids": unit_ids,
                "checked_evidence_unit_ids": unit_ids,
                "completeness": "complete",
                "evidence_policy_version": evidence_policy_version,
            },
        }
        return PromptEnvelopeV1.from_mapping(payload)

    def score_envelope(self, envelope):
        raise NotImplementedError

    def complete_json(self, instructions, payload):
        """通用结构化 JSON 补全原语（供语义一致性、L2 等复用）。默认未实现。"""
        raise NotImplementedError


class LLMScoringError(RuntimeError):
    pass
