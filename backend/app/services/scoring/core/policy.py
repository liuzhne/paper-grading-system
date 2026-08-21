"""M1 ScoringPolicy、权重校验和 Decimal 聚合。

本模块刻意不依赖 ORM、FastAPI 或论文 Profile。所有外部输入先编译成冻结的
``ScoringPolicy``，初评和人工复核再复用同一个 ``aggregate_scores`` 入口。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any


POLICY_SCHEMA_VERSION = "scoring-policy@1"
AGGREGATION_SCHEMA_VERSION = "score-item-aggregation@1"
_WEIGHT_MIN = Decimal("0.01")
_WEIGHT_MAX = Decimal("9999.99")
_ALLOWED_USAGE = {"authoritative_new_runs", "golden_compare_only"}
_EXPECTED_USAGE = {
    "corrected_thesis_policy": "authoritative_new_runs",
    "legacy_behavior_policy": "golden_compare_only",
}
_POLICY_KEY = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")
_MISSING = object()


@dataclass(frozen=True, slots=True)
class AggregationPolicy:
    mode: str
    total_score: Decimal


@dataclass(frozen=True, slots=True)
class RoundingPolicy:
    mode: str
    digits: int


@dataclass(frozen=True, slots=True)
class GradeBand:
    label: str
    minimum: Decimal


@dataclass(frozen=True, slots=True)
class GradeScale:
    basis: str
    bands: tuple[GradeBand, ...]


@dataclass(frozen=True, slots=True)
class GradeBoundaryTolerance:
    value: Decimal
    unit: str


@dataclass(frozen=True, slots=True)
class ReviewPolicy:
    total_below: Decimal
    grade_boundary_tolerance: GradeBoundaryTolerance
    confidence_below: Decimal
    parse_quality_below: Decimal
    on_invalid_evidence: str


@dataclass(frozen=True, slots=True)
class EvidencePolicy:
    schema_version: str
    default_policy: str
    requirement: str
    minimum_valid_items: int
    allowed_types: tuple[str, ...]
    review_on_optional_invalid: bool
    absence: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class FailurePolicy:
    unknown_checker: str
    rule_conflict: str
    semantic_error: str


@dataclass(frozen=True, slots=True)
class LegacyCompatibilityPolicy:
    allow_legacy_direct_compat: bool
    force_review: bool


@dataclass(frozen=True, slots=True)
class ScoringPolicy:
    schema_version: str
    policy_key: str
    usage: str
    aggregation: AggregationPolicy
    rounding: RoundingPolicy
    grade_scale: GradeScale
    review: ReviewPolicy
    evidence: EvidencePolicy
    failure: FailurePolicy
    legacy: LegacyCompatibilityPolicy
    policy_hash: str

    def to_mapping(self, *, include_policy_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "policy_key": self.policy_key,
            "usage": self.usage,
            "aggregation": {
                "mode": self.aggregation.mode,
                "total_score": _decimal_text(self.aggregation.total_score),
            },
            "rounding": {
                "mode": self.rounding.mode,
                "digits": self.rounding.digits,
            },
            "grade_scale": {
                "basis": self.grade_scale.basis,
                "bands": [
                    {"label": band.label, "minimum": _decimal_text(band.minimum)}
                    for band in self.grade_scale.bands
                ],
            },
            "review": {
                "total_below": _decimal_text(self.review.total_below),
                "grade_boundary_tolerance": {
                    "value": _decimal_text(self.review.grade_boundary_tolerance.value),
                    "unit": self.review.grade_boundary_tolerance.unit,
                },
                "confidence_below": _decimal_text(self.review.confidence_below),
                "parse_quality_below": _decimal_text(self.review.parse_quality_below),
                "on_invalid_evidence": self.review.on_invalid_evidence,
            },
            "evidence": {
                "schema_version": self.evidence.schema_version,
                "default_policy": self.evidence.default_policy,
                "requirement": self.evidence.requirement,
                "minimum_valid_items": self.evidence.minimum_valid_items,
                "allowed_types": list(self.evidence.allowed_types),
                "review_on_optional_invalid": self.evidence.review_on_optional_invalid,
                "absence": {
                    "enabled": self.evidence.absence["enabled"],
                    "policy_version": self.evidence.absence["policy_version"],
                    "allowed_targets": list(self.evidence.absence["allowed_targets"]),
                    "complete_scope_selectors": list(
                        self.evidence.absence["complete_scope_selectors"]
                    ),
                },
            },
            "failure": {
                "unknown_checker": self.failure.unknown_checker,
                "rule_conflict": self.failure.rule_conflict,
                "semantic_error": self.failure.semantic_error,
            },
            "legacy": {
                "allow_legacy_direct_compat": self.legacy.allow_legacy_direct_compat,
                "force_review": self.legacy.force_review,
            },
        }
        if include_policy_hash:
            payload["policy_hash"] = self.policy_hash
        return payload

    def model_dump(self, *, mode: str | None = None, **_: Any) -> dict[str, Any]:
        """提供与 Pydantic DTO 相同的只读 JSON 投影，便于 adapter 持久化。"""

        return self.to_mapping()


@dataclass(frozen=True, slots=True)
class WeightValidation:
    mode: str
    total_score: Decimal
    weight_sum: Decimal | None


@dataclass(frozen=True, slots=True)
class AggregatedItem:
    criterion_code: str
    raw_score: Decimal | None
    max_score: Decimal
    weight: Decimal | None
    contribution: Decimal | None
    auto_score_status: str


@dataclass(frozen=True, slots=True)
class AggregationResult:
    unrounded_total: Decimal | None
    rounded_total: Decimal | None
    grade: str | None
    need_manual_review: bool
    items: tuple[AggregatedItem, ...]

    @property
    def total_score(self) -> Decimal | None:
        return self.rounded_total


def build_corrected_thesis_policy(
    total_score: Any,
    mode: str,
    *,
    rounding_digits: int = 2,
) -> ScoringPolicy:
    """构造 M1 新论文权威运行使用的冻结默认 policy。"""

    total = _positive_decimal(total_score, "total_score")
    return compile_scoring_policy(
        {
            "policy_key": "corrected_thesis_policy",
            "usage": "authoritative_new_runs",
            "aggregation": {
                "mode": mode,
                "total_score": _decimal_text(total),
            },
            "rounding": {"mode": "half_up", "digits": rounding_digits},
            "grade_scale": {
                "basis": "percentage",
                "bands": [
                    {"label": "优秀", "minimum": "90"},
                    {"label": "良好", "minimum": "80"},
                    {"label": "中等", "minimum": "70"},
                    {"label": "及格", "minimum": "60"},
                    {"label": "不及格", "minimum": "0"},
                ],
            },
            "review": {
                "total_below": "60",
                "grade_boundary_tolerance": {
                    "value": "2",
                    "unit": "percentage_points",
                },
                "confidence_below": "0.65",
                "parse_quality_below": "0.65",
                "on_invalid_evidence": "required_only",
            },
            "evidence": {
                "schema_version": "evidence-policy-v1",
                "default_policy": "required",
                "requirement": "required",
                "minimum_valid_items": 1,
                "allowed_types": [
                    "source_quote",
                    "deterministic_observation",
                    "scoped_absence",
                ],
                "review_on_optional_invalid": False,
                "absence": {
                    "enabled": True,
                    "policy_version": "absence-policy-v1",
                    "allowed_targets": ["risk_owner"],
                    "complete_scope_selectors": ["document://all-units"],
                },
            },
            "failure": {
                "unknown_checker": "block",
                "rule_conflict": "block",
                "semantic_error": "review",
            },
            "legacy": {
                # M1 仅允许 legacy_unversioned 的旧 llm_direct 标准使用；
                # 正式 RubricVersion 路径在 adapter 层继续 fail closed。
                "allow_legacy_direct_compat": True,
                "force_review": False,
            },
        },
        total_score=total,
    )


def compile_scoring_policy(policy_input: Mapping[str, Any] | ScoringPolicy, *, total_score: Any) -> ScoringPolicy:
    """校验并冻结 policy；hash 绑定除 ``policy_hash`` 自身外的完整行为内容。"""

    expected_total = _positive_decimal(total_score, "total_score")
    if isinstance(policy_input, ScoringPolicy):
        policy_input = policy_input.to_mapping()
    if not isinstance(policy_input, Mapping):
        raise ValueError("policy must be a mapping")

    allowed_top_level = {
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
    _reject_unknown(policy_input, allowed_top_level, "policy")
    schema_version = str(policy_input.get("schema_version") or POLICY_SCHEMA_VERSION)
    if schema_version != POLICY_SCHEMA_VERSION:
        raise ValueError(f"unsupported policy schema_version: {schema_version}")

    policy_key = str(_required(policy_input, "policy_key", "policy"))
    usage = str(_required(policy_input, "usage", "policy"))
    if not _POLICY_KEY.fullmatch(policy_key):
        raise ValueError(f"unsupported policy_key: {policy_key}")
    expected_usage = _EXPECTED_USAGE.get(policy_key, "authoritative_new_runs")
    if usage not in _ALLOWED_USAGE or usage != expected_usage:
        raise ValueError(f"policy {policy_key} cannot be used as {usage}")

    aggregation_data = _mapping(_required(policy_input, "aggregation", "policy"), "aggregation")
    _reject_unknown(aggregation_data, {"mode", "total_score"}, "aggregation")
    aggregation_mode = str(_required(aggregation_data, "mode", "aggregation"))
    if aggregation_mode not in {"points", "weighted_normalized"}:
        raise ValueError(f"unsupported aggregation mode: {aggregation_mode}")
    policy_total = _positive_decimal(
        _required(aggregation_data, "total_score", "aggregation"),
        "aggregation.total_score",
    )
    if policy_total != expected_total:
        raise ValueError("policy total_score does not match rubric total_score")

    rounding_data = _mapping(_required(policy_input, "rounding", "policy"), "rounding")
    _reject_unknown(rounding_data, {"mode", "digits"}, "rounding")
    rounding_mode = str(_required(rounding_data, "mode", "rounding"))
    if rounding_mode != "half_up":
        raise ValueError(f"unsupported rounding mode: {rounding_mode}")
    digits = _integer(_required(rounding_data, "digits", "rounding"), "rounding.digits")
    if digits < 0 or digits > 2:
        raise ValueError("rounding.digits must be between 0 and 2")

    grade_data = _mapping(_required(policy_input, "grade_scale", "policy"), "grade_scale")
    _reject_unknown(grade_data, {"basis", "bands"}, "grade_scale")
    grade_basis = str(_required(grade_data, "basis", "grade_scale"))
    if grade_basis not in {"percentage", "raw_score"}:
        raise ValueError("grade_scale.basis must be percentage or raw_score")
    raw_bands = _required(grade_data, "bands", "grade_scale")
    if not isinstance(raw_bands, Sequence) or isinstance(raw_bands, (str, bytes)) or not raw_bands:
        raise ValueError("grade_scale.bands must be a non-empty array")
    bands: list[GradeBand] = []
    band_labels: set[str] = set()
    for index, raw_band in enumerate(raw_bands):
        band_data = _mapping(raw_band, f"grade_scale.bands[{index}]")
        _reject_unknown(band_data, {"label", "minimum"}, f"grade_scale.bands[{index}]")
        label = str(_required(band_data, "label", f"grade_scale.bands[{index}]")).strip()
        if not label:
            raise ValueError("grade band label cannot be empty")
        if label in band_labels:
            raise ValueError("grade band labels must be unique")
        band_labels.add(label)
        minimum = _decimal(_required(band_data, "minimum", f"grade_scale.bands[{index}]"), "grade minimum")
        if grade_basis == "percentage" and not Decimal("0") <= minimum <= Decimal("100"):
            raise ValueError("percentage grade minimum must be between 0 and 100")
        if grade_basis == "raw_score" and not Decimal("0") <= minimum <= policy_total:
            raise ValueError("raw-score grade minimum must be within rubric total_score")
        bands.append(GradeBand(label=label, minimum=minimum))
    if any(left.minimum <= right.minimum for left, right in zip(bands, bands[1:])):
        raise ValueError("grade bands must be ordered by strictly descending minimum")

    review_data = _mapping(_required(policy_input, "review", "policy"), "review")
    _reject_unknown(
        review_data,
        {
            "total_below",
            "grade_boundary_tolerance",
            "confidence_below",
            "parse_quality_below",
            "on_invalid_evidence",
        },
        "review",
    )
    tolerance_data = _mapping(
        _required(review_data, "grade_boundary_tolerance", "review"),
        "review.grade_boundary_tolerance",
    )
    _reject_unknown(tolerance_data, {"value", "unit"}, "review.grade_boundary_tolerance")
    tolerance_unit = str(_required(tolerance_data, "unit", "review.grade_boundary_tolerance"))
    if tolerance_unit not in {"percentage_points", "raw_score_points"}:
        raise ValueError("grade boundary tolerance unit must be explicit")
    tolerance_value = _decimal(
        _required(tolerance_data, "value", "review.grade_boundary_tolerance"),
        "review.grade_boundary_tolerance.value",
    )
    if tolerance_value < 0:
        raise ValueError("grade boundary tolerance cannot be negative")
    total_below = _decimal(
        _required(review_data, "total_below", "review"),
        "review.total_below",
    )
    if grade_basis == "percentage" and not Decimal("0") <= total_below <= Decimal("100"):
        raise ValueError("percentage review.total_below must be between 0 and 100")
    if grade_basis == "raw_score" and not Decimal("0") <= total_below <= policy_total:
        raise ValueError("raw-score review.total_below must be within rubric total_score")
    on_invalid_evidence = str(
        _required(review_data, "on_invalid_evidence", "review")
    )
    if on_invalid_evidence not in {"required_only", "always", "never"}:
        raise ValueError("unsupported review.on_invalid_evidence")

    evidence_data = _mapping(_required(policy_input, "evidence", "policy"), "evidence")
    _reject_unknown(
        evidence_data,
        {
            "schema_version",
            "default_policy",
            "requirement",
            "minimum_valid_items",
            "allowed_types",
            "review_on_optional_invalid",
            "absence",
        },
        "evidence",
    )
    evidence_schema_version = str(
        evidence_data.get("schema_version", "evidence-policy-v1")
    )
    if evidence_schema_version != "evidence-policy-v1":
        raise ValueError("unsupported evidence policy schema_version")
    default_evidence = str(_required(evidence_data, "default_policy", "evidence"))
    requirement = str(evidence_data.get("requirement", default_evidence))
    if default_evidence not in {"required", "optional"} or requirement not in {"required", "optional"}:
        raise ValueError("evidence policy must be required or optional")
    if requirement != default_evidence:
        raise ValueError("evidence requirement must match default_policy in M1")
    minimum_valid_items = _integer(
        evidence_data.get("minimum_valid_items", 1),
        "evidence.minimum_valid_items",
    )
    if minimum_valid_items < 0:
        raise ValueError("evidence.minimum_valid_items cannot be negative")
    if requirement == "required" and minimum_valid_items < 1:
        raise ValueError("required evidence must require at least one valid item")
    raw_allowed_types = evidence_data.get(
        "allowed_types",
        ("source_quote", "deterministic_observation", "scoped_absence"),
    )
    if not isinstance(raw_allowed_types, Sequence) or isinstance(
        raw_allowed_types, (str, bytes)
    ):
        raise ValueError("evidence.allowed_types must be an array")
    allowed_types = tuple(raw_allowed_types)
    supported_evidence_types = {
        "source_quote",
        "deterministic_observation",
        "scoped_absence",
    }
    if (
        not allowed_types
        or any(not isinstance(value, str) for value in allowed_types)
        or set(allowed_types) - supported_evidence_types
    ):
        raise ValueError("evidence.allowed_types contains unsupported values")
    if len(set(allowed_types)) != len(allowed_types):
        raise ValueError("evidence.allowed_types cannot contain duplicates")
    review_on_optional_invalid = evidence_data.get("review_on_optional_invalid", False)
    if not isinstance(review_on_optional_invalid, bool):
        raise ValueError("evidence.review_on_optional_invalid must be boolean")
    absence_data = evidence_data.get(
        "absence",
        {
            "enabled": True,
            "policy_version": "absence-policy-v1",
            "allowed_targets": ["risk_owner"],
            "complete_scope_selectors": ["document://all-units"],
        },
    )
    absence_data = _mapping(absence_data, "evidence.absence")
    _reject_unknown(
        absence_data,
        {"enabled", "policy_version", "allowed_targets", "complete_scope_selectors"},
        "evidence.absence",
    )
    absence_enabled = absence_data.get("enabled", True)
    if not isinstance(absence_enabled, bool):
        raise ValueError("evidence.absence.enabled must be boolean")
    absence_policy_version = str(
        absence_data.get("policy_version", "absence-policy-v1")
    ).strip()
    if not absence_policy_version:
        raise ValueError("evidence.absence.policy_version cannot be empty")
    raw_allowed_targets = absence_data.get("allowed_targets", ["risk_owner"])
    raw_scope_selectors = absence_data.get(
        "complete_scope_selectors", ["document://all-units"]
    )
    for raw_values, label in (
        (raw_allowed_targets, "evidence.absence.allowed_targets"),
        (raw_scope_selectors, "evidence.absence.complete_scope_selectors"),
    ):
        if not isinstance(raw_values, Sequence) or isinstance(
            raw_values, (str, bytes)
        ):
            raise ValueError(f"{label} must be an array")
    allowed_targets = tuple(raw_allowed_targets)
    scope_selectors = tuple(raw_scope_selectors)
    if any(not isinstance(value, str) or not value.strip() for value in allowed_targets):
        raise ValueError("evidence.absence.allowed_targets must contain non-empty strings")
    if any(not isinstance(value, str) or not value.strip() for value in scope_selectors):
        raise ValueError("evidence.absence.complete_scope_selectors must contain non-empty strings")
    if absence_enabled and "scoped_absence" in allowed_types and (
        not allowed_targets or not scope_selectors
    ):
        raise ValueError("enabled scoped absence requires targets and complete scopes")
    if len(set(allowed_targets)) != len(allowed_targets) or len(set(scope_selectors)) != len(scope_selectors):
        raise ValueError("evidence absence authorization lists cannot contain duplicates")
    failure_data = _mapping(_required(policy_input, "failure", "policy"), "failure")
    _reject_unknown(failure_data, {"unknown_checker", "rule_conflict", "semantic_error"}, "failure")
    failure_values = {
        name: str(_required(failure_data, name, "failure"))
        for name in ("unknown_checker", "rule_conflict", "semantic_error")
    }
    if any(value not in {"block", "review", "error"} for value in failure_values.values()):
        raise ValueError("failure actions must be block, review, or error")

    legacy_data = _mapping(policy_input.get("legacy", {}), "legacy")
    _reject_unknown(
        legacy_data,
        {"allow_legacy_direct_compat", "force_review"},
        "legacy",
    )
    legacy_allow_direct = legacy_data.get("allow_legacy_direct_compat", False)
    legacy_force_review = legacy_data.get("force_review", False)
    if not isinstance(legacy_allow_direct, bool) or not isinstance(
        legacy_force_review, bool
    ):
        raise ValueError("legacy compatibility flags must be boolean")

    provisional = ScoringPolicy(
        schema_version=schema_version,
        policy_key=policy_key,
        usage=usage,
        aggregation=AggregationPolicy(mode=aggregation_mode, total_score=policy_total),
        rounding=RoundingPolicy(mode=rounding_mode, digits=digits),
        grade_scale=GradeScale(basis=grade_basis, bands=tuple(bands)),
        review=ReviewPolicy(
            total_below=total_below,
            grade_boundary_tolerance=GradeBoundaryTolerance(value=tolerance_value, unit=tolerance_unit),
            confidence_below=_ratio(
                _required(review_data, "confidence_below", "review"),
                "review.confidence_below",
            ),
            parse_quality_below=_ratio(
                _required(review_data, "parse_quality_below", "review"),
                "review.parse_quality_below",
            ),
            on_invalid_evidence=on_invalid_evidence,
        ),
        evidence=EvidencePolicy(
            schema_version=evidence_schema_version,
            default_policy=default_evidence,
            requirement=requirement,
            minimum_valid_items=minimum_valid_items,
            allowed_types=allowed_types,
            review_on_optional_invalid=review_on_optional_invalid,
            absence=MappingProxyType(
                {
                    "enabled": absence_enabled,
                    "policy_version": absence_policy_version,
                    "allowed_targets": allowed_targets,
                    "complete_scope_selectors": scope_selectors,
                }
            ),
        ),
        failure=FailurePolicy(
            unknown_checker=failure_values["unknown_checker"],
            rule_conflict=failure_values["rule_conflict"],
            semantic_error=failure_values["semantic_error"],
        ),
        legacy=LegacyCompatibilityPolicy(
            allow_legacy_direct_compat=legacy_allow_direct,
            force_review=legacy_force_review,
        ),
        policy_hash="",
    )
    policy_hash = _canonical_sha256(provisional.to_mapping(include_policy_hash=False))
    supplied_hash = policy_input.get("policy_hash")
    if supplied_hash is not None and str(supplied_hash).removeprefix("sha256:") != policy_hash:
        raise ValueError("policy_hash does not match policy snapshot")
    return ScoringPolicy(
        schema_version=provisional.schema_version,
        policy_key=provisional.policy_key,
        usage=provisional.usage,
        aggregation=provisional.aggregation,
        rounding=provisional.rounding,
        grade_scale=provisional.grade_scale,
        review=provisional.review,
        evidence=provisional.evidence,
        failure=provisional.failure,
        legacy=provisional.legacy,
        policy_hash=policy_hash,
    )


def validate_weight_configuration(criteria: Sequence[Any], *, total_score: Any) -> WeightValidation:
    """验证唯一两种 weight 语义，不做任何静默量化。"""

    total = _positive_decimal(total_score, "total_score")
    if not criteria:
        raise ValueError("weight configuration requires at least one criterion")

    max_scores: list[Decimal] = []
    weights: list[Decimal | None] = []
    for index, criterion in enumerate(criteria):
        max_scores.append(
            _positive_decimal(_read(criterion, "max_score"), f"criterion[{index}].max_score")
        )
        raw_weight = _read(criterion, "weight", default=None)
        if raw_weight is None:
            weights.append(None)
            continue
        weight = _decimal(raw_weight, f"criterion[{index}].weight")
        if weight < _WEIGHT_MIN or weight > _WEIGHT_MAX:
            raise ValueError("weight must be between 0.01 and 9999.99")
        if weight.as_tuple().exponent < -2:
            raise ValueError("weight must have at most two decimal places")
        weights.append(weight)

    if all(weight is None for weight in weights):
        if sum(max_scores, Decimal("0")) != total:
            raise ValueError("weight points mode requires criterion max_score sum to equal total_score")
        return WeightValidation(mode="points", total_score=total, weight_sum=None)
    if any(weight is None for weight in weights):
        raise ValueError("weight must be either present for every criterion or absent for every criterion")

    weight_sum = sum((weight for weight in weights if weight is not None), Decimal("0"))
    return WeightValidation(mode="weighted_normalized", total_score=total, weight_sum=weight_sum)


def quantize_decimal(value: Any, *, digits: int, mode: str) -> Decimal:
    number = _decimal(value, "value")
    if mode != "half_up":
        raise ValueError(f"unsupported rounding mode: {mode}")
    if not isinstance(digits, int) or isinstance(digits, bool) or digits < 0 or digits > 2:
        raise ValueError("rounding digits must be between 0 and 2")
    quantum = Decimal("1").scaleb(-digits)
    return number.quantize(quantum, rounding=ROUND_HALF_UP)


def aggregate_scores(policy: ScoringPolicy, items: Sequence[Any]) -> AggregationResult:
    """按冻结 policy 聚合 criterion 分数，等级始终使用舍入前总分。"""

    if not isinstance(policy, ScoringPolicy):
        raise ValueError("aggregate_scores requires a compiled ScoringPolicy")
    if not items:
        raise ValueError("cannot aggregate an empty criterion list")

    validation = validate_weight_configuration(items, total_score=policy.aggregation.total_score)
    if validation.mode != policy.aggregation.mode:
        raise ValueError("criterion weight mode does not match frozen policy")

    weight_sum = validation.weight_sum
    aggregated: list[AggregatedItem] = []
    blocked = False
    for index, item in enumerate(items):
        code = str(_read(item, "criterion_code", default=_read(item, "code", default=index)))
        max_score = _positive_decimal(_read(item, "max_score"), f"item[{index}].max_score")
        raw_weight = _read(item, "weight", default=None)
        weight = None if raw_weight is None else _decimal(raw_weight, f"item[{index}].weight")
        status = str(_read(item, "auto_score_status", default="calculated"))
        if status not in {"calculated", "invalid", "blocked"}:
            raise ValueError(f"unsupported auto_score_status: {status}")

        if status in {"invalid", "blocked"}:
            blocked = True
            aggregated.append(
                AggregatedItem(
                    criterion_code=code,
                    raw_score=None,
                    max_score=max_score,
                    weight=weight,
                    contribution=None,
                    auto_score_status=status,
                )
            )
            continue

        final_score = _read(item, "final_score", default=None)
        score_value = final_score if final_score is not None else _read(item, "raw_score")
        score = _decimal(score_value, f"item[{index}].raw_score")
        if score < 0 or score > max_score:
            raise ValueError(f"item[{index}] score must be within 0..max_score")
        if validation.mode == "points":
            contribution = score
        else:
            assert weight is not None and weight_sum is not None
            contribution = policy.aggregation.total_score * weight / weight_sum * score / max_score
        aggregated.append(
            AggregatedItem(
                criterion_code=code,
                raw_score=score,
                max_score=max_score,
                weight=weight,
                contribution=contribution,
                auto_score_status=status,
            )
        )

    if blocked:
        return AggregationResult(
            unrounded_total=None,
            rounded_total=None,
            grade=None,
            need_manual_review=True,
            items=tuple(aggregated),
        )

    unrounded = sum(
        (item.contribution for item in aggregated if item.contribution is not None),
        Decimal("0"),
    )
    rounded = quantize_decimal(
        unrounded,
        digits=policy.rounding.digits,
        mode=policy.rounding.mode,
    )
    grade = _grade_for(policy, unrounded)
    return AggregationResult(
        unrounded_total=unrounded,
        rounded_total=rounded,
        grade=grade,
        need_manual_review=_requires_review(policy, unrounded),
        items=tuple(aggregated),
    )


def _grade_for(policy: ScoringPolicy, total: Decimal) -> str:
    comparable = _grade_comparable(policy, total)
    for band in policy.grade_scale.bands:
        if comparable >= band.minimum:
            return band.label
    # 编译器允许自定义不覆盖 0 的档位；低于全部阈值时使用最后一档。
    return policy.grade_scale.bands[-1].label


def _requires_review(policy: ScoringPolicy, total: Decimal) -> bool:
    comparable = _grade_comparable(policy, total)
    if comparable < policy.review.total_below:
        return True

    tolerance = policy.review.grade_boundary_tolerance
    if tolerance.value == 0:
        return False
    for band in policy.grade_scale.bands:
        if band.minimum == 0:
            continue
        if tolerance.unit == "percentage_points":
            actual = total / policy.aggregation.total_score * Decimal("100")
            boundary = (
                band.minimum
                if policy.grade_scale.basis == "percentage"
                else band.minimum / policy.aggregation.total_score * Decimal("100")
            )
        else:
            actual = total
            boundary = (
                band.minimum / Decimal("100") * policy.aggregation.total_score
                if policy.grade_scale.basis == "percentage"
                else band.minimum
            )
        if abs(actual - boundary) <= tolerance.value:
            return True
    return False


def _grade_comparable(policy: ScoringPolicy, total: Decimal) -> Decimal:
    if policy.grade_scale.basis == "percentage":
        return total / policy.aggregation.total_score * Decimal("100")
    return total


def _canonical_sha256(value: Any) -> str:
    """调用共享 Core canonical；在该原语尚未装载时使用等价 JSON 子集。"""

    try:
        from backend.app.services.scoring.core.canonical import canonical_sha256
    except ModuleNotFoundError:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()
    return str(canonical_sha256(value)).removeprefix("sha256:")


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite decimal")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not number.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return number


def _positive_decimal(value: Any, field: str) -> Decimal:
    number = _decimal(value, field)
    if number <= 0:
        raise ValueError(f"{field} must be positive")
    return number


def _ratio(value: Any, field: str) -> Decimal:
    number = _decimal(value, field)
    if number < 0 or number > 1:
        raise ValueError(f"{field} must be between 0 and 1")
    return number


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if Decimal(str(value)) != Decimal(number):
        raise ValueError(f"{field} must be an integer")
    return number


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _required(value: Mapping[str, Any], name: str, field: str) -> Any:
    if name not in value:
        raise ValueError(f"{field}.{name} is required")
    return value[name]


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{field} contains unknown fields: {', '.join(sorted(unknown))}")


def _read(value: Any, name: str, *, default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping) and name in value:
        return value[name]
    if hasattr(value, name):
        return getattr(value, name)
    if default is not _MISSING:
        return default
    raise ValueError(f"missing field: {name}")


__all__ = [
    "AGGREGATION_SCHEMA_VERSION",
    "POLICY_SCHEMA_VERSION",
    "AggregatedItem",
    "AggregationResult",
    "LegacyCompatibilityPolicy",
    "ScoringPolicy",
    "WeightValidation",
    "aggregate_scores",
    "build_corrected_thesis_policy",
    "compile_scoring_policy",
    "quantize_decimal",
    "validate_weight_configuration",
]
