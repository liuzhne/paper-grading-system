"""Evidence validation primitives for the corrected M1 scoring path.

The validator is deliberately independent from ORM entities and LLM response
models.  It accepts plain mappings, derives every authorization decision from
frozen inputs, and returns a content-addressed immutable result that can be
passed to legacy or future Core executors without reconstruction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
import hashlib
import json
import math
import re
from types import MappingProxyType
import unicodedata

RESULT_SCHEMA_VERSION = "evidence-validation-result@1"


# Evidence text is untrusted input.  Injection detection belongs to Core as a
# pure value check; importing the outer scoring validator here would invert the
# dependency boundary and couple replay semantics to an adapter-era module.
_INJECTION_PATTERNS = (
    re.compile(r"忽略(以上|上述|前面|之前|前述).{0,8}(指令|提示|要求|规则|内容)"),
    re.compile(r"(请|务必|必须|麻烦|帮我).{0,6}(给|打|评).{0,4}(满分|最高分|高分)"),
    re.compile(r"(直接)?(给|打).{0,2}(满分|最高分)"),
    re.compile(r"(忽略|无视).{0,6}(评分|扣分)(标准|规则)"),
    re.compile(
        r"ignore\s+(the\s+)?(previous|above|prior|all).{0,24}instruction",
        re.IGNORECASE,
    ),
    re.compile(r"(full|maximum|perfect)\s+(marks?|score)", re.IGNORECASE),
    re.compile(r"system\s+prompt|you\s+are\s+now", re.IGNORECASE),
)


def detect_injection(text) -> bool:
    """Return whether untrusted evidence text resembles a prompt injection."""

    if not text:
        return False
    return any(pattern.search(str(text)) for pattern in _INJECTION_PATTERNS)


class FrozenList(list):
    """A list-compatible, equality-friendly immutable sequence.

    Contract consumers historically compare list-valued fields with ``[]``.
    A tuple would make those comparisons fail, so this small list subclass
    retains the public shape while rejecting every mutating operation.
    """

    @staticmethod
    def _immutable(*_args, **_kwargs):
        raise TypeError("frozen sequence cannot be modified")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return FrozenList(_freeze(item) for item in value)
    return value


def _field(value, name, default=None):
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _canonical_value(value):
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if is_dataclass(value):
        return {
            item.name: _canonical_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal is not canonical")
        normalized = value.normalize()
        if normalized == 0:
            return "0"
        return format(normalized, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float is not canonical")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _canonical_hash(value) -> str:
    payload = json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_id_set_hash(values: Sequence[str]) -> str:
    return _canonical_hash(sorted(set(values)))


@dataclass(frozen=True)
class EvidenceValidationResult:
    schema_version: str
    document_snapshot_hash: str | None
    evidence_policy_hash: str
    evidence_sufficient: bool
    valid_evidence: FrozenList
    issues: FrozenList
    injection_flagged: bool
    coverage_completeness: str
    score_effect_allowed: bool
    review_required: bool
    blocks_final_total: bool
    validation_hash: str

    @property
    def validated_evidence(self):
        return self.valid_evidence

    @property
    def evidence(self):
        return self.valid_evidence

    @property
    def validation_issues(self):
        return self.issues

    @property
    def injection_detected(self):
        return self.injection_flagged

    @property
    def coverage_status(self):
        return self.coverage_completeness

    @property
    def effect_allowed(self):
        return self.score_effect_allowed

    @property
    def needs_review(self):
        return self.review_required

    @property
    def final_total_blocked(self):
        return self.blocks_final_total

    def to_mapping(self):
        """Return a detached JSON-ready projection of the immutable result."""

        return _canonical_value(self)


def _issue(code: str, message: str, *, item_index: int | None = None, **details):
    value = {"code": code, "message": message}
    if item_index is not None:
        value["item_index"] = item_index
    value.update(details)
    return value


def _normalized_text(value) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", text, flags=re.UNICODE).strip()


def _validate_source_quote(item, evidence_units, *, item_index):
    unit_id = _field(item, "evidence_unit_id")
    if not isinstance(unit_id, str) or not unit_id.strip():
        return None, _issue(
            "EVIDENCE_UNIT_ID_REQUIRED",
            "source quote requires an authoritative evidence_unit_id",
            item_index=item_index,
        )
    unit = evidence_units.get(unit_id)
    if unit is None:
        return None, _issue(
            "EVIDENCE_UNIT_NOT_FOUND",
            "the named evidence unit is not present in the frozen snapshot",
            item_index=item_index,
            evidence_unit_id=unit_id,
        )

    quote = _normalized_text(_field(item, "quote"))
    if not quote:
        return None, _issue(
            "EMPTY_QUOTE",
            "source quote is empty after Unicode whitespace normalization",
            item_index=item_index,
            evidence_unit_id=unit_id,
        )
    unit_text = _normalized_text(_field(unit, "text"))
    if quote not in unit_text:
        return None, _issue(
            "QUOTE_NOT_IN_UNIT",
            "source quote is not contained by the named evidence unit",
            item_index=item_index,
            evidence_unit_id=unit_id,
        )

    # ``chunk_id`` and other adapter-local references are intentionally not
    # retained: evidence_unit_id is the sole authoritative source identity.
    return {
        "type": "source_quote",
        "evidence_unit_id": unit_id,
        "quote": quote,
        "location": _field(item, "location"),
    }, None


def _lookup_snapshot_metric(context, locator):
    if not isinstance(locator, Mapping):
        return False, None
    if locator.get("kind") != "document_metric":
        return False, None
    path = str(locator.get("path") or "")
    parts = [part for part in path.split(".") if part]
    if parts and parts[0] == "metrics":
        parts = parts[1:]
    current = _field(context, "snapshot_metrics", {})
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return bool(parts), current


def _validate_deterministic_observation(item, context, *, item_index):
    checker_key = _field(item, "checker_key")
    checker_version = _field(item, "checker_version")
    manifest = _field(context, "checker_manifest", {})
    manifest_entry = manifest.get(checker_key) if isinstance(manifest, Mapping) else None
    frozen_version = _field(manifest_entry, "version") if manifest_entry is not None else None
    if not checker_key or frozen_version != checker_version:
        return None, _issue(
            "CHECKER_IDENTITY_MISMATCH",
            "deterministic observation does not match the frozen checker manifest",
            item_index=item_index,
            checker_key=checker_key,
        )

    found, replayed_value = _lookup_snapshot_metric(context, _field(item, "locator"))
    if not found or _canonical_value(replayed_value) != _canonical_value(
        _field(item, "measured_value")
    ):
        return None, _issue(
            "OBSERVATION_REPLAY_MISMATCH",
            "deterministic observation cannot be replayed from the frozen snapshot",
            item_index=item_index,
            checker_key=checker_key,
        )

    return {
        "type": "deterministic_observation",
        "checker_key": checker_key,
        "checker_version": checker_version,
        "locator": _field(item, "locator"),
        "observation_code": _field(item, "observation_code"),
        "measured_value": _field(item, "measured_value"),
        "expected_value": _field(item, "expected_value"),
    }, None


def _absence_invalid(code, message, *, item_index, coverage="invalid", **details):
    return None, _issue(code, message, item_index=item_index, **details), coverage


def _validate_scoped_absence(item, evidence_units, policy, context, *, item_index):
    absence_policy = _field(policy, "absence", {})
    if not isinstance(absence_policy, Mapping) or not absence_policy.get("enabled", False):
        return _absence_invalid(
            "ABSENCE_DISABLED",
            "the frozen evidence policy does not authorize absence evidence",
            item_index=item_index,
        )

    declared_scope = _field(context, "declared_scope", {})
    if not isinstance(declared_scope, Mapping):
        return _absence_invalid(
            "DECLARED_SCOPE_MISSING",
            "absence evidence requires an authoritative declared scope",
            item_index=item_index,
        )

    context_snapshot_hash = _field(context, "document_snapshot_hash")
    if _field(item, "document_snapshot_hash") != context_snapshot_hash:
        return _absence_invalid(
            "SNAPSHOT_IDENTITY_MISMATCH",
            "absence evidence belongs to a different document snapshot",
            item_index=item_index,
        )

    selector = _field(item, "scope_selector")
    expected_selector = declared_scope.get("selector")
    allowed_selectors = absence_policy.get("complete_scope_selectors", ())
    if selector != expected_selector or selector not in allowed_selectors:
        return _absence_invalid(
            "SCOPE_NOT_AUTHORIZED",
            "absence scope is not the frozen declared scope",
            item_index=item_index,
        )

    target = _field(item, "target")
    if target not in absence_policy.get("allowed_targets", ()):
        return _absence_invalid(
            "ABSENCE_TARGET_NOT_AUTHORIZED",
            "absence target is not authorized by the frozen policy",
            item_index=item_index,
        )
    expected_policy_version = absence_policy.get("policy_version")
    if _field(item, "evidence_policy_version") != expected_policy_version:
        return _absence_invalid(
            "ABSENCE_POLICY_VERSION_MISMATCH",
            "absence evidence policy version does not match the frozen policy",
            item_index=item_index,
        )

    authoritative_expected = declared_scope.get("expected_evidence_unit_ids")
    reported_expected = _field(item, "expected_evidence_unit_ids")
    checked = _field(item, "checked_evidence_unit_ids")
    if not isinstance(authoritative_expected, (list, tuple)) or not isinstance(
        reported_expected, (list, tuple)
    ) or not isinstance(checked, (list, tuple)):
        return _absence_invalid(
            "COVERAGE_SET_INVALID",
            "absence coverage sets must be explicit ordered collections",
            item_index=item_index,
        )
    if not authoritative_expected or not reported_expected or not checked:
        return _absence_invalid(
            "EMPTY_COVERAGE_SCOPE",
            "an empty resolved scope cannot prove absence",
            item_index=item_index,
        )
    if len(authoritative_expected) != len(set(authoritative_expected)):
        return _absence_invalid(
            "DUPLICATE_EXPECTED_UNIT",
            "declared scope contains duplicate evidence unit identities",
            item_index=item_index,
        )
    if len(reported_expected) != len(set(reported_expected)) or len(checked) != len(
        set(checked)
    ):
        return _absence_invalid(
            "DUPLICATE_COVERAGE_UNIT",
            "absence evidence contains duplicate evidence unit identities",
            item_index=item_index,
        )

    expected_set = set(authoritative_expected)
    reported_set = set(reported_expected)
    checked_set = set(checked)
    if reported_set != expected_set:
        return _absence_invalid(
            "EXPECTED_SCOPE_MISMATCH",
            "model-reported expected units differ from the declared scope",
            item_index=item_index,
        )
    if not checked_set.issubset(expected_set) or any(
        unit_id not in evidence_units for unit_id in checked_set
    ):
        return _absence_invalid(
            "CHECKED_SCOPE_MISMATCH",
            "checked units fall outside the declared frozen scope",
            item_index=item_index,
        )

    expected_hash = _canonical_id_set_hash(authoritative_expected)
    checked_hash = _canonical_id_set_hash(checked)
    if declared_scope.get("expected_evidence_unit_ids_hash") != expected_hash:
        return _absence_invalid(
            "DECLARED_SCOPE_HASH_MISMATCH",
            "declared scope hash is inconsistent with its evidence unit set",
            item_index=item_index,
        )
    if _field(item, "expected_evidence_unit_ids_hash") != expected_hash:
        return _absence_invalid(
            "EXPECTED_SCOPE_HASH_MISMATCH",
            "absence expected-set hash is inconsistent",
            item_index=item_index,
        )
    if _field(item, "checked_evidence_unit_ids_hash") != checked_hash:
        return _absence_invalid(
            "CHECKED_SCOPE_HASH_MISMATCH",
            "absence checked-set hash is inconsistent",
            item_index=item_index,
        )

    completeness = "complete" if checked_set == expected_set else "partial"
    if completeness != "complete":
        return _absence_invalid(
            "INCOMPLETE_COVERAGE",
            "partial coverage cannot authorize an absence-based score effect",
            item_index=item_index,
            coverage="partial",
        )

    return {
        "type": "scoped_absence",
        "document_snapshot_hash": context_snapshot_hash,
        "scope_selector": selector,
        "target": target,
        "expected_evidence_unit_ids": sorted(expected_set),
        "expected_evidence_unit_ids_hash": expected_hash,
        "checked_evidence_unit_ids": sorted(checked_set),
        "checked_evidence_unit_ids_hash": checked_hash,
        "coverage_completeness": completeness,
        "evidence_policy_version": expected_policy_version,
    }, None, completeness


def _coverage_merge(current: str, candidate: str) -> str:
    rank = {"not_applicable": 0, "complete": 1, "partial": 2, "invalid": 3}
    return candidate if rank.get(candidate, 3) > rank.get(current, 3) else current


def _response_evidence_ref(item):
    """Return a normalized response-local ref, preserving legacy no-ref items."""

    if "evidence_ref" not in item:
        return None, None
    value = item.get("evidence_ref")
    if not isinstance(value, str) or not value.strip():
        return None, "invalid"
    return value.strip(), None


def validate_evidence(*, evidence, evidence_units, policy, context):
    """Validate evidence and derive score/review authorization.

    No model-provided sufficiency or coverage boolean is trusted.  All accepted
    source quotes are checked against their named frozen evidence unit, and all
    absence claims are replayed against the authoritative scope manifest.
    """

    evidence = evidence if isinstance(evidence, Mapping) else {}
    evidence_units = evidence_units if isinstance(evidence_units, Mapping) else {}
    policy = policy if isinstance(policy, Mapping) else {}
    context = context if isinstance(context, Mapping) else {}
    items = evidence.get("items", ())
    if not isinstance(items, (list, tuple)):
        items = ()
    allowed_types = set(policy.get("allowed_types") or ())
    accepted = []
    issues = []
    coverage = "not_applicable"

    # A reference is an address within one provider response.  Reject every
    # occurrence of a duplicate rather than letting array order decide which
    # evidence an effect binds to.  Historical callers may omit the field;
    # those items stay valid for display but are intentionally unaddressable.
    evidence_ref_counts = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        evidence_ref, ref_error = _response_evidence_ref(item)
        if ref_error is None and evidence_ref is not None:
            evidence_ref_counts[evidence_ref] = (
                evidence_ref_counts.get(evidence_ref, 0) + 1
            )

    for item_index, item in enumerate(items):
        if not isinstance(item, Mapping):
            issues.append(
                _issue(
                    "EVIDENCE_ITEM_INVALID",
                    "evidence item must be a mapping",
                    item_index=item_index,
                )
            )
            continue
        evidence_ref, ref_error = _response_evidence_ref(item)
        if ref_error is not None:
            issues.append(
                _issue(
                    "EVIDENCE_REF_INVALID",
                    "evidence_ref must be a non-empty response-local string",
                    item_index=item_index,
                )
            )
            continue
        if (
            evidence_ref is not None
            and evidence_ref_counts.get(evidence_ref, 0) != 1
        ):
            issues.append(
                _issue(
                    "EVIDENCE_REF_DUPLICATE",
                    "evidence_ref must be unique within one response",
                    item_index=item_index,
                    evidence_ref=evidence_ref,
                )
            )
            continue
        evidence_type = item.get("type")
        if evidence_type not in allowed_types:
            issues.append(
                _issue(
                    "EVIDENCE_TYPE_NOT_ALLOWED",
                    "evidence type is not authorized by the frozen policy",
                    item_index=item_index,
                    evidence_type=evidence_type,
                )
            )
            if evidence_type == "scoped_absence":
                coverage = _coverage_merge(coverage, "invalid")
            continue

        if evidence_type == "source_quote":
            valid, issue = _validate_source_quote(
                item, evidence_units, item_index=item_index
            )
            item_coverage = None
        elif evidence_type == "deterministic_observation":
            valid, issue = _validate_deterministic_observation(
                item, context, item_index=item_index
            )
            item_coverage = None
        elif evidence_type == "scoped_absence":
            valid, issue, item_coverage = _validate_scoped_absence(
                item, evidence_units, policy, context, item_index=item_index
            )
        else:
            valid = None
            issue = _issue(
                "EVIDENCE_TYPE_UNSUPPORTED",
                "evidence type has no Core validator",
                item_index=item_index,
            )
            item_coverage = None

        if item_coverage is not None:
            coverage = _coverage_merge(coverage, item_coverage)
        if valid is not None:
            if evidence_ref is not None:
                valid = {**valid, "evidence_ref": evidence_ref}
            accepted.append(valid)
        if issue is not None:
            issues.append(issue)

    minimum = policy.get("minimum_valid_items", 1)
    try:
        minimum = max(0, int(minimum))
    except (TypeError, ValueError):
        minimum = 1
        issues.append(
            _issue(
                "EVIDENCE_POLICY_INVALID",
                "minimum_valid_items is not an integer",
            )
        )
    sufficient = len(accepted) >= minimum
    requirement = str(policy.get("requirement") or policy.get("default_policy") or "required")

    injection_flagged = any(
        detect_injection(_field(unit, "text", ""))
        for unit in evidence_units.values()
    )
    blocks_final_total = requirement == "required" and not sufficient
    review_required = injection_flagged or blocks_final_total
    if requirement == "optional" and not sufficient:
        review_required = review_required or bool(policy.get("review_on_optional_invalid", False))
    score_effect_allowed = sufficient

    frozen_evidence = FrozenList(_freeze(item) for item in accepted)
    frozen_issues = FrozenList(_freeze(item) for item in issues)
    content = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "document_snapshot_hash": context.get("document_snapshot_hash"),
        "evidence_policy_hash": _canonical_hash(policy),
        "evidence_sufficient": sufficient,
        "valid_evidence": frozen_evidence,
        "issues": frozen_issues,
        "injection_flagged": injection_flagged,
        "coverage_completeness": coverage,
        "score_effect_allowed": score_effect_allowed,
        "review_required": review_required,
        "blocks_final_total": blocks_final_total,
    }
    return EvidenceValidationResult(
        **content,
        validation_hash=_canonical_hash(content),
    )


__all__ = [
    "EvidenceValidationResult",
    "FrozenList",
    "RESULT_SCHEMA_VERSION",
    "detect_injection",
    "validate_evidence",
]
