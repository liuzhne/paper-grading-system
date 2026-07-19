"""M3 schema-evolution gates for the executable vertical slice.

M2 introduced the immutable DTO classes, but intentionally did not yet make
their @1 shapes executable for the M3 semantic-band plan.  These probes keep
that distinction explicit: only the exact, known M2 schema limitation becomes
a strict XFAIL.  Any other constructor error is a real regression.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy

import pytest

from backend.app.services.scoring.core.contracts import (
    CompiledRubricSnapshot,
    RuleExecutionPlan,
    ScoringRequest,
)
from backend.app.tests.m3_contract_fixtures import (
    expected_plan_payload,
    published_rubric_payload,
    scoring_request_payload,
)


class M3SchemaCapabilityUnavailable(RuntimeError):
    """Raised only for one precisely identified pre-M3 DTO limitation."""


_KNOWN_ATOMIC_RULE_LIMITATIONS = {
    "AtomicRuleSnapshot.checker_key must be a string",
    "AtomicRuleSnapshot.checker_version must be a string",
    "AtomicRuleSnapshot.max_points must be a finite decimal",
    "AtomicRuleSnapshot.repeat_policy must be a string",
}


def _probe(factory, payload_factory, *, known_limitation):
    try:
        return factory.from_mapping(payload_factory()), None
    except (TypeError, ValueError) as exc:
        if known_limitation(exc):
            return None, M3SchemaCapabilityUnavailable(str(exc))
        raise


def _is_known_rubric_limitation(exc: Exception) -> bool:
    return str(exc) in _KNOWN_ATOMIC_RULE_LIMITATIONS


def _is_known_plan_limitation(exc: Exception) -> bool:
    message = str(exc)
    return (
        message == "unsupported RuleExecutionPlan schema_version"
        or (
            message.startswith("RuleExecutionPlan contains unknown fields:")
            and all(
                field in message
                for field in (
                    "business_profile_version",
                    "engine_contract_version",
                    "policy_compiler_version",
                    "rubric_source_kind",
                    "rubric_version_id",
                    "rubric_version_hash",
                    "rubric_hash_scheme",
                )
            )
        )
    )


def _is_known_request_limitation(exc: Exception) -> bool:
    return str(exc) == "unsupported ScoringRequest schema_version"


_RUBRIC, _RUBRIC_ERROR = _probe(
    CompiledRubricSnapshot,
    published_rubric_payload,
    known_limitation=_is_known_rubric_limitation,
)
_PLAN, _PLAN_ERROR = _probe(
    RuleExecutionPlan,
    expected_plan_payload,
    known_limitation=_is_known_plan_limitation,
)
_REQUEST, _REQUEST_ERROR = _probe(
    ScoringRequest,
    scoring_request_payload,
    known_limitation=_is_known_request_limitation,
)


def _requires(error, capability: str):
    return pytest.mark.xfail(
        condition=error is not None,
        reason=f"M3 DTO capability is not implemented: {capability}",
        raises=M3SchemaCapabilityUnavailable,
        strict=True,
    )


def _require(value, error):
    if error is not None:
        raise M3SchemaCapabilityUnavailable(str(error))
    return value


def _mapping(value) -> dict:
    method = getattr(value, "to_mapping", None)
    assert callable(method)
    mapped = method()
    assert isinstance(mapped, Mapping)
    return deepcopy(dict(mapped))


def _assert_recursively_immutable(value) -> None:
    if isinstance(value, Mapping):
        with pytest.raises(TypeError):
            value["__mutation__"] = True
        for item in value.values():
            if isinstance(item, Mapping) or (
                isinstance(item, Sequence)
                and not isinstance(item, (str, bytes))
            ):
                _assert_recursively_immutable(item)
        return
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    with pytest.raises((AttributeError, TypeError)):
        value[0] = "mutation"
    for item in value:
        if isinstance(item, Mapping) or (
            isinstance(item, Sequence) and not isinstance(item, (str, bytes))
        ):
            _assert_recursively_immutable(item)


@_requires(_RUBRIC_ERROR, "semantic-band CompiledRubricSnapshot")
def test_m3_compiled_rubric_dto_supports_conditional_rule_fields():
    rubric = _require(_RUBRIC, _RUBRIC_ERROR)
    payload = published_rubric_payload()

    assert _mapping(rubric) == payload
    deterministic, semantic = payload["atomic_rules"]
    assert deterministic["checker_key"] is not None
    assert deterministic["checker_version"] is None
    assert deterministic["max_points"] is not None
    assert semantic["checker_key"] is None
    assert semantic["checker_version"] is None
    assert semantic["max_points"] is None
    assert semantic["repeat_policy"] is None
    _assert_recursively_immutable(rubric.atomic_rules)


@_requires(_PLAN_ERROR, "rule-execution-plan@2")
def test_m3_rule_execution_plan_dto_carries_full_replay_identity_and_nodes():
    plan = _require(_PLAN, _PLAN_ERROR)
    payload = expected_plan_payload()

    assert _mapping(plan) == payload
    assert plan.schema_version == "rule-execution-plan@2"
    assert plan.rubric_version_id == payload["rubric_version_id"]
    assert plan.rubric_version_hash == payload["rubric_version_hash"]
    assert plan.rubric_hash_scheme == payload["rubric_hash_scheme"]
    assert plan.business_profile_version == payload["business_profile_version"]
    assert plan.policy_compiler_version == payload["policy_compiler_version"]
    assert plan.engine_contract_version == payload["engine_contract_version"]
    deterministic_node = next(
        node
        for node in payload["nodes"]
        if node["atomic_rule_snapshot"]["judge_type"] == "deterministic"
    )
    assert deterministic_node["atomic_rule_snapshot"]["checker_version"]
    _assert_recursively_immutable(plan.nodes)


@_requires(_REQUEST_ERROR, "scoring-request@2")
def test_m3_scoring_request_dto_round_trips_full_idempotency_identity():
    request = _require(_REQUEST, _REQUEST_ERROR)
    payload = scoring_request_payload()

    assert _mapping(request) == payload
    assert request.schema_version == "scoring-request@2"
    assert request.idempotency_key == payload["idempotency_key"]
    assert request.rescore_generation == 0
    assert request.plan["rubric_version_id"] == payload["plan"][
        "rubric_version_id"
    ]
    _assert_recursively_immutable(request.plan)
