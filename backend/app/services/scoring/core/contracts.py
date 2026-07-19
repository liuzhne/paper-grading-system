"""Immutable provider-ready contracts introduced by M1.

``PromptEnvelopeV1`` owns the exact payload seen by both the cache and the
provider adapter.  It accepts a plain mapping at the boundary, validates its
closed schema, normalizes fields with set semantics, and keeps a recursively
immutable copy internally.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.identity import (
    derive_evidence_unit_id,
    hash_document_snapshot,
    hash_normalized_content,
)
from backend.app.services.scoring.core.policy import compile_scoring_policy


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MISSING = object()

_TOP_LEVEL_FIELDS = {
    "schema_version",
    "prompt_version",
    "profile",
    "engine",
    "provider",
    "rubric_snapshot_hash",
    "policy_hash",
    "criterion",
    "submission",
    "evidence_units",
    "observations",
    "calibration_anchors",
    "calibration_anchors_hash",
    "coverage",
}


def _assert_closed_mapping(value, *, fields, path, required=None):
    if not isinstance(value, Mapping):
        raise TypeError("%s must be an object" % path)
    keys = set(value)
    if any(not isinstance(key, str) for key in keys):
        raise TypeError("%s keys must be strings" % path)
    unexpected = keys - set(fields)
    if unexpected:
        raise ValueError("%s contains unknown fields: %s" % (path, sorted(unexpected)))
    missing = set(fields if required is None else required) - keys
    if missing:
        raise ValueError("%s is missing fields: %s" % (path, sorted(missing)))


def _assert_text(value, path, *, allow_empty=False):
    if not isinstance(value, str):
        raise TypeError("%s must be a string" % path)
    if not allow_empty and not value.strip():
        raise ValueError("%s must not be empty" % path)
    return value


def _assert_sha256(value, path):
    value = _assert_text(value, path)
    if not _SHA256.fullmatch(value):
        raise ValueError("%s must be a lowercase SHA-256 digest" % path)
    return value


def _decimal_text(value, path, *, positive=False):
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("%s must use an exact decimal representation" % path)
    if not isinstance(value, (str, int, Decimal)):
        raise TypeError("%s must be a decimal string, Decimal, or integer" % path)
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("%s is not a valid decimal" % path) from exc
    if not decimal.is_finite():
        raise ValueError("%s must be finite" % path)
    if positive and decimal <= 0:
        raise ValueError("%s must be positive" % path)
    if decimal.is_zero():
        return "0"
    return format(decimal.normalize(), "f")


def _assert_json_value(value, path):
    """Validate an explicitly schema-owned JSON subtree without coercion."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise TypeError("%s must not contain floats" % path)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("%s must not contain non-finite decimals" % path)
        return _decimal_text(value, path)
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("%s keys must be strings" % path)
            result[key] = _assert_json_value(item, "%s.%s" % (path, key))
        return result
    if isinstance(value, (list, tuple)):
        return [_assert_json_value(item, "%s[%s]" % (path, index)) for index, item in enumerate(value)]
    raise TypeError("%s contains unsupported value %s" % (path, type(value).__name__))


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _normalize_profile(value):
    fields = {"key", "prompt_version"}
    _assert_closed_mapping(value, fields=fields, path="profile")
    return {
        "key": _assert_text(value["key"], "profile.key"),
        "prompt_version": _assert_text(value["prompt_version"], "profile.prompt_version"),
    }


def _normalize_engine(value):
    fields = {"version"}
    _assert_closed_mapping(value, fields=fields, path="engine")
    return {"version": _assert_text(value["version"], "engine.version")}


def _normalize_provider(value):
    fields = {
        "name",
        "model",
        "model_version",
        "sampling",
        "thinking",
        "response_format",
        "response_schema",
    }
    _assert_closed_mapping(value, fields=fields, path="provider")
    sampling_fields = {"temperature", "top_p", "seed", "max_tokens"}
    _assert_closed_mapping(value["sampling"], fields=sampling_fields, path="provider.sampling")
    sampling = value["sampling"]
    temperature = _decimal_text(sampling["temperature"], "provider.sampling.temperature")
    top_p = _decimal_text(sampling["top_p"], "provider.sampling.top_p")
    seed = sampling["seed"]
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise TypeError("provider.sampling.seed must be an integer or null")
    max_tokens = sampling["max_tokens"]
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("provider.sampling.max_tokens must be a positive integer")

    _assert_closed_mapping(
        value["thinking"],
        fields={"enabled", "type"},
        path="provider.thinking",
    )
    thinking_enabled = value["thinking"]["enabled"]
    if not isinstance(thinking_enabled, bool):
        raise TypeError("provider.thinking.enabled must be boolean")
    thinking_type = value["thinking"]["type"]
    if thinking_type is not None:
        thinking_type = _assert_text(thinking_type, "provider.thinking.type")
    disabled_thinking_types = {None, "disabled", "none", "false", "off"}
    if thinking_enabled != (str(thinking_type).strip().lower() not in disabled_thinking_types):
        raise ValueError("provider.thinking.enabled must match provider.thinking.type")
    response_format = _assert_text(value["response_format"], "provider.response_format")
    if response_format not in {"none", "json_object", "json_schema"}:
        raise ValueError("provider.response_format is unsupported")
    return {
        "name": _assert_text(value["name"], "provider.name"),
        "model": _assert_text(value["model"], "provider.model"),
        "model_version": _assert_text(value["model_version"], "provider.model_version"),
        "sampling": {
            "temperature": temperature,
            "top_p": top_p,
            "seed": seed,
            "max_tokens": max_tokens,
        },
        "thinking": {"enabled": thinking_enabled, "type": thinking_type},
        "response_format": response_format,
        "response_schema": _assert_text(value["response_schema"], "provider.response_schema"),
    }


def _normalize_text_array(value, path):
    if not isinstance(value, (list, tuple)):
        raise TypeError("%s must be an array" % path)
    return [
        _assert_text(item, "%s[%s]" % (path, index), allow_empty=False)
        for index, item in enumerate(value)
    ]


def _normalize_rubric_levels(value):
    if not isinstance(value, (list, tuple)):
        raise TypeError("criterion.rubric_levels must be an array")
    normalized = []
    for index, level in enumerate(value):
        path = "criterion.rubric_levels[%s]" % index
        if not isinstance(level, Mapping):
            raise TypeError("%s must be an object" % path)
        projected = _assert_json_value(level, path)
        if not projected:
            raise ValueError("%s must not be empty" % path)
        normalized.append(projected)
    return normalized


def _normalize_criterion(value):
    fields = {
        "code",
        "name",
        "max_score",
        "scoring_mode",
        "description",
        "evidence_hints",
        "deduction_rules",
        "rubric_levels",
        "authorized_rules",
    }
    _assert_closed_mapping(value, fields=fields, path="criterion")
    code = _assert_text(value["code"], "criterion.code")
    rules = value["authorized_rules"]
    if not isinstance(rules, (list, tuple)):
        raise TypeError("criterion.authorized_rules must be an array")
    normalized_rules = []
    for index, rule in enumerate(rules):
        path = "criterion.authorized_rules[%s]" % index
        rule_fields = {
            "code",
            "points",
            "description",
            "evidence_mode",
            "absence_target",
        }
        _assert_closed_mapping(rule, fields=rule_fields, path=path)
        rule_code = _assert_text(rule["code"], path + ".code")
        if rule_code.startswith("LEGACY:") and not rule_code.startswith("LEGACY:%s:" % code):
            raise ValueError("%s.code is scoped to another criterion" % path)
        evidence_mode = _assert_text(
            rule["evidence_mode"], path + ".evidence_mode"
        )
        if evidence_mode not in {
            "source_quote",
            "scoped_absence",
            "review_only",
        }:
            raise ValueError("%s.evidence_mode is unsupported" % path)
        absence_target = rule["absence_target"]
        if evidence_mode == "scoped_absence":
            absence_target = _assert_text(
                absence_target, path + ".absence_target"
            )
        elif absence_target is not None:
            raise ValueError(
                "%s.absence_target is only valid for scoped_absence" % path
            )
        normalized_rules.append(
            {
                "code": rule_code,
                "points": _decimal_text(rule["points"], path + ".points", positive=True),
                "description": _assert_text(rule["description"], path + ".description", allow_empty=True),
                "evidence_mode": evidence_mode,
                "absence_target": absence_target,
            }
        )
    return {
        "code": code,
        "name": _assert_text(value["name"], "criterion.name"),
        "max_score": _decimal_text(value["max_score"], "criterion.max_score", positive=True),
        "scoring_mode": _assert_text(value["scoring_mode"], "criterion.scoring_mode"),
        "description": _assert_text(
            value["description"], "criterion.description", allow_empty=True
        ),
        "evidence_hints": _normalize_text_array(
            value["evidence_hints"], "criterion.evidence_hints"
        ),
        "deduction_rules": _normalize_text_array(
            value["deduction_rules"], "criterion.deduction_rules"
        ),
        "rubric_levels": _normalize_rubric_levels(value["rubric_levels"]),
        "authorized_rules": normalized_rules,
    }


