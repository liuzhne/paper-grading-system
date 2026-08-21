"""M1 ScoringPolicy、权重和 Decimal 聚合的可执行合同。

这些测试有意只依赖 ``scoring.core.policy`` 的公共函数，不接触 ORM、
FastAPI 或论文 Profile。M1 实现合入前以严格 xfail 记录缺失能力；模块一旦
具备完整公共 API，标记自动失效，任何合同偏差都会成为普通测试失败。
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
from importlib import import_module

import pytest


POLICY_MODULE = "backend.app.services.scoring.core.policy"

try:
    _policy_module = import_module(POLICY_MODULE)
except ModuleNotFoundError as exc:
    # 只把目标包尚未建立视为 M1 能力缺失；模块内部依赖缺失仍应在收集期暴露。
    if not POLICY_MODULE.startswith(exc.name or ""):
        raise
    _policy_module = None


def _callable(name: str):
    return getattr(_policy_module, name, None) if _policy_module is not None else None


_compile_scoring_policy = _callable("compile_scoring_policy")
_validate_weight_configuration = _callable("validate_weight_configuration")
_aggregate_scores = _callable("aggregate_scores")
_quantize_decimal = _callable("quantize_decimal")
POLICY_API_AVAILABLE = all(
    callable(value)
    for value in (
        _compile_scoring_policy,
        _validate_weight_configuration,
        _aggregate_scores,
        _quantize_decimal,
    )
)

pytestmark = pytest.mark.xfail(
    not POLICY_API_AVAILABLE,
    reason="M1 scoring.core.policy public API is not implemented yet",
    strict=True,
)


_MISSING = object()


def _require_policy_api() -> None:
    """保证条件 xfail 真正执行并失败，避免缺模块时出现 strict XPASS。"""

    assert POLICY_API_AVAILABLE, (
        f"{POLICY_MODULE} must expose compile_scoring_policy, "
        "validate_weight_configuration, aggregate_scores and quantize_decimal"
    )


def _field(value, *names: str, default=_MISSING):
    """同时读取 mapping、dataclass/Pydantic/普通对象形式的合同结果。"""

    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    if default is not _MISSING:
        return default
    raise AssertionError(f"missing contract field {names!r} in {value!r}")


def _decimal(value) -> Decimal:
    assert value is not None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _policy_payload(
    *,
    mode: str,
    total_score: str = "100",
    digits: int = 2,
    policy_key: str = "corrected_thesis_policy",
) -> dict:
    usage = "authoritative_new_runs" if policy_key == "corrected_thesis_policy" else "golden_compare_only"
    return {
        "policy_key": policy_key,
        "usage": usage,
        "aggregation": {"mode": mode, "total_score": total_score},
        "rounding": {"mode": "half_up", "digits": digits},
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
            "grade_boundary_tolerance": {"value": "2", "unit": "percentage_points"},
            "confidence_below": "0.65",
            "parse_quality_below": "0.65",
            "on_invalid_evidence": "required_only",
        },
        "evidence": {"default_policy": "required"},
        "failure": {
            "unknown_checker": "block",
            "rule_conflict": "block",
            "semantic_error": "review",
        },
    }


def _compile(*, mode: str, total_score: str = "100", digits: int = 2, policy_key: str = "corrected_thesis_policy"):
    _require_policy_api()
    payload = _policy_payload(mode=mode, total_score=total_score, digits=digits, policy_key=policy_key)
    return _compile_scoring_policy(payload, total_score=Decimal(total_score))


def _validate(criteria: list[dict], total_score: str = "100"):
    _require_policy_api()
    return _validate_weight_configuration(criteria, total_score=Decimal(total_score))


def _aggregate(policy, items: list[dict]):
    _require_policy_api()
    return _aggregate_scores(policy, items)


def _aggregation_mode(result) -> str:
    if isinstance(result, str):
        return result
    mode = _field(result, "mode", "aggregation_mode", default=None)
    if mode is not None:
        return str(mode)
    aggregation = _field(result, "aggregation", default=None)
    assert aggregation is not None
    return str(_field(aggregation, "mode"))


def _unrounded_total(result):
    return _field(result, "unrounded_total", "raw_total", "total_before_rounding", default=None)


def _rounded_total(result):
    return _field(result, "total_score", "rounded_total", "final_total", default=None)


def _contribution_map(result) -> dict[str, Decimal | None]:
    rows = _field(result, "items", "criterion_results", "contributions")
    if isinstance(rows, Mapping):
        return {str(key): None if value is None else _decimal(value) for key, value in rows.items()}
    contributions: dict[str, Decimal | None] = {}
    for row in rows:
        code = str(_field(row, "criterion_code", "code", "criterion_id"))
        value = _field(row, "contribution", "weighted_contribution", default=None)
        contributions[code] = None if value is None else _decimal(value)
    return contributions


def _weighted_items(weight_a: str = "80", weight_b: str = "20") -> list[dict]:
    return [
        {
            "criterion_code": "C1",
            "raw_score": Decimal("5"),
            "max_score": Decimal("10"),
            "weight": Decimal(weight_a),
            "auto_score_status": "calculated",
        },
        {
            "criterion_code": "C2",
            "raw_score": Decimal("10"),
            "max_score": Decimal("10"),
            "weight": Decimal(weight_b),
            "auto_score_status": "calculated",
        },
    ]


def test_weighted_normalized_80_20_yields_60_and_records_each_contribution():
    validation = _validate(
        [
            {"criterion_code": "C1", "max_score": Decimal("10"), "weight": Decimal("80")},
            {"criterion_code": "C2", "max_score": Decimal("10"), "weight": Decimal("20")},
        ]
    )
    assert _aggregation_mode(validation) == "weighted_normalized"

    result = _aggregate(_compile(mode="weighted_normalized"), _weighted_items())

    assert _decimal(_unrounded_total(result)) == Decimal("60")
    assert _decimal(_rounded_total(result)) == Decimal("60.00")
    assert _contribution_map(result) == {"C1": Decimal("40"), "C2": Decimal("20")}


def test_weighted_normalized_is_invariant_to_weight_scale():
    policy = _compile(mode="weighted_normalized")
    first = _aggregate(policy, _weighted_items("80", "20"))
    second = _aggregate(policy, _weighted_items("8", "2"))

    assert _decimal(_unrounded_total(first)) == _decimal(_unrounded_total(second)) == Decimal("60")
    assert _contribution_map(first) == _contribution_map(second)


def test_weight_precision_boundaries_are_accepted_exactly():
    result = _validate(
        [
            {"criterion_code": "C1", "max_score": Decimal("10"), "weight": Decimal("0.01")},
            {"criterion_code": "C2", "max_score": Decimal("10"), "weight": Decimal("9999.99")},
        ]
    )
    assert _aggregation_mode(result) == "weighted_normalized"


def test_points_mode_requires_empty_weights_and_sums_awarded_points():
    validation = _validate(
        [
            {"criterion_code": "C1", "max_score": Decimal("60"), "weight": None},
            {"criterion_code": "C2", "max_score": Decimal("40"), "weight": None},
        ]
    )
    assert _aggregation_mode(validation) == "points"

    result = _aggregate(
        _compile(mode="points"),
        [
            {
                "criterion_code": "C1",
                "raw_score": Decimal("45.25"),
                "max_score": Decimal("60"),
                "weight": None,
                "auto_score_status": "calculated",
            },
            {
                "criterion_code": "C2",
                "raw_score": Decimal("32.50"),
                "max_score": Decimal("40"),
                "weight": None,
                "auto_score_status": "calculated",
            },
        ],
    )

    assert _decimal(_unrounded_total(result)) == Decimal("77.75")
    assert _contribution_map(result) == {"C1": Decimal("45.25"), "C2": Decimal("32.50")}


@pytest.mark.parametrize(
    ("criteria", "total_score"),
    [
        (
            [
                {"criterion_code": "C1", "max_score": Decimal("10"), "weight": Decimal("80")},
                {"criterion_code": "C2", "max_score": Decimal("10"), "weight": None},
            ],
            "100",
        ),
        (
            [
                {"criterion_code": "C1", "max_score": Decimal("10"), "weight": Decimal("0")},
                {"criterion_code": "C2", "max_score": Decimal("10"), "weight": Decimal("20")},
            ],
            "100",
        ),
        (
            [
                {"criterion_code": "C1", "max_score": Decimal("10"), "weight": Decimal("-1")},
                {"criterion_code": "C2", "max_score": Decimal("10"), "weight": Decimal("20")},
            ],
            "100",
        ),
        (
            [
                {"criterion_code": "C1", "max_score": Decimal("10"), "weight": Decimal("80.001")},
                {"criterion_code": "C2", "max_score": Decimal("10"), "weight": Decimal("20")},
            ],
            "100",
        ),
        (
            [
                {"criterion_code": "C1", "max_score": Decimal("10"), "weight": Decimal("10000")},
                {"criterion_code": "C2", "max_score": Decimal("10"), "weight": Decimal("20")},
            ],
            "100",
        ),
        (
            [
                {"criterion_code": "C1", "max_score": Decimal("60"), "weight": None},
                {"criterion_code": "C2", "max_score": Decimal("30"), "weight": None},
            ],
            "100",
        ),
    ],
    ids=["mixed", "zero", "negative", "extra-precision", "out-of-range", "points-total-mismatch"],
)
def test_invalid_weight_configurations_fail_without_silent_normalization(criteria, total_score):
    with pytest.raises(ValueError):
        _validate(criteria, total_score)


def test_half_up_quantization_is_not_bankers_rounding():
    _require_policy_api()
    assert _decimal(_quantize_decimal(Decimal("2.25"), digits=1, mode="half_up")) == Decimal("2.3")
    assert _decimal(_quantize_decimal(Decimal("-2.25"), digits=1, mode="half_up")) == Decimal("-2.3")


@pytest.mark.parametrize("digits", [-1, 3])
def test_rounding_digits_outside_current_database_precision_are_rejected(digits):
    with pytest.raises(ValueError):
        _compile(mode="points", digits=digits)


def test_non_100_rubric_uses_percentage_grade_scale():
    result = _aggregate(
        _compile(mode="points", total_score="20"),
        [
            {
                "criterion_code": "C1",
                "raw_score": Decimal("20"),
                "max_score": Decimal("20"),
                "weight": None,
                "auto_score_status": "calculated",
            }
        ],
    )

    assert _decimal(_rounded_total(result)) == Decimal("20.00")
    assert _field(result, "grade", "grade_label") == "优秀"


def test_grade_is_selected_from_unrounded_total_before_display_quantization():
    result = _aggregate(
        _compile(mode="points", digits=1),
        [
            {
                "criterion_code": "C1",
                "raw_score": Decimal("89.96"),
                "max_score": Decimal("100"),
                "weight": None,
                "auto_score_status": "calculated",
            }
        ],
    )

    assert _decimal(_unrounded_total(result)) == Decimal("89.96")
    assert _decimal(_rounded_total(result)) == Decimal("90.0")
    assert _field(result, "grade", "grade_label") == "良好"


def test_grade_boundary_tolerance_unit_is_explicit_and_changes_non_100_review_math():
    _require_policy_api()
    percentage_payload = _policy_payload(mode="points", total_score="20")
    percentage_payload["review"]["grade_boundary_tolerance"] = {
        "value": "2",
        "unit": "percentage_points",
    }
    raw_payload = deepcopy(percentage_payload)
    raw_payload["review"]["grade_boundary_tolerance"] = {
        "value": "0.1",
        "unit": "raw_score_points",
    }
    item = {
        "criterion_code": "C1",
        "raw_score": Decimal("17.8"),
        "max_score": Decimal("20"),
        "weight": None,
        "auto_score_status": "calculated",
    }

    percentage = _aggregate(
        _compile_scoring_policy(percentage_payload, total_score=Decimal("20")),
        [item],
    )
    raw = _aggregate(
        _compile_scoring_policy(raw_payload, total_score=Decimal("20")),
        [item],
    )

    assert _field(percentage, "need_manual_review", "requires_review") is True
    assert _field(raw, "need_manual_review", "requires_review") is False


def test_unknown_grade_boundary_tolerance_unit_is_rejected():
    _require_policy_api()
    payload = _policy_payload(mode="points")
    payload["review"]["grade_boundary_tolerance"]["unit"] = "implicit_or_deployment_default"
    with pytest.raises(ValueError):
        _compile_scoring_policy(payload, total_score=Decimal("100"))


@pytest.mark.parametrize("total_score", [Decimal("0"), Decimal("-1")])
def test_policy_requires_a_positive_total_score(total_score):
    _require_policy_api()
    payload = _policy_payload(mode="points", total_score=str(total_score))
    with pytest.raises(ValueError):
        _compile_scoring_policy(payload, total_score=total_score)


@pytest.mark.parametrize("max_score", [Decimal("0"), Decimal("-0.01")])
def test_weight_validation_requires_each_criterion_max_score_to_be_positive(max_score):
    with pytest.raises(ValueError):
        _validate(
            [
                {"criterion_code": "C1", "max_score": max_score, "weight": None},
                {
                    "criterion_code": "C2",
                    "max_score": Decimal("100") - max_score,
                    "weight": None,
                },
            ]
        )


@pytest.mark.parametrize("raw_score", [Decimal("-0.01"), Decimal("10.01")])
def test_aggregation_rejects_out_of_range_raw_scores_instead_of_clamping(raw_score):
    with pytest.raises(ValueError):
        _aggregate(
            _compile(mode="points", total_score="10"),
            [
                {
                    "criterion_code": "C1",
                    "raw_score": raw_score,
                    "max_score": Decimal("10"),
                    "weight": None,
                    "auto_score_status": "calculated",
                }
            ],
        )


@pytest.mark.parametrize("status", ["invalid", "blocked"])
def test_invalid_or_blocked_item_makes_run_total_and_contribution_empty(status):
    items = _weighted_items()
    items[0]["raw_score"] = None
    items[0]["auto_score_status"] = status

    result = _aggregate(_compile(mode="weighted_normalized"), items)

    assert _unrounded_total(result) is None
    assert _rounded_total(result) is None
    assert _field(result, "grade", "grade_label", default=None) is None
    assert _field(result, "need_manual_review", "requires_review") is True
    assert _contribution_map(result)["C1"] is None


def test_ordinary_human_override_cannot_resolve_invalid_auto_score():
    items = _weighted_items()
    items[0].update(
        {
            "raw_score": None,
            "auto_score_status": "invalid",
            "final_score": Decimal("10"),
            "resolution_type": "ordinary_override",
        }
    )

    result = _aggregate(_compile(mode="weighted_normalized"), items)

    assert _unrounded_total(result) is None
    assert _rounded_total(result) is None
    assert _field(result, "need_manual_review", "requires_review") is True


def test_compiled_policy_is_detached_from_mutable_input_and_freezes_policy_role():
    _require_policy_api()
    corrected_input = _policy_payload(mode="points", policy_key="corrected_thesis_policy")
    corrected = _compile_scoring_policy(corrected_input, total_score=Decimal("100"))
    corrected_input["rounding"]["digits"] = 0
    corrected_input["usage"] = "golden_compare_only"

    corrected_rounding = _field(corrected, "rounding")
    assert int(_field(corrected_rounding, "digits")) == 2
    assert _field(corrected, "policy_key", "name", "key") == "corrected_thesis_policy"
    corrected_usage = _field(corrected, "usage", "allowed_usage", default=None)
    if corrected_usage is not None:
        assert corrected_usage == "authoritative_new_runs"
    with pytest.raises((AttributeError, TypeError, ValueError)):
        if isinstance(corrected_rounding, Mapping):
            corrected_rounding["digits"] = 0
        else:
            corrected_rounding.digits = 0

    legacy = _compile(
        mode="points",
        policy_key="legacy_behavior_policy",
    )
    assert _field(legacy, "policy_key", "name", "key") == "legacy_behavior_policy"
    legacy_usage = _field(legacy, "usage", "allowed_usage", default=None)
    if legacy_usage is not None:
        assert legacy_usage == "golden_compare_only"


def test_compiled_policy_hash_is_stable_and_binds_behavioral_content():
    first = _compile(mode="points", digits=2)
    identical = _compile(mode="points", digits=2)
    changed_rounding = _compile(mode="points", digits=1)

    first_hash = str(_field(first, "policy_hash"))
    assert first_hash == str(_field(identical, "policy_hash"))
    assert first_hash != str(_field(changed_rounding, "policy_hash"))
    digest = first_hash.removeprefix("sha256:")
    assert len(digest) == 64
    assert digest == digest.lower()
    assert set(digest) <= set("0123456789abcdef")


def test_legacy_compatibility_flags_are_frozen_and_hash_bound():
    base = _policy_payload(mode="points")
    base["legacy"] = {
        "allow_legacy_direct_compat": False,
        "force_review": False,
    }
    changed = deepcopy(base)
    changed["legacy"]["allow_legacy_direct_compat"] = True

    blocked = _compile_scoring_policy(base, total_score=Decimal("100"))
    allowed = _compile_scoring_policy(changed, total_score=Decimal("100"))
    base["legacy"]["allow_legacy_direct_compat"] = True

    assert _field(_field(blocked, "legacy"), "allow_legacy_direct_compat") is False
    assert _field(_field(allowed, "legacy"), "allow_legacy_direct_compat") is True
    assert _field(blocked, "policy_hash") != _field(allowed, "policy_hash")


def test_legacy_behavior_policy_cannot_claim_authoritative_run_usage():
    _require_policy_api()
    payload = _policy_payload(mode="points", policy_key="legacy_behavior_policy")
    payload["usage"] = "authoritative_new_runs"
    with pytest.raises(ValueError):
        _compile_scoring_policy(payload, total_score=Decimal("100"))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload["evidence"].update(
            {"schema_version": "evidence-policy-v2"}
        ),
        lambda payload: payload["evidence"].update(
            {"minimum_valid_items": 0}
        ),
        lambda payload: payload["evidence"].update(
            {"allowed_types": "source_quote"}
        ),
        lambda payload: payload["evidence"].update(
            {
                "absence": {
                    "enabled": True,
                    "policy_version": "",
                    "allowed_targets": ["risk_owner"],
                    "complete_scope_selectors": ["document://all-units"],
                }
            }
        ),
        lambda payload: payload["grade_scale"]["bands"].append(
            {"label": "不及格", "minimum": "-1"}
        ),
    ],
    ids=[
        "evidence-schema",
        "required-minimum-zero",
        "allowed-types-not-array",
        "absence-version-empty",
        "duplicate-grade-label",
    ],
)
def test_policy_rejects_ambiguous_or_non_replayable_behavior(mutation):
    payload = _policy_payload(mode="points")
    mutation(payload)

    with pytest.raises(ValueError):
        _compile_scoring_policy(payload, total_score=Decimal("100"))
