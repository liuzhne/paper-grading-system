"""Fail-closed adapter and temporary authorized executor for legacy rubrics.

Legacy database rows have mutable labels and no immutable version identity.
This module turns only provenance-free, published legacy rubrics into a small
content-addressed snapshot.  Model output may select the frozen rule codes, but
never supplies authoritative deduction points.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from types import MappingProxyType

from backend.app.services.scoring.core.evidence import FrozenList
from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.contracts import CompiledRubricSnapshot


HASH_SCHEME = "core-canonical-json-v1"
SNAPSHOT_SCHEMA_VERSION = "legacy-rubric-snapshot@1"
_MISSING = object()
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_LEGACY_RULE_CODE = re.compile(r"LEGACY:([^:]+):([0-9a-f]{64})\Z")
_IDENTITY_ONLY_RULE_FIELDS = frozenset({"database_id"})
_RULE_EVIDENCE_MODES = frozenset(
    {"source_quote", "scoped_absence", "review_only"}
)


class LegacyRubricError(ValueError):
    """Raised when a rubric cannot safely use the unversioned compatibility path."""


def _field(value, name, default=None):
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return FrozenList(_freeze(item) for item in value)
    return value


def _freeze_behavior(value):
    """Deep-freeze a legacy behavior field while dropping DB-only rule metadata."""

    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise LegacyRubricError("legacy behavior mappings require string keys")
            if key in _IDENTITY_ONLY_RULE_FIELDS:
                continue
            frozen[key] = _freeze_behavior(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return FrozenList(_freeze_behavior(item) for item in value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise LegacyRubricError("legacy behavior Decimal values must be finite")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LegacyRubricError("legacy behavior float values must be finite")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise LegacyRubricError(
        f"unsupported legacy behavior value: {type(value).__name__}"
    )


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
            raise LegacyRubricError("legacy numeric content must be finite")
        normalized = value.normalize()
        return "0" if normalized == 0 else format(normalized, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LegacyRubricError("legacy numeric content must be finite")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _canonical_hash(value) -> str:
    encoded = json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decimal(value, *, label):
    if isinstance(value, bool) or value is None or value == "":
        raise LegacyRubricError(f"{label} must be a finite decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LegacyRubricError(f"{label} must be a finite decimal") from exc
    if not result.is_finite():
        raise LegacyRubricError(f"{label} must be a finite decimal")
    return result


@dataclass(frozen=True)
class LegacyAuthorizedRule:
    code: str
    criterion_code: str
    match: str
    points: Decimal
    reason: str
    source: str | None
    evidence_mode: str
    absence_target: str | None


@dataclass(frozen=True)
class LegacyCriterionSnapshot:
    code: str
    name: str
    max_score: Decimal
    weight: Decimal | None
    description: str | None
    evidence_hints: FrozenList
    deduction_rules: FrozenList
    deduction_rules_structured: FrozenList
    rubric_levels: FrozenList
    sub_checks: FrozenList
    criterion_type: str
    scoring_mode: str
    applies_to: str
    dimension: str | None
    authorized_rules: FrozenList


@dataclass(frozen=True)
class LegacyRubricSnapshot:
    schema_version: str
    hash_scheme: str
    rubric_source_kind: str
    rubric_version_id: str | None
    version_hash: str | None
    name: str
    total_score: Decimal
    criteria: FrozenList
    rubric_snapshot_hash: str

    def to_mapping(self):
        """Return a detached JSON-ready projection; Decimal values become strings."""

        return _canonical_value(self)


@dataclass(frozen=True)
class LegacyScoreResult:
    auto_score: Decimal | None
    auto_score_status: str
    applied_effects: FrozenList
    validation_issues: FrozenList
    need_manual_review: bool
    final_total_blocked: bool

    @property
    def requires_review(self):
        return self.need_manual_review

    @property
    def blocks_final_total(self):
        return self.final_total_blocked

    def to_mapping(self):
        """Return a detached JSON-ready projection of the executor outcome."""

        return _canonical_value(self)


def _rule_evidence_contract(rule):
    raw_mode = _field(rule, "evidence_mode")
    if raw_mode in (None, ""):
        # Historical rows never declared what could prove the rule.  Inferring
        # this from natural-language ``match`` text would turn mutable prose
        # into scoring authority, so old rows are manual-review-only.
        evidence_mode = "review_only"
    elif not isinstance(raw_mode, str) or raw_mode.strip() not in _RULE_EVIDENCE_MODES:
        raise LegacyRubricError(
            "legacy rule evidence_mode must be source_quote, scoped_absence, or review_only"
        )
    else:
        evidence_mode = raw_mode.strip()

    raw_target = _field(rule, "absence_target")
    if evidence_mode == "scoped_absence":
        if not isinstance(raw_target, str) or not raw_target.strip():
            raise LegacyRubricError(
                "scoped_absence legacy rule requires a non-empty absence_target"
            )
        absence_target = raw_target.strip()
    else:
        if raw_target not in (None, ""):
            raise LegacyRubricError(
                "absence_target is only valid for scoped_absence legacy rules"
            )
        absence_target = None
    return evidence_mode, absence_target


def _rule_semantic_content(rule, *, criterion_code, points):
    evidence_mode, absence_target = _rule_evidence_contract(rule)
    return {
        "criterion_code": criterion_code,
        "match": str(_field(rule, "match") or "").strip(),
        "points": points,
        "reason": str(_field(rule, "reason") or "").strip(),
        "source": (
            str(_field(rule, "source")).strip()
            if _field(rule, "source") not in (None, "")
            else None
        ),
        "evidence_mode": evidence_mode,
        "absence_target": absence_target,
    }


def _authorized_rules(criterion_code, maximum, raw_rules):
    result = []
    seen_codes = set()
    for raw_rule in raw_rules or ():
        if not isinstance(raw_rule, Mapping) and not hasattr(raw_rule, "points"):
            continue
        try:
            points = _decimal(_field(raw_rule, "points"), label="legacy rule points")
        except LegacyRubricError:
            continue
        if points <= 0 or points > maximum:
            continue
        semantic = _rule_semantic_content(
            raw_rule,
            criterion_code=criterion_code,
            points=points,
        )
        if not semantic["match"]:
            continue
        rule_hash = _canonical_hash(semantic)
        code = f"LEGACY:{criterion_code}:{rule_hash}"
        if code in seen_codes:
            raise LegacyRubricError(
                f"duplicate authorized legacy rule for criterion {criterion_code}"
            )
        seen_codes.add(code)
        result.append(
            LegacyAuthorizedRule(
                code=code,
                criterion_code=criterion_code,
                match=semantic["match"],
                points=points,
                reason=semantic["reason"],
                source=semantic["source"],
                evidence_mode=semantic["evidence_mode"],
                absence_target=semantic["absence_target"],
            )
        )
    return FrozenList(result)


def adapt_legacy_rubric(rubric, *, compilations=_MISSING):
    """Create an immutable legacy snapshot after an explicit provenance lookup."""

    if compilations is _MISSING:
        raise LegacyRubricError(
            "an explicit provenance query result is required before legacy adaptation"
        )
    if compilations is None:
        raise LegacyRubricError("provenance query result cannot be unknown")
    if tuple(compilations):
        raise LegacyRubricError(
            "rubric provenance exists; legacy fallback cannot bypass the published version path"
        )
    source_kind = _field(rubric, "rubric_source_kind")
    if source_kind not in (None, "", "legacy_unversioned"):
        raise LegacyRubricError(
            "formal rubric_source_kind cannot be adapted as legacy_unversioned"
        )
    if str(_field(rubric, "status", "published")) != "published":
        raise LegacyRubricError("only a published legacy rubric can be scored")

    total_score = _decimal(_field(rubric, "total_score"), label="rubric total_score")
    if total_score <= 0:
        raise LegacyRubricError("rubric total_score must be positive")
    criteria = []
    seen_codes = set()
    for raw_criterion in _field(rubric, "criteria", ()) or ():
        code = str(_field(raw_criterion, "code") or "").strip()
        if not code or code in seen_codes:
            raise LegacyRubricError("legacy criterion codes must be non-empty and unique")
        seen_codes.add(code)
        maximum = _decimal(
            _field(raw_criterion, "max_score"),
            label=f"criterion {code} max_score",
        )
        if maximum <= 0:
            raise LegacyRubricError(f"criterion {code} max_score must be positive")
        weight_value = _field(raw_criterion, "weight")
        weight = (
            None
            if weight_value in (None, "")
            else _decimal(weight_value, label=f"criterion {code} weight")
        )
        rules = _authorized_rules(
            code,
            maximum,
            _field(raw_criterion, "deduction_rules_structured", ()),
        )
        description = _field(raw_criterion, "description")
        if description is not None:
            description = str(description)
        dimension = _field(raw_criterion, "dimension")
        if dimension is not None:
            dimension = str(dimension)
        criteria.append(
            LegacyCriterionSnapshot(
                code=code,
                name=str(_field(raw_criterion, "name") or "").strip(),
                max_score=maximum,
                weight=weight,
                description=description,
                evidence_hints=_freeze_behavior(
                    _field(raw_criterion, "evidence_hints", ()) or ()
                ),
                deduction_rules=_freeze_behavior(
                    _field(raw_criterion, "deduction_rules", ()) or ()
                ),
                deduction_rules_structured=_freeze_behavior(
                    _field(raw_criterion, "deduction_rules_structured", ()) or ()
                ),
                rubric_levels=_freeze_behavior(
                    _field(raw_criterion, "rubric_levels", ()) or ()
                ),
                sub_checks=_freeze_behavior(
                    _field(raw_criterion, "sub_checks", ()) or ()
                ),
                criterion_type=str(
                    _field(raw_criterion, "criterion_type") or "llm_judgment"
                ),
                scoring_mode=str(_field(raw_criterion, "scoring_mode") or "llm_direct"),
                applies_to=str(_field(raw_criterion, "applies_to") or "global"),
                dimension=dimension,
                authorized_rules=rules,
            )
        )
    if not criteria:
        raise LegacyRubricError("legacy rubric has no criteria")

    frozen_criteria = FrozenList(criteria)
    content = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "hash_scheme": HASH_SCHEME,
        "rubric_source_kind": "legacy_unversioned",
        "rubric_version_id": None,
        "version_hash": None,
        "name": str(_field(rubric, "name") or "").strip(),
        "total_score": total_score,
        "criteria": frozen_criteria,
    }
    return LegacyRubricSnapshot(
        **content,
        rubric_snapshot_hash=_canonical_hash(content),
    )


def _core_decimal(value, *, label):
    result = _decimal(value, label=label).normalize()
    return "0" if result == 0 else format(result, "f")


def _core_rule_code(criterion_code: str, content: Mapping) -> str:
    return "legacy.%s.%s" % (
        re.sub(r"[^a-z0-9_.-]+", "_", criterion_code.casefold()).strip("_")
        or "criterion",
        canonical_sha256(
            {"scheme": "legacy-core-atomic-rule-v1", **dict(content)}
        )[:24],
    )


def _core_evidence_policy(raw_rule=None, *, deterministic=False):
    raw_mode = _field(raw_rule, "evidence_mode") if raw_rule is not None else None
    if raw_mode == "scoped_absence":
        mode = "scoped_absence"
    elif deterministic:
        # A deterministic checker result is an observation; old prose is never
        # promoted to source-quote evidence.
        mode = "scoped_absence"
    else:
        mode = "source_quote"
    return {
        "mode": mode,
        "requirement": "required",
        "minimum_coverage": "1",
    }


def _core_criterion(raw_criterion):
    code = str(_field(raw_criterion, "code") or "").strip()
    if not code:
        raise LegacyRubricError("legacy criterion code must be non-empty")
    scoring_mode = str(_field(raw_criterion, "scoring_mode") or "llm_direct")
    criterion_type = str(
        _field(raw_criterion, "criterion_type") or "llm_judgment"
    )
    if criterion_type == "deterministic" or scoring_mode == "deductive":
        assessment_mode = "deduct"
    else:
        assessment_mode = "band"
    weight = _field(raw_criterion, "weight")
    return {
        "criterion_code": code,
        "name": str(_field(raw_criterion, "name") or code).strip(),
        "max_score": _core_decimal(
            _field(raw_criterion, "max_score"),
            label=f"criterion {code} max_score",
        ),
        "weight": (
            None
            if weight in (None, "")
            else _core_decimal(weight, label=f"criterion {code} weight")
        ),
        "assessment_mode": assessment_mode,
    }


def _core_level(raw_level, *, index, criterion_code, maximum):
    raw_code = _field(raw_level, "level_code", _field(raw_level, "label"))
    level_code = str(raw_code or f"LEVEL_{index + 1}").strip()
    points = _field(raw_level, "points", maximum)
    descriptor = _field(
        raw_level,
        "descriptor",
        _field(raw_level, "label", level_code),
    )
    return {
        "level_code": level_code,
        "points": _core_decimal(
            points, label=f"criterion {criterion_code} level points"
        ),
        "descriptor": str(descriptor or ""),
        "positive_example": (
            None
            if _field(raw_level, "positive_example") in (None, "")
            else str(_field(raw_level, "positive_example"))
        ),
        "negative_example": (
            None
            if _field(raw_level, "negative_example") in (None, "")
            else str(_field(raw_level, "negative_example"))
        ),
        "display_order": index,
    }


def _core_atomic_rules(raw_criterion, criterion_snapshot):
    code = criterion_snapshot["criterion_code"]
    maximum = criterion_snapshot["max_score"]
    criterion_type = str(
        _field(raw_criterion, "criterion_type") or "llm_judgment"
    )
    scoring_mode = str(_field(raw_criterion, "scoring_mode") or "llm_direct")
    structured = list(
        _field(raw_criterion, "deduction_rules_structured", ()) or ()
    )

    if criterion_type == "deterministic" or scoring_mode == "deductive":
        # M3 is intentionally a one-rule vertical slice.  Multiple legacy
        # deduction rows are frozen into one checker decision whose cap is the
        # sum of authorized points, never model-supplied points.
        valid_points = []
        for row in structured:
            try:
                points = _decimal(
                    _field(row, "points"), label=f"criterion {code} rule points"
                )
            except LegacyRubricError:
                continue
            if points > 0:
                valid_points.append(points)
        maximum_decimal = _decimal(maximum, label=f"criterion {code} max_score")
        max_points = min(sum(valid_points, Decimal("0")), maximum_decimal)
        if max_points <= 0:
            # A deterministic compatibility node without a structured effect
            # remains executable only as a review observation.  It can never
            # deduct more than the frozen criterion maximum.
            max_points = maximum_decimal
        first_rule = structured[0] if structured else None
        rule_identity = {
            "criterion_code": code,
            "judge_type": "deterministic",
            "checker_key": "thesis.legacy_required_fields.v1",
            "max_points": _core_decimal(
                max_points, label=f"criterion {code} max_points"
            ),
            "structured_rules": _canonical_value(structured),
        }
        rule_code = _core_rule_code(code, rule_identity)
        return [
            {
                "schema_version": "atomic-rule-snapshot@1",
                "rule_code": rule_code,
                "criterion_code": code,
                "direction": "deduct",
                "effect_type": "score",
                "judge_type": "deterministic",
                "checker_key": "thesis.legacy_required_fields.v1",
                "checker_version": None,
                "checker_params": {
                    "criterion_code": code,
                    "applies_to": str(
                        _field(raw_criterion, "applies_to") or "global"
                    ),
                },
                "evidence_policy": _core_evidence_policy(
                    first_rule, deterministic=True
                ),
                "max_points": rule_identity["max_points"],
                "repeat_policy": "once",
                "cap_points": None,
                "depends_on_rule_codes": [],
                "mutex_group": None,
                "levels": [],
            }
        ]

    raw_levels = list(_field(raw_criterion, "rubric_levels", ()) or ())
    if raw_levels:
        levels = [
            _core_level(
                item,
                index=index,
                criterion_code=code,
                maximum=maximum,
            )
            for index, item in enumerate(raw_levels)
        ]
    else:
        # llm_direct/hybrid parity is M5.  The M3 comparison plan carries a
        # review-only synthetic band, and the candidate never becomes an
        # authoritative ScoringRun.
        levels = [
            {
                "level_code": "LEGACY_REVIEW_ONLY",
                "points": maximum,
                "descriptor": "Legacy criterion requires later compatibility execution.",
                "positive_example": None,
                "negative_example": None,
                "display_order": 0,
            }
        ]
    levels.sort(key=lambda item: (item["display_order"], item["level_code"]))
    rule_identity = {
        "criterion_code": code,
        "judge_type": "semantic",
        "levels": levels,
    }
    return [
        {
            "schema_version": "atomic-rule-snapshot@1",
            "rule_code": _core_rule_code(code, rule_identity),
            "criterion_code": code,
            "direction": "band",
            "effect_type": "score",
            "judge_type": "semantic",
            "checker_key": None,
            "checker_version": None,
            "checker_params": {},
            "evidence_policy": _core_evidence_policy(),
            "max_points": None,
            "repeat_policy": None,
            "cap_points": None,
            "depends_on_rule_codes": [],
            "mutex_group": None,
            "levels": levels,
        }
    ]


class LegacyRubricAdapter:
    """Produce the executable Core rubric snapshot for a legacy rubric.

    This is deliberately separate from :func:`adapt_legacy_rubric`, which is
    the M1 compatibility authorization object consumed by the legacy engine.
    """

    def adapt(
        self,
        *,
        rubric,
        criteria=None,
        policy_snapshot,
        business_profile_key: str,
        compilation_rows=_MISSING,
    ) -> CompiledRubricSnapshot:
        if compilation_rows is _MISSING:
            raise LegacyRubricError(
                "an explicit provenance/compilation query result is required"
            )
        if compilation_rows is None:
            raise LegacyRubricError("provenance/compilation result cannot be unknown")
        if tuple(compilation_rows):
            raise LegacyRubricError(
                "formal compilation/version provenance cannot be hidden as legacy"
            )
        source_kind = _field(rubric, "rubric_source_kind")
        if source_kind not in (None, "", "legacy_unversioned"):
            raise LegacyRubricError("formal provenance cannot use the legacy adapter")
        if str(_field(rubric, "status", "published")) != "published":
            raise LegacyRubricError("only a published legacy rubric can be adapted")

        raw_criteria = list(
            criteria
            if criteria is not None
            else (_field(rubric, "criteria", ()) or ())
        )
        if not raw_criteria:
            raise LegacyRubricError("legacy rubric has no criteria")
        criterion_snapshots = [_core_criterion(item) for item in raw_criteria]
        if len({item["criterion_code"] for item in criterion_snapshots}) != len(
            criterion_snapshots
        ):
            raise LegacyRubricError("legacy criterion codes must be unique")
        atomic_rules = []
        for raw, criterion in zip(raw_criteria, criterion_snapshots, strict=True):
            atomic_rules.extend(_core_atomic_rules(raw, criterion))

        content = {
            "schema_version": "compiled-rubric-snapshot@1",
            "rubric_source_kind": "legacy_unversioned",
            "business_profile_key": str(business_profile_key).strip(),
            "total_score": _core_decimal(
                _field(rubric, "total_score"), label="rubric total_score"
            ),
            "criteria": sorted(
                criterion_snapshots, key=lambda item: item["criterion_code"]
            ),
            "atomic_rules": sorted(
                atomic_rules, key=lambda item: item["rule_code"]
            ),
            "global_policy": _canonical_value(policy_snapshot),
        }
        if not content["business_profile_key"]:
            raise LegacyRubricError("business profile key must be non-empty")
        payload = {
            **content,
            "rubric_snapshot_hash": canonical_sha256(
                {"scheme": "compiled-rubric-snapshot-v1", **content}
            ),
        }
        return CompiledRubricSnapshot.from_mapping(payload)


def _criterion(snapshot, criterion_code):
    for criterion in _field(snapshot, "criteria", ()) or ():
        if _field(criterion, "code") == criterion_code:
            return criterion
    raise LegacyRubricError(f"legacy criterion not found: {criterion_code}")


def _ensure_legacy_snapshot(snapshot):
    if not isinstance(snapshot, LegacyRubricSnapshot):
        raise LegacyRubricError(
            "legacy executor requires an immutable LegacyRubricSnapshot"
        )
    if snapshot.schema_version != SNAPSHOT_SCHEMA_VERSION:
        raise LegacyRubricError("unsupported legacy rubric snapshot schema_version")
    if snapshot.hash_scheme != HASH_SCHEME:
        raise LegacyRubricError("unsupported legacy rubric snapshot hash_scheme")
    if snapshot.rubric_source_kind != "legacy_unversioned":
        raise LegacyRubricError(
            "formal rubric/version cannot use the legacy rubric executor"
        )
    if snapshot.rubric_version_id is not None or snapshot.version_hash is not None:
        raise LegacyRubricError(
            "legacy_unversioned snapshot must not contain formal version identity"
        )
    if not isinstance(snapshot.rubric_snapshot_hash, str) or not _HEX_64.fullmatch(
        snapshot.rubric_snapshot_hash
    ):
        raise LegacyRubricError("legacy rubric snapshot hash is malformed")

    content = _object_content(snapshot, excluding=("rubric_snapshot_hash",))
    if content is None or _canonical_hash(content) != snapshot.rubric_snapshot_hash:
        raise LegacyRubricError("legacy rubric snapshot hash mismatch")

    total_score = _decimal(snapshot.total_score, label="rubric total_score")
    if total_score <= 0:
        raise LegacyRubricError("rubric total_score must be positive")
    if not isinstance(snapshot.name, str):
        raise LegacyRubricError("legacy rubric name must be a string")
    if not isinstance(snapshot.criteria, FrozenList) or not snapshot.criteria:
        raise LegacyRubricError(
            "legacy rubric criteria must be a non-empty frozen sequence"
        )

    criterion_codes = set()
    rule_codes = set()
    for criterion in snapshot.criteria:
        _validate_legacy_criterion(criterion, criterion_codes, rule_codes)


def _validate_legacy_criterion(criterion, criterion_codes, rule_codes):
    if not isinstance(criterion, LegacyCriterionSnapshot):
        raise LegacyRubricError("legacy snapshot contains an invalid criterion contract")
    code = criterion.code
    if not isinstance(code, str) or not code.strip() or code != code.strip():
        raise LegacyRubricError("legacy criterion code must be a non-empty normalized string")
    if code in criterion_codes:
        raise LegacyRubricError(f"duplicate legacy criterion code: {code}")
    criterion_codes.add(code)
    if not isinstance(criterion.name, str):
        raise LegacyRubricError(f"criterion {code} name must be a string")

    maximum = _decimal(criterion.max_score, label=f"criterion {code} max_score")
    if maximum <= 0:
        raise LegacyRubricError(f"criterion {code} max_score must be positive")
    if criterion.weight is not None:
        weight = _decimal(criterion.weight, label=f"criterion {code} weight")
        if weight <= 0:
            raise LegacyRubricError(f"criterion {code} weight must be positive")

    if criterion.description is not None and not isinstance(criterion.description, str):
        raise LegacyRubricError(f"criterion {code} description must be a string or null")
    if criterion.dimension is not None and not isinstance(criterion.dimension, str):
        raise LegacyRubricError(f"criterion {code} dimension must be a string or null")
    if criterion.criterion_type not in {"deterministic", "llm_judgment", "hybrid"}:
        raise LegacyRubricError(f"criterion {code} has an unsupported criterion_type")
    if criterion.scoring_mode not in {"deductive", "banded", "llm_direct"}:
        raise LegacyRubricError(f"criterion {code} has an unsupported scoring_mode")
    if not isinstance(criterion.applies_to, str) or not criterion.applies_to:
        raise LegacyRubricError(f"criterion {code} applies_to must be non-empty")

    behavior_sequences = (
        criterion.evidence_hints,
        criterion.deduction_rules,
        criterion.deduction_rules_structured,
        criterion.rubric_levels,
        criterion.sub_checks,
    )
    if any(not isinstance(value, FrozenList) for value in behavior_sequences):
        raise LegacyRubricError(
            f"criterion {code} behavior fields must be deeply frozen"
        )
    if not all(_is_deeply_frozen(value) for value in behavior_sequences):
        raise LegacyRubricError(
            f"criterion {code} contains mutable nested behavior content"
        )
    if any(not isinstance(value, str) for value in criterion.evidence_hints):
        raise LegacyRubricError(f"criterion {code} evidence_hints must contain strings")
    if any(not isinstance(value, str) for value in criterion.deduction_rules):
        raise LegacyRubricError(f"criterion {code} deduction_rules must contain strings")

    if not isinstance(criterion.authorized_rules, FrozenList):
        raise LegacyRubricError(
            f"criterion {code} authorized_rules must be a frozen sequence"
        )
    expected_rules = _authorized_rules(
        code,
        maximum,
        criterion.deduction_rules_structured,
    )
    if len(expected_rules) != len(criterion.authorized_rules):
        raise LegacyRubricError(
            f"criterion {code} authorized rules do not match structured rules"
        )
    for actual, expected in zip(
        criterion.authorized_rules, expected_rules, strict=True
    ):
        _validate_legacy_rule(actual, code, maximum, rule_codes)
        if _canonical_value(actual) != _canonical_value(expected):
            raise LegacyRubricError(
                f"criterion {code} authorized rule content was altered"
            )


def _validate_legacy_rule(rule, criterion_code, maximum, rule_codes):
    if not isinstance(rule, LegacyAuthorizedRule):
        raise LegacyRubricError("legacy snapshot contains an invalid rule contract")
    if rule.criterion_code != criterion_code:
        raise LegacyRubricError(
            f"legacy rule {rule.code} is outside criterion scope {criterion_code}"
        )
    points = _decimal(rule.points, label=f"legacy rule {rule.code} points")
    if points <= 0 or points > maximum:
        raise LegacyRubricError(
            f"legacy rule {rule.code} points are outside criterion range"
        )
    if not isinstance(rule.match, str) or not rule.match.strip():
        raise LegacyRubricError(f"legacy rule {rule.code} match must be non-empty")
    if not isinstance(rule.reason, str):
        raise LegacyRubricError(f"legacy rule {rule.code} reason must be a string")
    if rule.source is not None and not isinstance(rule.source, str):
        raise LegacyRubricError(f"legacy rule {rule.code} source must be a string or null")
    if rule.evidence_mode not in _RULE_EVIDENCE_MODES:
        raise LegacyRubricError(
            f"legacy rule {rule.code} evidence_mode is unsupported"
        )
    if rule.evidence_mode == "scoped_absence":
        if not isinstance(rule.absence_target, str) or not rule.absence_target.strip():
            raise LegacyRubricError(
                f"legacy rule {rule.code} absence_target must be non-empty"
            )
    elif rule.absence_target is not None:
        raise LegacyRubricError(
            f"legacy rule {rule.code} absence_target is out of mode scope"
        )

    code_match = _LEGACY_RULE_CODE.fullmatch(rule.code) if isinstance(rule.code, str) else None
    if code_match is None or code_match.group(1) != criterion_code:
        raise LegacyRubricError(f"legacy rule code is malformed or out of scope: {rule.code}")
    semantic = _rule_semantic_content(
        rule,
        criterion_code=criterion_code,
        points=points,
    )
    expected_code = f"LEGACY:{criterion_code}:{_canonical_hash(semantic)}"
    if rule.code != expected_code:
        raise LegacyRubricError(f"legacy rule semantic hash mismatch: {rule.code}")
    if rule.code in rule_codes:
        raise LegacyRubricError(f"duplicate legacy authorized rule: {rule.code}")
    rule_codes.add(rule.code)


def _is_deeply_frozen(value):
    if isinstance(value, FrozenList):
        return all(_is_deeply_frozen(item) for item in value)
    if isinstance(value, MappingProxyType):
        return all(_is_deeply_frozen(item) for item in value.values())
    if isinstance(value, (list, dict, set)):
        return False
    return value is None or isinstance(value, (str, int, bool, float, Decimal))


def _object_content(value, *, excluding=()):
    excluded = set(excluding)
    if isinstance(value, Mapping):
        return {key: item for key, item in value.items() if key not in excluded}
    if is_dataclass(value):
        return {
            item.name: getattr(value, item.name)
            for item in fields(value)
            if item.name not in excluded
        }
    return None


def _policy_evidence(policy):
    value = _field(policy, "evidence", {})
    return value if isinstance(value, Mapping) else {}


def _evidence_authorization(evidence_result, policy):
    issues = []
    content = _object_content(evidence_result, excluding=("validation_hash",))
    validation_hash = _field(evidence_result, "validation_hash")
    authentic = bool(
        content is not None
        and _field(evidence_result, "schema_version") == "evidence-validation-result@1"
        and isinstance(validation_hash, str)
        and _HEX_64.fullmatch(validation_hash)
        and _canonical_hash(content) == validation_hash
    )
    evidence_policy = _policy_evidence(policy)
    expected_policy_hash = _field(policy, "evidence_policy_hash")
    actual_policy_hash = _field(evidence_result, "evidence_policy_hash")
    if not expected_policy_hash or expected_policy_hash != _canonical_hash(evidence_policy):
        authentic = False
        issues.append({"code": "FROZEN_EVIDENCE_POLICY_INVALID"})
    if actual_policy_hash != expected_policy_hash:
        authentic = False
        issues.append({"code": "EVIDENCE_POLICY_IDENTITY_MISMATCH"})
    if not authentic:
        issues.append({"code": "EVIDENCE_VALIDATION_IDENTITY_INVALID"})

    valid_items = _field(evidence_result, "valid_evidence", ()) or ()
    ref_counts = {}
    for item in valid_items:
        evidence_ref = _field(item, "evidence_ref")
        if isinstance(evidence_ref, str) and evidence_ref.strip():
            normalized_ref = evidence_ref.strip()
            ref_counts[normalized_ref] = ref_counts.get(normalized_ref, 0) + 1

    valid_by_ref = {}
    for index, item in enumerate(valid_items):
        evidence_ref = _field(item, "evidence_ref")
        if evidence_ref in (None, ""):
            # Evidence created before the response-local reference contract can
            # remain valid for display/direct compatibility, but it cannot be
            # selected by an authoritative deduction effect.
            continue
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            issues.append(
                {"code": "VALIDATED_EVIDENCE_REF_INVALID", "evidence_index": index}
            )
            continue
        normalized_ref = evidence_ref.strip()
        if ref_counts.get(normalized_ref) != 1:
            issues.append(
                {
                    "code": "VALIDATED_EVIDENCE_REF_DUPLICATE",
                    "evidence_index": index,
                    "evidence_ref": normalized_ref,
                }
            )
            continue
        valid_by_ref[normalized_ref] = _canonical_value(item)

    sufficient = bool(_field(evidence_result, "evidence_sufficient", False))
    sufficient = authentic and sufficient and bool(valid_items)
    requirement = str(
        _field(evidence_policy, "requirement")
        or _field(evidence_policy, "default_policy")
        or "required"
    )
    result_issues = _field(evidence_result, "issues", ()) or ()
    issues.extend(_canonical_value(item) for item in result_issues)
    injection = authentic and bool(_field(evidence_result, "injection_flagged", False))
    return {
        "authentic": authentic,
        "sufficient": sufficient,
        "requirement": requirement,
        "issues": issues,
        "injection": injection,
        "review_on_optional_invalid": bool(
            _field(evidence_policy, "review_on_optional_invalid", False)
        ),
        "coverage_completeness": str(
            _field(evidence_result, "coverage_completeness", "not_applicable")
            or "not_applicable"
        ),
        "valid_by_ref": valid_by_ref,
    }


def _effect_evidence_refs(effect, *, effect_index):
    raw_refs = effect.get("evidence_refs")
    if not isinstance(raw_refs, (list, tuple)) or not raw_refs:
        return None, {
            "code": "EVIDENCE_REFS_REQUIRED",
            "effect_index": effect_index,
        }
    refs = []
    seen = set()
    for raw_ref in raw_refs:
        if not isinstance(raw_ref, str) or not raw_ref.strip():
            return None, {
                "code": "EVIDENCE_REF_INVALID",
                "effect_index": effect_index,
            }
        evidence_ref = raw_ref.strip()
        if evidence_ref in seen:
            return None, {
                "code": "EVIDENCE_REFS_DUPLICATE",
                "effect_index": effect_index,
                "evidence_ref": evidence_ref,
            }
        seen.add(evidence_ref)
        refs.append(evidence_ref)
    return refs, None


def _bind_effect_evidence(rule, refs, evidence, *, effect_index):
    bound = []
    expected_type = _field(rule, "evidence_mode")
    for evidence_ref in refs:
        item = evidence["valid_by_ref"].get(evidence_ref)
        if item is None:
            return None, {
                "code": "EVIDENCE_REF_NOT_VALIDATED",
                "effect_index": effect_index,
                "evidence_ref": evidence_ref,
            }
        if _field(item, "type") != expected_type:
            return None, {
                "code": "RULE_EVIDENCE_TYPE_MISMATCH",
                "effect_index": effect_index,
                "evidence_ref": evidence_ref,
                "expected_type": expected_type,
            }
        if expected_type == "scoped_absence" and _field(
            item, "target"
        ) != _field(rule, "absence_target"):
            return None, {
                "code": "RULE_ABSENCE_TARGET_MISMATCH",
                "effect_index": effect_index,
                "evidence_ref": evidence_ref,
                "expected_target": _field(rule, "absence_target"),
            }
        bound.append(item)
    return bound, None


def _legacy_policy_flags(policy):
    legacy = _field(policy, "legacy", {})
    return {
        "allow_direct": bool(_field(legacy, "allow_legacy_direct_compat", False)),
        "force_review": bool(_field(legacy, "force_review", False)),
    }


def _score_result(
    *,
    auto_score,
    status,
    applied=(),
    issues=(),
    review=False,
    blocked=False,
):
    return LegacyScoreResult(
        auto_score=auto_score,
        auto_score_status=status,
        applied_effects=FrozenList(_freeze(item) for item in applied),
        validation_issues=FrozenList(_freeze(item) for item in issues),
        need_manual_review=bool(review),
        final_total_blocked=bool(blocked),
    )


def apply_legacy_deductions(
    *, snapshot, criterion_code, model_effects, evidence_result, policy
):
    """Apply only authorized frozen legacy deductions selected by rule code."""

    _ensure_legacy_snapshot(snapshot)
    criterion = _criterion(snapshot, criterion_code)
    maximum = _decimal(_field(criterion, "max_score"), label="criterion max_score")
    rules = list(_field(criterion, "authorized_rules", ()) or ())
    evidence = _evidence_authorization(evidence_result, policy)

    if not evidence["authentic"] or (
        evidence["requirement"] == "required" and not evidence["sufficient"]
    ):
        return _score_result(
            auto_score=None,
            status="invalid",
            issues=evidence["issues"],
            review=True,
            blocked=True,
        )
    if not rules:
        return _score_result(
            auto_score=None,
            status="blocked",
            issues=[*evidence["issues"], {"code": "NO_AUTHORIZED_NUMERIC_RULES"}],
            review=True,
            blocked=True,
        )

    review_only_rules = [
        _field(rule, "code")
        for rule in rules
        if _field(rule, "evidence_mode") == "review_only"
    ]
    if review_only_rules:
        return _score_result(
            auto_score=None,
            status="blocked",
            issues=[
                *evidence["issues"],
                {
                    "code": "LEGACY_RULE_REVIEW_ONLY",
                    "rule_refs": review_only_rules,
                },
            ],
            review=True,
            blocked=True,
        )

    scoped_absence_rules = [
        _field(rule, "code")
        for rule in rules
        if _field(rule, "evidence_mode") == "scoped_absence"
    ]
    if (
        scoped_absence_rules
        and evidence["coverage_completeness"] != "complete"
    ):
        return _score_result(
            auto_score=None,
            status="blocked",
            issues=[
                *evidence["issues"],
                {
                    "code": "SCOPED_ABSENCE_COVERAGE_NOT_AUTHORIZED",
                    "rule_refs": scoped_absence_rules,
                    "coverage_completeness": evidence[
                        "coverage_completeness"
                    ],
                },
            ],
            review=True,
            blocked=True,
        )

    if not evidence["sufficient"]:
        return _score_result(
            auto_score=maximum,
            status="calculated",
            issues=evidence["issues"],
            review=evidence["review_on_optional_invalid"],
            blocked=False,
        )

    authorized = {_field(rule, "code"): rule for rule in rules}
    applied = []
    issues = list(evidence["issues"])
    seen = set()
    total_deduction = Decimal("0")
    effects = model_effects if isinstance(model_effects, (list, tuple)) else ()
    if model_effects is not None and not isinstance(model_effects, (list, tuple)):
        issues.append({"code": "MODEL_EFFECTS_INVALID"})
    for index, effect in enumerate(effects):
        if not isinstance(effect, Mapping):
            issues.append({"code": "MODEL_EFFECT_INVALID", "effect_index": index})
            continue
        rule_ref = effect.get("rule_ref")
        if not isinstance(rule_ref, str) or not rule_ref.strip():
            issues.append({"code": "RULE_REF_REQUIRED", "effect_index": index})
            continue
        rule = authorized.get(rule_ref)
        if rule is None:
            issues.append(
                {"code": "UNAUTHORIZED_RULE_REF", "effect_index": index, "rule_ref": rule_ref}
            )
            continue
        if rule_ref in seen:
            continue

        if "points" in effect and effect.get("points") is not None:
            try:
                reported = _decimal(effect.get("points"), label="model-reported points")
            except LegacyRubricError:
                issues.append({"code": "MODEL_POINTS_INVALID", "effect_index": index})
                continue
            if reported < 0 or reported > maximum:
                issues.append({"code": "MODEL_POINTS_OUT_OF_RANGE", "effect_index": index})
                continue

        evidence_refs, ref_issue = _effect_evidence_refs(
            effect, effect_index=index
        )
        if ref_issue is not None:
            issues.append(ref_issue)
            continue
        bound_evidence, binding_issue = _bind_effect_evidence(
            rule,
            evidence_refs,
            evidence,
            effect_index=index,
        )
        if binding_issue is not None:
            issues.append(binding_issue)
            continue

        frozen_points = _decimal(_field(rule, "points"), label="frozen rule points")
        if total_deduction + frozen_points > maximum:
            issues.append({"code": "CUMULATIVE_DEDUCTION_OUT_OF_RANGE", "effect_index": index})
            continue
        seen.add(rule_ref)
        total_deduction += frozen_points
        applied.append(
            {
                "rule_ref": rule_ref,
                "points": frozen_points,
                "reason": _field(rule, "reason", ""),
                "evidence_refs": evidence_refs,
                "evidence": bound_evidence,
            }
        )

    unresolved_absence_rules = [
        rule_ref for rule_ref in scoped_absence_rules if rule_ref not in seen
    ]
    if unresolved_absence_rules:
        return _score_result(
            auto_score=None,
            status="blocked",
            applied=applied,
            issues=[
                *issues,
                {
                    "code": "SCOPED_ABSENCE_RULE_UNRESOLVED",
                    "rule_refs": unresolved_absence_rules,
                },
            ],
            review=True,
            blocked=True,
        )

    return _score_result(
        auto_score=maximum - total_deduction,
        status="calculated",
        applied=applied,
        issues=issues,
        review=bool(issues) or evidence["injection"],
        blocked=False,
    )


def apply_legacy_direct_compat(
    *, snapshot, criterion_code, model_score, evidence_result, policy
):
    """Apply the explicitly enabled, evidence-gated legacy direct-score escape hatch."""

    _ensure_legacy_snapshot(snapshot)
    criterion = _criterion(snapshot, criterion_code)
    maximum = _decimal(_field(criterion, "max_score"), label="criterion max_score")
    flags = _legacy_policy_flags(policy)
    if not flags["allow_direct"]:
        return _score_result(
            auto_score=None,
            status="blocked",
            issues=[{"code": "LEGACY_DIRECT_NOT_AUTHORIZED"}],
            review=True,
            blocked=True,
        )

    evidence = _evidence_authorization(evidence_result, policy)
    if not evidence["authentic"] or (
        evidence["requirement"] == "required" and not evidence["sufficient"]
    ):
        return _score_result(
            auto_score=None,
            status="invalid",
            issues=evidence["issues"],
            review=True,
            blocked=True,
        )
    try:
        score = _decimal(model_score, label="legacy direct model score")
    except LegacyRubricError:
        return _score_result(
            auto_score=None,
            status="invalid",
            issues=[{"code": "LEGACY_DIRECT_SCORE_INVALID"}],
            review=True,
            blocked=True,
        )
    if score < 0 or score > maximum:
        return _score_result(
            auto_score=None,
            status="invalid",
            issues=[{"code": "LEGACY_DIRECT_SCORE_OUT_OF_RANGE"}],
            review=True,
            blocked=True,
        )
    return _score_result(
        auto_score=score,
        status="calculated",
        issues=evidence["issues"],
        review=(
            flags["force_review"]
            or evidence["injection"]
            or (not evidence["sufficient"] and evidence["review_on_optional_invalid"])
        ),
        blocked=False,
    )


__all__ = [
    "LegacyAuthorizedRule",
    "LegacyCriterionSnapshot",
    "LegacyRubricAdapter",
    "LegacyRubricError",
    "LegacyRubricSnapshot",
    "LegacyScoreResult",
    "adapt_legacy_rubric",
    "apply_legacy_deductions",
    "apply_legacy_direct_compat",
]