def _normalize_submission(value):
    fields = {"title", "source_artifact_hash", "normalized_content_hash", "document_snapshot_hash"}
    _assert_closed_mapping(value, fields=fields, path="submission")
    return {
        "title": _assert_text(value["title"], "submission.title", allow_empty=True),
        "source_artifact_hash": _assert_sha256(value["source_artifact_hash"], "submission.source_artifact_hash"),
        "normalized_content_hash": _assert_sha256(value["normalized_content_hash"], "submission.normalized_content_hash"),
        "document_snapshot_hash": _assert_sha256(value["document_snapshot_hash"], "submission.document_snapshot_hash"),
    }


def _normalize_evidence_units(value):
    if not isinstance(value, (list, tuple)):
        raise TypeError("evidence_units must be an array")
    result = []
    seen = set()
    fields = {"evidence_unit_id", "text", "location", "section_title"}
    for index, unit in enumerate(value):
        path = "evidence_units[%s]" % index
        _assert_closed_mapping(unit, fields=fields, path=path)
        unit_id = _assert_sha256(unit["evidence_unit_id"], path + ".evidence_unit_id")
        if unit_id in seen:
            raise ValueError("evidence_units contains duplicate evidence_unit_id")
        seen.add(unit_id)
        result.append(
            {
                "evidence_unit_id": unit_id,
                "text": _assert_text(unit["text"], path + ".text", allow_empty=True),
                "location": _assert_text(unit["location"], path + ".location", allow_empty=True),
                "section_title": _assert_text(unit["section_title"], path + ".section_title", allow_empty=True),
            }
        )
    return result, seen


def _normalize_observations(value):
    if not isinstance(value, (list, tuple)):
        raise TypeError("observations must be an array")
    result = []
    fields = {
        "checker_key",
        "checker_version",
        "locator",
        "observation_code",
        "measured_value",
        "expected_value",
        "payload_hash",
    }
    for index, observation in enumerate(value):
        path = "observations[%s]" % index
        _assert_closed_mapping(observation, fields=fields, path=path)
        normalized = {
            "checker_key": _assert_text(observation["checker_key"], path + ".checker_key"),
            "checker_version": _assert_text(observation["checker_version"], path + ".checker_version"),
            "locator": _assert_json_value(observation["locator"], path + ".locator"),
            "observation_code": _assert_text(observation["observation_code"], path + ".observation_code"),
            "measured_value": _assert_json_value(observation["measured_value"], path + ".measured_value"),
            "expected_value": _assert_json_value(observation["expected_value"], path + ".expected_value"),
        }
        payload_hash = _assert_sha256(observation["payload_hash"], path + ".payload_hash")
        if canonical_sha256(normalized) != payload_hash:
            raise ValueError("%s.payload_hash does not match observation content" % path)
        result.append({**normalized, "payload_hash": payload_hash})
    return result


def _normalize_calibration_anchors(value):
    if not isinstance(value, (list, tuple)):
        raise TypeError("calibration_anchors must be an array")
    fields = {"label", "score", "max_score", "excerpt", "rationale"}
    result = []
    for index, anchor in enumerate(value):
        path = "calibration_anchors[%s]" % index
        _assert_closed_mapping(anchor, fields=fields, path=path)
        score = _decimal_text(anchor["score"], path + ".score")
        maximum = _decimal_text(anchor["max_score"], path + ".max_score", positive=True)
        if Decimal(score) < 0 or Decimal(score) > Decimal(maximum):
            raise ValueError("%s.score must be within 0..max_score" % path)
        result.append(
            {
                "label": _assert_text(anchor["label"], path + ".label", allow_empty=True),
                "score": score,
                "max_score": maximum,
                "excerpt": _assert_text(
                    anchor["excerpt"], path + ".excerpt", allow_empty=True
                ),
                "rationale": _assert_text(
                    anchor["rationale"], path + ".rationale", allow_empty=True
                ),
            }
        )
    return result


def _normalize_id_set(value, path):
    if not isinstance(value, (list, tuple)):
        raise TypeError("%s must be an array" % path)
    normalized = [_assert_sha256(item, "%s[%s]" % (path, index)) for index, item in enumerate(value)]
    if len(set(normalized)) != len(normalized):
        raise ValueError("%s contains duplicate IDs" % path)
    return sorted(normalized)


def _normalize_coverage(value, evidence_unit_ids):
    required = {
        "declared_scope",
        "scope_selector",
        "authoritative_for_absence",
        "expected_evidence_unit_ids",
        "checked_evidence_unit_ids",
        "completeness",
        "evidence_policy_version",
    }
    fields = required | {"expected_evidence_unit_ids_hash", "checked_evidence_unit_ids_hash"}
    _assert_closed_mapping(value, fields=fields, required=required, path="coverage")
    declared_scope = value["declared_scope"]
    if not isinstance(declared_scope, (list, tuple)):
        raise TypeError("coverage.declared_scope must be an array")
    normalized_scope = [
        _assert_text(item, "coverage.declared_scope[%s]" % index, allow_empty=False)
        for index, item in enumerate(declared_scope)
    ]
    scope_selector = _assert_text(value["scope_selector"], "coverage.scope_selector")
    authoritative_for_absence = value["authoritative_for_absence"]
    if not isinstance(authoritative_for_absence, bool):
        raise TypeError("coverage.authoritative_for_absence must be boolean")
    if authoritative_for_absence and scope_selector.startswith("retrieval://"):
        raise ValueError("retrieval candidate coverage cannot authorize absence")
    expected = _normalize_id_set(value["expected_evidence_unit_ids"], "coverage.expected_evidence_unit_ids")
    checked = _normalize_id_set(value["checked_evidence_unit_ids"], "coverage.checked_evidence_unit_ids")
    if not set(expected).issubset(evidence_unit_ids):
        raise ValueError("coverage.expected_evidence_unit_ids must belong to this document snapshot")
    if not set(checked).issubset(set(expected)):
        raise ValueError("coverage.checked_evidence_unit_ids must be a subset of expected IDs")
    completeness = _assert_text(value["completeness"], "coverage.completeness")
    if completeness not in {"complete", "partial"}:
        raise ValueError("coverage.completeness must be complete or partial")
    if completeness == "complete" and set(checked) != set(expected):
        raise ValueError("complete coverage requires every expected evidence unit to be checked")
    if completeness == "partial" and set(checked) == set(expected):
        raise ValueError("partial coverage must omit at least one expected evidence unit")
    if authoritative_for_absence and completeness != "complete":
        raise ValueError("absence-authoritative coverage must be complete")
    expected_hash = canonical_sha256(expected)
    checked_hash = canonical_sha256(checked)
    for supplied, calculated, field in (
        (value.get("expected_evidence_unit_ids_hash", _MISSING), expected_hash, "expected_evidence_unit_ids_hash"),
        (value.get("checked_evidence_unit_ids_hash", _MISSING), checked_hash, "checked_evidence_unit_ids_hash"),
    ):
        if supplied is not _MISSING and supplied != calculated:
            raise ValueError("coverage.%s does not match its canonical ID set" % field)
    return {
        "declared_scope": normalized_scope,
        "scope_selector": scope_selector,
        "authoritative_for_absence": authoritative_for_absence,
        "expected_evidence_unit_ids": expected,
        "checked_evidence_unit_ids": checked,
        "expected_evidence_unit_ids_hash": expected_hash,
        "checked_evidence_unit_ids_hash": checked_hash,
        "completeness": completeness,
        "evidence_policy_version": _assert_text(
            value["evidence_policy_version"], "coverage.evidence_policy_version"
        ),
    }


def _normalize_envelope(value):
    _assert_closed_mapping(value, fields=_TOP_LEVEL_FIELDS, path="PromptEnvelopeV1")
    if value["schema_version"] != "prompt-envelope@1":
        raise ValueError("unsupported PromptEnvelope schema_version")
    evidence_units, evidence_unit_ids = _normalize_evidence_units(value["evidence_units"])
    calibration_anchors = _normalize_calibration_anchors(value["calibration_anchors"])
    calibration_anchors_hash = _assert_sha256(
        value["calibration_anchors_hash"], "calibration_anchors_hash"
    )
    if canonical_sha256(calibration_anchors) != calibration_anchors_hash:
        raise ValueError("calibration_anchors_hash does not match calibration_anchors")
    normalized = {
        "schema_version": "prompt-envelope@1",
        "prompt_version": _assert_text(value["prompt_version"], "prompt_version"),
        "profile": _normalize_profile(value["profile"]),
        "engine": _normalize_engine(value["engine"]),
        "provider": _normalize_provider(value["provider"]),
        "rubric_snapshot_hash": _assert_sha256(value["rubric_snapshot_hash"], "rubric_snapshot_hash"),
        "policy_hash": _assert_sha256(value["policy_hash"], "policy_hash"),
        "criterion": _normalize_criterion(value["criterion"]),
        "submission": _normalize_submission(value["submission"]),
        "evidence_units": evidence_units,
        "observations": _normalize_observations(value["observations"]),
        "calibration_anchors": calibration_anchors,
        "calibration_anchors_hash": calibration_anchors_hash,
        "coverage": _normalize_coverage(value["coverage"], evidence_unit_ids),
    }
    return normalized


def _normalize_envelope_v2(value):
    """Validate the provider envelope that carries Profile-owned extensions."""

    fields = set(_TOP_LEVEL_FIELDS) | {"profile_prompt_extensions"}
    _assert_closed_mapping(value, fields=fields, path="PromptEnvelopeV2")
    if value["schema_version"] != "prompt-envelope@2":
        raise ValueError("unsupported PromptEnvelope schema_version")
    extensions = value["profile_prompt_extensions"]
    if not isinstance(extensions, Mapping):
        raise TypeError("profile_prompt_extensions must be an object")

    # Reuse the complete V1 provider contract, changing only the envelope
    # schema discriminator.  This prevents V2 from drifting on evidence,
    # coverage, provider, policy, or calibration identity validation.
    base_payload = dict(value)
    base_payload.pop("profile_prompt_extensions")
    base_payload["schema_version"] = "prompt-envelope@1"
    normalized = _normalize_envelope(base_payload)
    normalized["schema_version"] = "prompt-envelope@2"
    normalized["profile_prompt_extensions"] = _assert_json_value(
        extensions,
        "profile_prompt_extensions",
    )
    return normalized


class PromptEnvelopeV1:
    """A recursively immutable, canonicalizable provider request."""

    __slots__ = ("_data", "_sealed")

    def __init__(self, **payload):
        object.__setattr__(self, "_data", _freeze(_normalize_envelope(payload)))
        object.__setattr__(self, "_sealed", True)

    @classmethod
    def from_mapping(cls, payload):
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            raise TypeError("PromptEnvelopeV1 requires a mapping")
        return cls(**dict(payload))

    @classmethod
    def model_validate(cls, payload):
        return cls.from_mapping(payload)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("PromptEnvelopeV1 is immutable")
        object.__setattr__(self, name, value)

    def __getattr__(self, name):
        data = object.__getattribute__(self, "_data")
        try:
            return data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def to_mapping(self):
        return _thaw(self._data)

    def model_dump(self, *, mode=None):
        del mode
        return self.to_mapping()

    def dict(self):
        return self.to_mapping()

    def canonical_hash(self):
        return canonical_sha256(self.to_mapping())

    def __repr__(self):
        return "PromptEnvelopeV1(%r)" % self.to_mapping()


class PromptEnvelopeV2(PromptEnvelopeV1):
    """Provider request with explicitly selected business-profile fields."""

    __slots__ = ()

    def __init__(self, **payload):
        object.__setattr__(self, "_data", _freeze(_normalize_envelope_v2(payload)))
        object.__setattr__(self, "_sealed", True)

    @classmethod
    def from_mapping(cls, payload):
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            raise TypeError("PromptEnvelopeV2 requires a mapping")
        return cls(**dict(payload))

    def __repr__(self):
        return "PromptEnvelopeV2(%r)" % self.to_mapping()


def _assert_boolean(value, path):
    if not isinstance(value, bool):
        raise TypeError("%s must be boolean" % path)
    return value


def _assert_integer(value, path, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("%s must be an integer" % path)
    if minimum is not None and value < minimum:
        raise ValueError("%s must be at least %s" % (path, minimum))
    return value


def _assert_optional_text(value, path):
    if value is None:
        return None
    return _assert_text(value, path)


def _assert_optional_decimal(value, path):
    if value is None:
        return None
    return _decimal_text(value, path)


def _assert_mapping_array(value, path, normalizer):
    if not isinstance(value, (list, tuple)):
        raise TypeError("%s must be an array" % path)
    return [normalizer(item, "%s[%s]" % (path, index)) for index, item in enumerate(value)]


def _assert_text_array_preserving_order(value, path):
    if not isinstance(value, (list, tuple)):
        raise TypeError("%s must be an array" % path)
    return [
        _assert_text(item, "%s[%s]" % (path, index))
        for index, item in enumerate(value)
    ]


def _normalize_section_path(value, path):
    """Apply the locator-v1 NFC+trim rule to a non-empty section path."""

    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("%s must be a non-empty array" % path)
    normalized = []
    for index, item in enumerate(value):
        text = _assert_text(item, "%s[%s]" % (path, index))
        text = unicodedata.normalize("NFC", text).strip()
        if not text:
            raise ValueError("%s[%s] must not be empty" % (path, index))
        normalized.append(text)
    return normalized


def _normalize_artifact_ref(value, path):
    fields = {"kind", "ref", "content_hash"}
    _assert_closed_mapping(value, fields=fields, path=path)
    ref = _assert_text(value["ref"], path + ".ref")
    scheme_match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", ref)
    if (
        scheme_match is None
        or scheme_match.group(1).casefold() == "file"
        or len(scheme_match.group(1)) == 1
        or "\\" in ref
    ):
        raise ValueError(
            "%s.ref must be a non-file artifact URI with an explicit scheme" % path
        )
    return {
        "kind": _assert_text(value["kind"], path + ".kind"),
        "ref": ref,
        "content_hash": _assert_sha256(value["content_hash"], path + ".content_hash"),
    }


def _normalize_submission_snapshot(value):
    fields = {
        "schema_version",
        "submission_id",
        "profile_key",
        "source_artifact_hash",
        "metadata",
        "artifact_refs",
    }
    _assert_closed_mapping(value, fields=fields, path="SubmissionSnapshot")
    if value["schema_version"] != "submission-snapshot@1":
        raise ValueError("unsupported SubmissionSnapshot schema_version")
    metadata = _assert_json_value(value["metadata"], "SubmissionSnapshot.metadata")
    if not isinstance(metadata, dict):
        raise TypeError("SubmissionSnapshot.metadata must be an object")
    artifact_refs = _assert_mapping_array(
        value["artifact_refs"],
        "SubmissionSnapshot.artifact_refs",
        _normalize_artifact_ref,
    )
    source_hash = _assert_sha256(
        value["source_artifact_hash"], "SubmissionSnapshot.source_artifact_hash"
    )
    source_refs = [item for item in artifact_refs if item["kind"] == "source"]
    if source_refs and any(item["content_hash"] != source_hash for item in source_refs):
        raise ValueError("SubmissionSnapshot source artifact identity does not match")
    return {
        "schema_version": "submission-snapshot@1",
        "submission_id": _assert_text(value["submission_id"], "SubmissionSnapshot.submission_id"),
        "profile_key": _assert_text(value["profile_key"], "SubmissionSnapshot.profile_key"),
        "source_artifact_hash": source_hash,
        "metadata": metadata,
        "artifact_refs": artifact_refs,
    }


def _normalize_section_locator(value, path, *, section_path, section_ordinal):
    fields = {"kind", "section_path", "section_ordinal"}
    _assert_closed_mapping(value, fields=fields, path=path)
    if value["kind"] != "section":
        raise ValueError("%s.kind must be section" % path)
    normalized_path = _normalize_section_path(
        value["section_path"], path + ".section_path"
    )
    normalized_ordinal = _assert_integer(
        value["section_ordinal"], path + ".section_ordinal", minimum=0
    )
    if normalized_path != section_path or normalized_ordinal != section_ordinal:
        raise ValueError("%s does not identify its section" % path)
    return {
        "kind": "section",
        "section_path": normalized_path,
        "section_ordinal": normalized_ordinal,
    }


def _normalize_document_section(value, path):
    fields = {
        "section_path",
        "section_ordinal",
        "heading",
        "normalized_text",
        "evidence_unit_ids",
        "locator",
    }
    _assert_closed_mapping(value, fields=fields, path=path)
    section_path = _normalize_section_path(
        value["section_path"], path + ".section_path"
    )
    section_ordinal = _assert_integer(
        value["section_ordinal"], path + ".section_ordinal", minimum=0
    )
    evidence_unit_ids = [
        _assert_sha256(item, "%s.evidence_unit_ids[%s]" % (path, index))
        for index, item in enumerate(value["evidence_unit_ids"])
    ] if isinstance(value["evidence_unit_ids"], (list, tuple)) else None
    if evidence_unit_ids is None:
        raise TypeError("%s.evidence_unit_ids must be an array" % path)
    if len(evidence_unit_ids) != len(set(evidence_unit_ids)):
        raise ValueError("%s.evidence_unit_ids contains duplicate identities" % path)
    return {
        "section_path": section_path,
        "section_ordinal": section_ordinal,
        "heading": _assert_text(value["heading"], path + ".heading", allow_empty=True),
        "normalized_text": _assert_text(
            value["normalized_text"], path + ".normalized_text", allow_empty=True
        ),
        "evidence_unit_ids": evidence_unit_ids,
        "locator": _normalize_section_locator(
            value["locator"],
            path + ".locator",
            section_path=section_path,
            section_ordinal=section_ordinal,
        ),
    }


def _normalize_text_span_locator(value, path, *, evidence_unit_id, text_length):
    fields = {"kind", "evidence_unit_id", "start", "end"}
    _assert_closed_mapping(value, fields=fields, path=path)
    if value["kind"] != "text_span":
        raise ValueError("%s.kind must be text_span" % path)
    locator_id = _assert_sha256(value["evidence_unit_id"], path + ".evidence_unit_id")
    start = _assert_integer(value["start"], path + ".start", minimum=0)
    end = _assert_integer(value["end"], path + ".end", minimum=0)
    if locator_id != evidence_unit_id:
        raise ValueError("%s evidence identity does not match" % path)
    if start >= end or end > text_length:
        raise ValueError("%s span is outside its evidence text" % path)
    return {
        "kind": "text_span",
        "evidence_unit_id": locator_id,
        "start": start,
        "end": end,
    }


def _normalize_document_evidence_unit(value, path):
    fields = {
        "normalized_content_hash",
        "section_path",
        "section_ordinal",
        "unit_ordinal",
        "normalized_text",
        "evidence_unit_id",
        "unit_text_hash",
        "locator",
    }
    _assert_closed_mapping(value, fields=fields, path=path)
    normalized = {
        "normalized_content_hash": _assert_sha256(
            value["normalized_content_hash"], path + ".normalized_content_hash"
        ),
        "section_path": _normalize_section_path(
            value["section_path"], path + ".section_path"
        ),
        "section_ordinal": _assert_integer(
            value["section_ordinal"], path + ".section_ordinal", minimum=0
        ),
        "unit_ordinal": _assert_integer(
            value["unit_ordinal"], path + ".unit_ordinal", minimum=0
        ),
        "normalized_text": _assert_text(
            value["normalized_text"], path + ".normalized_text"
        ),
    }
    evidence_unit_id = _assert_sha256(
        value["evidence_unit_id"], path + ".evidence_unit_id"
    )
    expected_id = derive_evidence_unit_id(normalized)
    if evidence_unit_id != expected_id:
        raise ValueError("%s evidence identity hash does not match its content" % path)
    unit_text_hash = _assert_sha256(value["unit_text_hash"], path + ".unit_text_hash")
    expected_text_hash = hashlib.sha256(
        normalized["normalized_text"].encode("utf-8")
    ).hexdigest()
    if unit_text_hash != expected_text_hash:
        raise ValueError("%s.unit_text_hash does not match normalized_text" % path)
    return {
        **normalized,
        "evidence_unit_id": evidence_unit_id,
        "unit_text_hash": unit_text_hash,
        "locator": _normalize_text_span_locator(
            value["locator"],
            path + ".locator",
            evidence_unit_id=evidence_unit_id,
            text_length=len(normalized["normalized_text"]),
        ),
    }


def _normalize_parser_diagnostic(value, path):
    fields = {"code", "severity", "message", "scoring_relevant"}
    _assert_closed_mapping(value, fields=fields, path=path)
    return {
        "code": _assert_text(value["code"], path + ".code"),
        "severity": _assert_text(value["severity"], path + ".severity"),
        "message": _assert_text(value["message"], path + ".message", allow_empty=True),
        "scoring_relevant": _assert_boolean(
            value["scoring_relevant"], path + ".scoring_relevant"
        ),
    }


def _normalize_document_snapshot(value):
    fields = {
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
        "document_snapshot_hash",
    }
    _assert_closed_mapping(value, fields=fields, path="DocumentSnapshot")
    if value["schema_version"] != "document-snapshot@1":
        raise ValueError("unsupported DocumentSnapshot schema_version")
    sections = _assert_mapping_array(
        value["sections"], "DocumentSnapshot.sections", _normalize_document_section
    )
    evidence_units = _assert_mapping_array(
        value["evidence_units"],
        "DocumentSnapshot.evidence_units",
        _normalize_document_evidence_unit,
    )
    diagnostics = _assert_mapping_array(
        value["parser_diagnostics"],
        "DocumentSnapshot.parser_diagnostics",
        _normalize_parser_diagnostic,
    )
    metrics = _assert_json_value(value["metrics"], "DocumentSnapshot.metrics")
    format_facts = _assert_json_value(
        value["format_facts"], "DocumentSnapshot.format_facts"
    )
    profile_extensions = _assert_json_value(
        value["profile_extensions"], "DocumentSnapshot.profile_extensions"
    )
    for name, item in (
        ("metrics", metrics),
        ("format_facts", format_facts),
        ("profile_extensions", profile_extensions),
    ):
        if not isinstance(item, dict):
            raise TypeError("DocumentSnapshot.%s must be an object" % name)
    normalized = {
        "schema_version": "document-snapshot@1",
        "profile_key": _assert_text(value["profile_key"], "DocumentSnapshot.profile_key"),
        "profile_version": _assert_text(
            value["profile_version"], "DocumentSnapshot.profile_version"
        ),
        "parser_version": _assert_text(
            value["parser_version"], "DocumentSnapshot.parser_version"
        ),
        "normalizer_version": _assert_text(
            value["normalizer_version"], "DocumentSnapshot.normalizer_version"
        ),
        "content_hash": _assert_sha256(value["content_hash"], "DocumentSnapshot.content_hash"),
        "full_text": _assert_text(value["full_text"], "DocumentSnapshot.full_text", allow_empty=True),
        "sections": sections,
        "evidence_units": evidence_units,
        "metrics": metrics,
        "format_facts": format_facts,
        "parse_quality": _decimal_text(value["parse_quality"], "DocumentSnapshot.parse_quality"),
        "parser_diagnostics": diagnostics,
        "profile_extensions": profile_extensions,
    }
    expected_full_text = "\n".join(
        section["normalized_text"] for section in sections
    )
    if normalized["full_text"] != expected_full_text:
        raise ValueError(
            "DocumentSnapshot full_text does not match canonical section content"
        )
    if [section["section_ordinal"] for section in sections] != list(range(len(sections))):
        raise ValueError("DocumentSnapshot section ordinals must be contiguous document order")
    by_section = {}
    seen_unit_ids = set()
    expected_section_ordinals = set(range(len(sections)))
    global_unit_order = [
        (unit["section_ordinal"], unit["unit_ordinal"])
        for unit in evidence_units
    ]
    if global_unit_order != sorted(global_unit_order):
        raise ValueError(
            "DocumentSnapshot evidence_units must use canonical document order"
        )
    for unit in evidence_units:
        if unit["section_ordinal"] not in expected_section_ordinals:
            raise ValueError("DocumentSnapshot contains an orphan evidence unit")
        if unit["normalized_content_hash"] != normalized["content_hash"]:
            raise ValueError("DocumentSnapshot evidence normalized content hash mismatch")
        if unit["evidence_unit_id"] in seen_unit_ids:
            raise ValueError("DocumentSnapshot contains duplicate evidence identity")
        seen_unit_ids.add(unit["evidence_unit_id"])
        by_section.setdefault(unit["section_ordinal"], []).append(unit)
    normalized_content_sections = []
    for section in sections:
        units = by_section.get(section["section_ordinal"], [])
        if [unit["unit_ordinal"] for unit in units] != list(range(len(units))):
            raise ValueError("DocumentSnapshot unit ordinals must be contiguous section order")
        if any(unit["section_path"] != section["section_path"] for unit in units):
            raise ValueError("DocumentSnapshot evidence section path mismatch")
        unit_ids = [unit["evidence_unit_id"] for unit in units]
        if unit_ids != section["evidence_unit_ids"]:
            raise ValueError("DocumentSnapshot section evidence identities do not match units")
        normalized_content_sections.append(
            {
                "section_path": section["section_path"],
                "heading": section["heading"],
                "normalized_text": section["normalized_text"],
                "units": [{"normalized_text": unit["normalized_text"]} for unit in units],
            }
        )
    normalized_content_input = {
        "normalizer_version": normalized["normalizer_version"],
        "sections": normalized_content_sections,
    }
    if hash_normalized_content(normalized_content_input) != normalized["content_hash"]:
        raise ValueError("DocumentSnapshot content hash does not match normalized content")
    snapshot_hash = _assert_sha256(
        value["document_snapshot_hash"], "DocumentSnapshot.document_snapshot_hash"
    )
    if hash_document_snapshot(normalized) != snapshot_hash:
        raise ValueError("DocumentSnapshot hash does not match snapshot identity")
    return {**normalized, "document_snapshot_hash": snapshot_hash}


def _normalize_rule_evidence_policy(value, path):
    fields = {"mode", "requirement", "minimum_coverage"}
    _assert_closed_mapping(value, fields=fields, path=path)
    return {
        "mode": _assert_text(value["mode"], path + ".mode"),
        "requirement": _assert_text(value["requirement"], path + ".requirement"),
        "minimum_coverage": _decimal_text(
            value["minimum_coverage"], path + ".minimum_coverage"
        ),
    }


def _normalize_rule_level(value, path):
    fields = {
        "level_code",
        "points",
        "descriptor",
        "positive_example",
        "negative_example",
        "display_order",
    }
    _assert_closed_mapping(value, fields=fields, path=path)
    return {
        "level_code": _assert_text(value["level_code"], path + ".level_code"),
        "points": _decimal_text(value["points"], path + ".points"),
        "descriptor": _assert_text(value["descriptor"], path + ".descriptor", allow_empty=True),
        "positive_example": _assert_optional_text(value["positive_example"], path + ".positive_example"),
        "negative_example": _assert_optional_text(value["negative_example"], path + ".negative_example"),
        "display_order": _assert_integer(value["display_order"], path + ".display_order", minimum=0),
    }


def _normalize_atomic_rule_snapshot(value):
    fields = {
        "schema_version",
        "rule_code",
        "criterion_code",
        "direction",
        "effect_type",
        "judge_type",
        "checker_key",
        "checker_version",
        "checker_params",
        "evidence_policy",
        "max_points",
        "repeat_policy",
        "cap_points",
        "depends_on_rule_codes",
        "mutex_group",
        "levels",
    }
    _assert_closed_mapping(value, fields=fields, path="AtomicRuleSnapshot")
    if value["schema_version"] != "atomic-rule-snapshot@1":
        raise ValueError("unsupported AtomicRuleSnapshot schema_version")
    checker_params = _assert_json_value(
        value["checker_params"], "AtomicRuleSnapshot.checker_params"
    )
    if not isinstance(checker_params, dict):
        raise TypeError("AtomicRuleSnapshot.checker_params must be an object")
    depends_on = _assert_text_array_preserving_order(
        value["depends_on_rule_codes"], "AtomicRuleSnapshot.depends_on_rule_codes"
    )
    if len(depends_on) != len(set(depends_on)):
        raise ValueError("AtomicRuleSnapshot dependency codes must be unique")
    levels = _assert_mapping_array(
        value["levels"], "AtomicRuleSnapshot.levels", _normalize_rule_level
    )
    checker_key = _assert_optional_text(
        value["checker_key"], "AtomicRuleSnapshot.checker_key"
    )
    checker_version = _assert_optional_text(
        value["checker_version"], "AtomicRuleSnapshot.checker_version"
    )
    max_points = _assert_optional_decimal(
        value["max_points"], "AtomicRuleSnapshot.max_points"
    )
    repeat_policy = _assert_optional_text(
        value["repeat_policy"], "AtomicRuleSnapshot.repeat_policy"
    )
    judge_type = _assert_text(value["judge_type"], "AtomicRuleSnapshot.judge_type")
    if judge_type == "deterministic" and checker_key is None:
        raise ValueError("AtomicRuleSnapshot deterministic rule requires checker_key")
    if judge_type == "semantic" and checker_key is not None:
        raise ValueError("AtomicRuleSnapshot semantic rule must not declare checker_key")
    return {
        "schema_version": "atomic-rule-snapshot@1",
        "rule_code": _assert_text(value["rule_code"], "AtomicRuleSnapshot.rule_code"),
        "criterion_code": _assert_text(
            value["criterion_code"], "AtomicRuleSnapshot.criterion_code"
        ),
        "direction": _assert_text(value["direction"], "AtomicRuleSnapshot.direction"),
        "effect_type": _assert_text(value["effect_type"], "AtomicRuleSnapshot.effect_type"),
        "judge_type": judge_type,
        "checker_key": checker_key,
        "checker_version": checker_version,
        "checker_params": checker_params,
        "evidence_policy": _normalize_rule_evidence_policy(
            value["evidence_policy"], "AtomicRuleSnapshot.evidence_policy"
        ),
        "max_points": max_points,
        "repeat_policy": repeat_policy,
        "cap_points": _assert_optional_decimal(
            value["cap_points"], "AtomicRuleSnapshot.cap_points"
        ),
        "depends_on_rule_codes": depends_on,
        "mutex_group": _assert_optional_text(
            value["mutex_group"], "AtomicRuleSnapshot.mutex_group"
        ),
        "levels": levels,
    }


def _normalize_criterion_snapshot(value, path):
    fields = {"criterion_code", "name", "max_score", "weight", "assessment_mode"}
    _assert_closed_mapping(value, fields=fields, path=path)
    return {
        "criterion_code": _assert_text(value["criterion_code"], path + ".criterion_code"),
        "name": _assert_text(value["name"], path + ".name"),
        "max_score": _decimal_text(value["max_score"], path + ".max_score", positive=True),
        "weight": _assert_optional_decimal(value["weight"], path + ".weight"),
        "assessment_mode": _assert_text(value["assessment_mode"], path + ".assessment_mode"),
    }


def _normalize_grade_band(value, path):
    fields = {"label", "minimum"}
    _assert_closed_mapping(value, fields=fields, path=path)
    return {
        "label": _assert_text(value["label"], path + ".label"),
        "minimum": _decimal_text(value["minimum"], path + ".minimum"),
    }


def _normalize_policy_snapshot(value, path):
    fields = {
        "schema_version",
        "policy_key",
        "usage",
        "aggregation",
        "rounding",
        "grade_scale",
        "review",
        "evidence",
        "failure",
        "legacy",
        "policy_hash",
    }
    _assert_closed_mapping(value, fields=fields, path=path)
    if value["schema_version"] != "scoring-policy@1":
        raise ValueError("unsupported scoring policy schema_version")
    _assert_closed_mapping(
        value["aggregation"], fields={"mode", "total_score"}, path=path + ".aggregation"
    )
    _assert_closed_mapping(
        value["rounding"], fields={"mode", "digits"}, path=path + ".rounding"
    )
    _assert_closed_mapping(
        value["grade_scale"], fields={"basis", "bands"}, path=path + ".grade_scale"
    )
    _assert_closed_mapping(
        value["review"],
        fields={
            "total_below",
            "grade_boundary_tolerance",
            "confidence_below",
            "parse_quality_below",
            "on_invalid_evidence",
        },
        path=path + ".review",
    )
    _assert_closed_mapping(
        value["review"]["grade_boundary_tolerance"],
        fields={"value", "unit"},
        path=path + ".review.grade_boundary_tolerance",
    )
    _assert_closed_mapping(
        value["evidence"],
        fields={
            "schema_version",
            "default_policy",
            "requirement",
            "minimum_valid_items",
            "allowed_types",
            "review_on_optional_invalid",
            "absence",
        },
        path=path + ".evidence",
    )
    _assert_closed_mapping(
        value["evidence"]["absence"],
        fields={
            "enabled",
            "policy_version",
            "allowed_targets",
            "complete_scope_selectors",
        },
        path=path + ".evidence.absence",
    )
    _assert_closed_mapping(
        value["failure"],
        fields={"unknown_checker", "rule_conflict", "semantic_error"},
        path=path + ".failure",
    )
    _assert_closed_mapping(
        value["legacy"],
        fields={"allow_legacy_direct_compat", "force_review"},
        path=path + ".legacy",
    )
    tolerance = value["review"]["grade_boundary_tolerance"]
    absence = value["evidence"]["absence"]
    normalized = {
        "schema_version": "scoring-policy@1",
        "policy_key": _assert_text(value["policy_key"], path + ".policy_key"),
        "usage": _assert_text(value["usage"], path + ".usage"),
        "aggregation": {
            "mode": _assert_text(value["aggregation"]["mode"], path + ".aggregation.mode"),
            "total_score": _decimal_text(
                value["aggregation"]["total_score"], path + ".aggregation.total_score", positive=True
            ),
        },
        "rounding": {
            "mode": _assert_text(value["rounding"]["mode"], path + ".rounding.mode"),
            "digits": _assert_integer(value["rounding"]["digits"], path + ".rounding.digits", minimum=0),
        },
        "grade_scale": {
            "basis": _assert_text(value["grade_scale"]["basis"], path + ".grade_scale.basis"),
            "bands": _assert_mapping_array(
                value["grade_scale"]["bands"], path + ".grade_scale.bands", _normalize_grade_band
            ),
        },
        "review": {
            "total_below": _decimal_text(value["review"]["total_below"], path + ".review.total_below"),
            "grade_boundary_tolerance": {
                "value": _decimal_text(tolerance["value"], path + ".review.grade_boundary_tolerance.value"),
                "unit": _assert_text(tolerance["unit"], path + ".review.grade_boundary_tolerance.unit"),
            },
            "confidence_below": _decimal_text(value["review"]["confidence_below"], path + ".review.confidence_below"),
            "parse_quality_below": _decimal_text(value["review"]["parse_quality_below"], path + ".review.parse_quality_below"),
            "on_invalid_evidence": _assert_text(value["review"]["on_invalid_evidence"], path + ".review.on_invalid_evidence"),
        },
        "evidence": {
            "schema_version": _assert_text(value["evidence"]["schema_version"], path + ".evidence.schema_version"),
            "default_policy": _assert_text(value["evidence"]["default_policy"], path + ".evidence.default_policy"),
            "requirement": _assert_text(value["evidence"]["requirement"], path + ".evidence.requirement"),
            "minimum_valid_items": _assert_integer(value["evidence"]["minimum_valid_items"], path + ".evidence.minimum_valid_items", minimum=0),
            "allowed_types": _assert_text_array_preserving_order(value["evidence"]["allowed_types"], path + ".evidence.allowed_types"),
            "review_on_optional_invalid": _assert_boolean(value["evidence"]["review_on_optional_invalid"], path + ".evidence.review_on_optional_invalid"),
            "absence": {
                "enabled": _assert_boolean(absence["enabled"], path + ".evidence.absence.enabled"),
                "policy_version": _assert_text(absence["policy_version"], path + ".evidence.absence.policy_version"),
                "allowed_targets": _assert_text_array_preserving_order(absence["allowed_targets"], path + ".evidence.absence.allowed_targets"),
                "complete_scope_selectors": _assert_text_array_preserving_order(absence["complete_scope_selectors"], path + ".evidence.absence.complete_scope_selectors"),
            },
        },
        "failure": {
            key: _assert_text(value["failure"][key], path + ".failure." + key)
            for key in ("unknown_checker", "rule_conflict", "semantic_error")
        },
        "legacy": {
            "allow_legacy_direct_compat": _assert_boolean(value["legacy"]["allow_legacy_direct_compat"], path + ".legacy.allow_legacy_direct_compat"),
            "force_review": _assert_boolean(value["legacy"]["force_review"], path + ".legacy.force_review"),
        },
    }
    policy_hash = _assert_sha256(value["policy_hash"], path + ".policy_hash")
    if canonical_sha256(normalized) != policy_hash:
        raise ValueError("%s.policy_hash does not match frozen policy snapshot" % path)
    normalized_with_hash = {**normalized, "policy_hash": policy_hash}
    # Reuse the authoritative M1 policy compiler so DTO construction cannot
    # admit a hash-consistent but semantically meaningless policy snapshot.
    # The compiler also independently verifies the supplied policy hash.
    compiled = compile_scoring_policy(
        normalized_with_hash,
        total_score=normalized["aggregation"]["total_score"],
    )
    compiled_mapping = compiled.to_mapping()
    if compiled_mapping != normalized_with_hash:
        raise ValueError("%s is not the canonical compiled scoring policy" % path)
    return compiled_mapping


def _normalize_compiled_rubric_snapshot(value):
    base_fields = {
        "schema_version",
        "rubric_source_kind",
        "rubric_snapshot_hash",
        "business_profile_key",
        "total_score",
        "criteria",
        "atomic_rules",
        "global_policy",
    }
    published_fields = {"rubric_version_id", "version_hash", "hash_scheme"}
    if not isinstance(value, Mapping):
        raise TypeError("CompiledRubricSnapshot must be an object")
    if "rubric_source_kind" not in value:
        raise ValueError("CompiledRubricSnapshot is missing required rubric_source_kind")
    source_kind = value.get("rubric_source_kind")
    if source_kind == "published_version":
        fields = base_fields | published_fields
    elif source_kind == "legacy_unversioned":
        fields = base_fields
    else:
        raise ValueError("CompiledRubricSnapshot rubric source kind is unsupported")
    _assert_closed_mapping(value, fields=fields, path="CompiledRubricSnapshot")
    if value["schema_version"] != "compiled-rubric-snapshot@1":
        raise ValueError("unsupported CompiledRubricSnapshot schema_version")
    criteria = _assert_mapping_array(
        value["criteria"], "CompiledRubricSnapshot.criteria", _normalize_criterion_snapshot
    )
    atomic_rules = []
    if not isinstance(value["atomic_rules"], (list, tuple)):
        raise TypeError("CompiledRubricSnapshot.atomic_rules must be an array")
    for item in value["atomic_rules"]:
        atomic_rules.append(_normalize_atomic_rule_snapshot(item))
    global_policy = _normalize_policy_snapshot(
        value["global_policy"], "CompiledRubricSnapshot.global_policy"
    )
    total_score = _decimal_text(
        value["total_score"], "CompiledRubricSnapshot.total_score", positive=True
    )
    if global_policy["aggregation"]["total_score"] != total_score:
        raise ValueError(
            "CompiledRubricSnapshot total_score does not match global policy"
        )
    normalized = {
        "schema_version": "compiled-rubric-snapshot@1",
        "rubric_source_kind": source_kind,
        "rubric_snapshot_hash": _assert_sha256(
            value["rubric_snapshot_hash"], "CompiledRubricSnapshot.rubric_snapshot_hash"
        ),
        "business_profile_key": _assert_text(
            value["business_profile_key"], "CompiledRubricSnapshot.business_profile_key"
        ),
        "total_score": total_score,
        "criteria": criteria,
        "atomic_rules": atomic_rules,
        "global_policy": global_policy,
    }
    if source_kind == "published_version":
        normalized.update(
            {
                "rubric_version_id": _assert_text(
                    value["rubric_version_id"], "CompiledRubricSnapshot.rubric_version_id"
                ),
                "version_hash": _assert_sha256(value["version_hash"], "CompiledRubricSnapshot.version_hash"),
                "hash_scheme": _assert_text(value["hash_scheme"], "CompiledRubricSnapshot.hash_scheme"),
            }
        )
    return normalized


def _normalize_plan_node(value, path):
    if not isinstance(value, Mapping):
        raise TypeError("%s must be an object" % path)
    if value.get("node_kind") != "atomic_rule":
        raise ValueError("%s node kind is unsupported by rule-execution-plan@1" % path)
    fields = {"node_kind", "criterion_code", "rule_code"}
    _assert_closed_mapping(value, fields=fields, path=path)
    return {
        "node_kind": "atomic_rule",
        "criterion_code": _assert_text(value["criterion_code"], path + ".criterion_code"),
        "rule_code": _assert_text(value["rule_code"], path + ".rule_code"),
    }


def _normalize_plan_node_v2(value, path):
    fields = {
        "node_kind",
        "criterion_code",
        "rule_code",
        "criterion_snapshot",
        "atomic_rule_snapshot",
    }
    _assert_closed_mapping(value, fields=fields, path=path)
    if value.get("node_kind") != "atomic_rule":
        raise ValueError("%s node kind is unsupported by rule-execution-plan@2" % path)
    criterion = _normalize_criterion_snapshot(
        value["criterion_snapshot"], path + ".criterion_snapshot"
    )
    rule = _normalize_atomic_rule_snapshot(value["atomic_rule_snapshot"])
    criterion_code = _assert_text(value["criterion_code"], path + ".criterion_code")
    rule_code = _assert_text(value["rule_code"], path + ".rule_code")
    if criterion_code != criterion["criterion_code"] or criterion_code != rule["criterion_code"]:
        raise ValueError("%s criterion identities do not match" % path)
    if rule_code != rule["rule_code"]:
        raise ValueError("%s rule identities do not match" % path)
    if rule["judge_type"] == "deterministic" and rule["checker_version"] is None:
        raise ValueError("%s deterministic checker_version must be resolved" % path)
    return {
        "node_kind": "atomic_rule",
        "criterion_code": criterion_code,
        "rule_code": rule_code,
        "criterion_snapshot": criterion,
        "atomic_rule_snapshot": rule,
    }


def _normalize_checker_manifest_entry(value, path):
    fields = {
        "checker_version",
        "implementation_hash",
        "params_schema",
        "supported_document_schemas",
        "supported_profiles",
        "observation_schema",
    }
    _assert_closed_mapping(value, fields=fields, path=path)
    return {
        "checker_version": _assert_text(value["checker_version"], path + ".checker_version"),
        "implementation_hash": _assert_sha256(value["implementation_hash"], path + ".implementation_hash"),
        "params_schema": _assert_text(value["params_schema"], path + ".params_schema"),
        "supported_document_schemas": _assert_text_array_preserving_order(
            value["supported_document_schemas"], path + ".supported_document_schemas"
        ),
        "supported_profiles": _assert_text_array_preserving_order(
            value["supported_profiles"], path + ".supported_profiles"
        ),
        "observation_schema": _assert_text(value["observation_schema"], path + ".observation_schema"),
    }


def _normalize_rule_execution_plan(value):
    v1_fields = {
        "schema_version",
        "business_profile_key",
        "rubric_snapshot_hash",
        "policy_snapshot",
        "policy_hash",
        "nodes",
        "dependency_order",
        "checker_manifest",
        "plan_hash",
    }
    v2_identity_fields = {
        "hash_scheme",
        "rubric_source_kind",
        "rubric_version_id",
        "rubric_version_hash",
        "rubric_hash_scheme",
        "business_profile_version",
        "policy_compiler_version",
        "engine_contract_version",
    }
    schema_version = value.get("schema_version") if isinstance(value, Mapping) else None
    if schema_version == "rule-execution-plan@1":
        fields = v1_fields
        node_normalizer = _normalize_plan_node
    elif schema_version == "rule-execution-plan@2":
        fields = v1_fields | v2_identity_fields
        node_normalizer = _normalize_plan_node_v2
    else:
        # Preserve the closed-schema error for missing schema_version while
        # still making future versions fail with an explicit discriminator.
        if isinstance(value, Mapping) and "schema_version" not in value:
            _assert_closed_mapping(value, fields=v1_fields, path="RuleExecutionPlan")
        raise ValueError("unsupported RuleExecutionPlan schema_version")
    _assert_closed_mapping(value, fields=fields, path="RuleExecutionPlan")
    profile_key = _assert_text(
        value["business_profile_key"], "RuleExecutionPlan.business_profile_key"
    )
    policy = _normalize_policy_snapshot(value["policy_snapshot"], "RuleExecutionPlan.policy_snapshot")
    policy_hash = _assert_sha256(value["policy_hash"], "RuleExecutionPlan.policy_hash")
    if policy_hash != policy["policy_hash"]:
        raise ValueError("RuleExecutionPlan policy hash does not match policy snapshot")
    checker_manifest_value = value["checker_manifest"]
    if not isinstance(checker_manifest_value, Mapping):
        raise TypeError("RuleExecutionPlan.checker_manifest must be an object")
    checker_manifest = {}
    for key, item in checker_manifest_value.items():
        checker_key = _assert_text(key, "RuleExecutionPlan.checker_manifest key")
        entry = _normalize_checker_manifest_entry(
            item, "RuleExecutionPlan.checker_manifest.%s" % checker_key
        )
        if profile_key not in entry["supported_profiles"]:
            raise ValueError("checker manifest does not support plan business profile")
        checker_manifest[checker_key] = entry
    normalized = {
        "schema_version": schema_version,
        "business_profile_key": profile_key,
        "rubric_snapshot_hash": _assert_sha256(
            value["rubric_snapshot_hash"], "RuleExecutionPlan.rubric_snapshot_hash"
        ),
        "policy_snapshot": policy,
        "policy_hash": policy_hash,
        "nodes": _assert_mapping_array(
            value["nodes"], "RuleExecutionPlan.nodes", node_normalizer
        ),
        "dependency_order": _assert_text_array_preserving_order(
            value["dependency_order"], "RuleExecutionPlan.dependency_order"
        ),
        "checker_manifest": checker_manifest,
        "plan_hash": _assert_sha256(value["plan_hash"], "RuleExecutionPlan.plan_hash"),
    }
    if schema_version == "rule-execution-plan@2":
        source_kind = _assert_text(
            value["rubric_source_kind"], "RuleExecutionPlan.rubric_source_kind"
        )
        if source_kind == "published_version":
            rubric_version_id = _assert_text(
                value["rubric_version_id"], "RuleExecutionPlan.rubric_version_id"
            )
            rubric_version_hash = _assert_sha256(
                value["rubric_version_hash"], "RuleExecutionPlan.rubric_version_hash"
            )
            rubric_hash_scheme = _assert_text(
                value["rubric_hash_scheme"], "RuleExecutionPlan.rubric_hash_scheme"
            )
        elif source_kind == "legacy_unversioned":
            if any(
                value[field] is not None
                for field in (
                    "rubric_version_id",
                    "rubric_version_hash",
                    "rubric_hash_scheme",
                )
            ):
                raise ValueError(
                    "legacy RuleExecutionPlan must not fabricate published version identity"
                )
            rubric_version_id = None
            rubric_version_hash = None
            rubric_hash_scheme = None
        else:
            raise ValueError("RuleExecutionPlan rubric source kind is unsupported")
        normalized.update(
            {
                "hash_scheme": _assert_text(value["hash_scheme"], "RuleExecutionPlan.hash_scheme"),
                "rubric_source_kind": source_kind,
                "rubric_version_id": rubric_version_id,
                "rubric_version_hash": rubric_version_hash,
                "rubric_hash_scheme": rubric_hash_scheme,
                "business_profile_version": _assert_text(value["business_profile_version"], "RuleExecutionPlan.business_profile_version"),
                "policy_compiler_version": _assert_text(value["policy_compiler_version"], "RuleExecutionPlan.policy_compiler_version"),
                "engine_contract_version": _assert_text(value["engine_contract_version"], "RuleExecutionPlan.engine_contract_version"),
            }
        )
        node_codes = [item["rule_code"] for item in normalized["nodes"]]
        if node_codes != normalized["dependency_order"]:
            raise ValueError("RuleExecutionPlan nodes must match dependency_order")
        used_checkers = {
            item["atomic_rule_snapshot"]["checker_key"]
            for item in normalized["nodes"]
            if item["atomic_rule_snapshot"]["checker_key"] is not None
        }
        if used_checkers != set(normalized["checker_manifest"]):
            raise ValueError("RuleExecutionPlan checker manifest must exactly match used checkers")
    return normalized


def _normalize_runtime_provider(value, path):
    fields = {
        "name",
        "model",
        "model_version",
        "sampling",
        "thinking",
        "response_format",
        "response_schema",
        "artifact_hash",
    }
    _assert_closed_mapping(value, fields=fields, path=path)
    _assert_closed_mapping(
        value["sampling"],
        fields={"temperature", "top_p", "seed", "max_tokens"},
        path=path + ".sampling",
    )
    _assert_closed_mapping(
        value["thinking"], fields={"enabled", "type"}, path=path + ".thinking"
    )
    return {
        "name": _assert_text(value["name"], path + ".name"),
        "model": _assert_text(value["model"], path + ".model"),
        "model_version": _assert_text(value["model_version"], path + ".model_version"),
        "sampling": {
            "temperature": _decimal_text(value["sampling"]["temperature"], path + ".sampling.temperature"),
            "top_p": _decimal_text(value["sampling"]["top_p"], path + ".sampling.top_p"),
            "seed": _assert_integer(value["sampling"]["seed"], path + ".sampling.seed"),
            "max_tokens": _assert_integer(value["sampling"]["max_tokens"], path + ".sampling.max_tokens", minimum=1),
        },
        "thinking": {
            "enabled": _assert_boolean(value["thinking"]["enabled"], path + ".thinking.enabled"),
            "type": _assert_optional_text(value["thinking"]["type"], path + ".thinking.type"),
        },
        "response_format": _assert_text(value["response_format"], path + ".response_format"),
        "response_schema": _assert_text(value["response_schema"], path + ".response_schema"),
        "artifact_hash": _assert_sha256(value["artifact_hash"], path + ".artifact_hash"),
    }


def _normalize_runtime_identity(value, path):
    fields = {
        "engine_contract_version",
        "engine_version",
        "profile_key",
        "profile_version",
        "prompt_version",
        "provider",
        "calibration_anchors_hash",
    }
    _assert_closed_mapping(value, fields=fields, path=path)
    return {
        "engine_contract_version": _assert_text(value["engine_contract_version"], path + ".engine_contract_version"),
        "engine_version": _assert_text(value["engine_version"], path + ".engine_version"),
        "profile_key": _assert_text(value["profile_key"], path + ".profile_key"),
        "profile_version": _assert_text(value["profile_version"], path + ".profile_version"),
        "prompt_version": _assert_text(value["prompt_version"], path + ".prompt_version"),
        "provider": _normalize_runtime_provider(value["provider"], path + ".provider"),
        "calibration_anchors_hash": _assert_sha256(value["calibration_anchors_hash"], path + ".calibration_anchors_hash"),
    }


def _normalize_scoring_request(value):
    fields = {
        "schema_version",
        "submission",
        "document",
        "plan",
        "runtime_identity",
        "rescore_generation",
        "idempotency_key",
    }
    _assert_closed_mapping(value, fields=fields, path="ScoringRequest")
    if value["schema_version"] not in {"scoring-request@1", "scoring-request@2"}:
        raise ValueError("unsupported ScoringRequest schema_version")
    schema_version = value["schema_version"]
    # Detect this cross-contract error before validating the content-addressed
    # document hash.  A caller cannot hide a business-profile mismatch behind
    # the consequential snapshot-hash mismatch caused by editing profile_key.
    raw_profile_keys = {
        value["submission"].get("profile_key") if isinstance(value["submission"], Mapping) else None,
        value["document"].get("profile_key") if isinstance(value["document"], Mapping) else None,
        value["plan"].get("business_profile_key") if isinstance(value["plan"], Mapping) else None,
        value["runtime_identity"].get("profile_key") if isinstance(value["runtime_identity"], Mapping) else None,
    }
    if len(raw_profile_keys) != 1:
        raise ValueError("ScoringRequest business profile identities do not match")
    submission = _normalize_submission_snapshot(value["submission"])
    document = _normalize_document_snapshot(value["document"])
    plan = _normalize_rule_execution_plan(value["plan"])
    runtime_identity = _normalize_runtime_identity(
        value["runtime_identity"], "ScoringRequest.runtime_identity"
    )
    profile_keys = {
        submission["profile_key"],
        document["profile_key"],
        plan["business_profile_key"],
        runtime_identity["profile_key"],
    }
    if len(profile_keys) != 1:
        raise ValueError("ScoringRequest business profile identities do not match")
    if document["profile_version"] != runtime_identity["profile_version"]:
        raise ValueError("ScoringRequest profile version identities do not match")
    for checker_key, entry in plan["checker_manifest"].items():
        if document["schema_version"] not in entry["supported_document_schemas"]:
            raise ValueError("checker %s does not support DocumentSnapshot schema" % checker_key)
    if schema_version == "scoring-request@1" and plan["schema_version"] != "rule-execution-plan@1":
        raise ValueError("scoring-request@1 requires rule-execution-plan@1")
    if schema_version == "scoring-request@2" and plan["schema_version"] != "rule-execution-plan@2":
        raise ValueError("scoring-request@2 requires rule-execution-plan@2")
    normalized = {
        "schema_version": schema_version,
        "submission": submission,
        "document": document,
        "plan": plan,
        "runtime_identity": runtime_identity,
        "rescore_generation": _assert_integer(
            value["rescore_generation"], "ScoringRequest.rescore_generation", minimum=0
        ),
        "idempotency_key": _assert_sha256(value["idempotency_key"], "ScoringRequest.idempotency_key"),
    }
    if schema_version == "scoring-request@2":
        projection = {
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
            "runtime_identity": runtime_identity,
            "rescore_generation": normalized["rescore_generation"],
        }
        if canonical_sha256(projection) != normalized["idempotency_key"]:
            raise ValueError("ScoringRequest idempotency_key does not match replay identity")
    return normalized


def _normalize_prompt_envelope_v3(value):
    """Normalize the exact M3 semantic-provider/cache payload.

    V3 intentionally does not extend the legacy criterion-centric V1/V2
    shape.  It binds the executable rule node and every replay identity while
    exposing only Profile-selected submission extensions.
    """

    fields = {
        "schema_version",
        "prompt_version",
        "runtime_identity",
        "rubric_identity",
        "rubric_snapshot_hash",
        "plan_hash",
        "policy_hash",
        "criterion_snapshot",
        "atomic_rule_snapshot",
        "submission",
        "evidence_units",
        "profile_prompt_extensions",
    }
    _assert_closed_mapping(value, fields=fields, path="PromptEnvelopeV3")
    if value["schema_version"] != "prompt-envelope@3":
        raise ValueError("unsupported PromptEnvelope schema_version")

    rubric_identity_fields = {
        "rubric_source_kind",
        "rubric_version_id",
        "rubric_version_hash",
        "rubric_hash_scheme",
    }
    identity = value["rubric_identity"]
    _assert_closed_mapping(
        identity, fields=rubric_identity_fields, path="PromptEnvelopeV3.rubric_identity"
    )
    source_kind = _assert_text(
        identity["rubric_source_kind"],
        "PromptEnvelopeV3.rubric_identity.rubric_source_kind",
    )
    if source_kind == "published_version":
        rubric_version_id = _assert_text(
            identity["rubric_version_id"],
            "PromptEnvelopeV3.rubric_identity.rubric_version_id",
        )
        rubric_version_hash = _assert_sha256(
            identity["rubric_version_hash"],
            "PromptEnvelopeV3.rubric_identity.rubric_version_hash",
        )
        rubric_hash_scheme = _assert_text(
            identity["rubric_hash_scheme"],
            "PromptEnvelopeV3.rubric_identity.rubric_hash_scheme",
        )
    elif source_kind == "legacy_unversioned":
        if any(
            identity[field] is not None
            for field in (
                "rubric_version_id",
                "rubric_version_hash",
                "rubric_hash_scheme",
            )
        ):
            raise ValueError("legacy PromptEnvelopeV3 must not fabricate version identity")
        rubric_version_id = None
        rubric_version_hash = None
        rubric_hash_scheme = None
    else:
        raise ValueError("PromptEnvelopeV3 rubric source kind is unsupported")

    submission_fields = {
        "source_artifact_hash",
        "normalized_content_hash",
        "document_snapshot_hash",
    }
    submission = value["submission"]
    _assert_closed_mapping(
        submission, fields=submission_fields, path="PromptEnvelopeV3.submission"
    )

    raw_evidence = value["evidence_units"]
    if not isinstance(raw_evidence, (list, tuple)) or not raw_evidence:
        raise ValueError("PromptEnvelopeV3.evidence_units must be a non-empty array")
    evidence_units = [
        _normalize_document_evidence_unit(
            item, "PromptEnvelopeV3.evidence_units[%s]" % index
        )
        for index, item in enumerate(raw_evidence)
    ]
    evidence_ids = [item["evidence_unit_id"] for item in evidence_units]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("PromptEnvelopeV3 contains duplicate evidence identities")

    criterion = _normalize_criterion_snapshot(
        value["criterion_snapshot"], "PromptEnvelopeV3.criterion_snapshot"
    )
    rule = _normalize_atomic_rule_snapshot(value["atomic_rule_snapshot"])
    if criterion["criterion_code"] != rule["criterion_code"]:
        raise ValueError("PromptEnvelopeV3 rule and criterion identities do not match")
    if rule["judge_type"] != "semantic":
        raise ValueError("PromptEnvelopeV3 only accepts semantic atomic rules")

    extensions = _assert_json_value(
        value["profile_prompt_extensions"],
        "PromptEnvelopeV3.profile_prompt_extensions",
    )
    if not isinstance(extensions, dict):
        raise TypeError("PromptEnvelopeV3.profile_prompt_extensions must be an object")

    return {
        "schema_version": "prompt-envelope@3",
        "prompt_version": _assert_text(
            value["prompt_version"], "PromptEnvelopeV3.prompt_version"
        ),
        "runtime_identity": _normalize_runtime_identity(
            value["runtime_identity"], "PromptEnvelopeV3.runtime_identity"
        ),
        "rubric_identity": {
            "rubric_source_kind": source_kind,
            "rubric_version_id": rubric_version_id,
            "rubric_version_hash": rubric_version_hash,
            "rubric_hash_scheme": rubric_hash_scheme,
        },
        "rubric_snapshot_hash": _assert_sha256(
            value["rubric_snapshot_hash"], "PromptEnvelopeV3.rubric_snapshot_hash"
        ),
        "plan_hash": _assert_sha256(value["plan_hash"], "PromptEnvelopeV3.plan_hash"),
        "policy_hash": _assert_sha256(
            value["policy_hash"], "PromptEnvelopeV3.policy_hash"
        ),
        "criterion_snapshot": criterion,
        "atomic_rule_snapshot": rule,
        "submission": {
            "source_artifact_hash": _assert_sha256(
                submission["source_artifact_hash"],
                "PromptEnvelopeV3.submission.source_artifact_hash",
            ),
            "normalized_content_hash": _assert_sha256(
                submission["normalized_content_hash"],
                "PromptEnvelopeV3.submission.normalized_content_hash",
            ),
            "document_snapshot_hash": _assert_sha256(
                submission["document_snapshot_hash"],
                "PromptEnvelopeV3.submission.document_snapshot_hash",
            ),
        },
        "evidence_units": evidence_units,
        "profile_prompt_extensions": extensions,
    }


class _ImmutableContract:
    """Closed, recursively immutable mapping contract shared by Core DTOs."""

    __slots__ = ("_data", "_sealed")
    _normalizer = None

    def __init__(self, **payload):
        normalizer = type(self)._normalizer
        object.__setattr__(self, "_data", _freeze(normalizer(payload)))
        object.__setattr__(self, "_sealed", True)

    @classmethod
    def from_mapping(cls, payload):
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            raise TypeError("%s requires a mapping" % cls.__name__)
        return cls(**dict(payload))

    @classmethod
    def model_validate(cls, payload):
        return cls.from_mapping(payload)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("%s is immutable" % type(self).__name__)
        object.__setattr__(self, name, value)

    def __getattr__(self, name):
        data = object.__getattribute__(self, "_data")
        try:
            return data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def to_mapping(self):
        return _thaw(self._data)

    def model_dump(self, *, mode=None):
        del mode
        return self.to_mapping()

    def dict(self):
        return self.to_mapping()

    def canonical_hash(self):
        return canonical_sha256(self.to_mapping())

    def __repr__(self):
        return "%s(%r)" % (type(self).__name__, self.to_mapping())


class SubmissionSnapshot(_ImmutableContract):
    __slots__ = ()
    _normalizer = staticmethod(_normalize_submission_snapshot)


class DocumentSnapshot(_ImmutableContract):
    __slots__ = ()
    _normalizer = staticmethod(_normalize_document_snapshot)


class AtomicRuleSnapshot(_ImmutableContract):
    __slots__ = ()
    _normalizer = staticmethod(_normalize_atomic_rule_snapshot)


class CompiledRubricSnapshot(_ImmutableContract):
    __slots__ = ()
    _normalizer = staticmethod(_normalize_compiled_rubric_snapshot)


class RuleExecutionPlan(_ImmutableContract):
    __slots__ = ()
    _normalizer = staticmethod(_normalize_rule_execution_plan)


class ScoringRequest(_ImmutableContract):
    __slots__ = ()
    _normalizer = staticmethod(_normalize_scoring_request)


class PromptEnvelopeV3(_ImmutableContract):
    """Closed M3 semantic scoring payload shared by runtime and cache."""

    __slots__ = ()
    _normalizer = staticmethod(_normalize_prompt_envelope_v3)


__all__ = [
    "AtomicRuleSnapshot",
    "CompiledRubricSnapshot",
    "DocumentSnapshot",
    "PromptEnvelopeV1",
    "PromptEnvelopeV2",
    "PromptEnvelopeV3",
    "RuleExecutionPlan",
    "ScoringRequest",
    "SubmissionSnapshot",
]
