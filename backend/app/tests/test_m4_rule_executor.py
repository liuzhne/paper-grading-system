"""M4 AtomicRule executor executable specification.

These tests intentionally describe the production capability before it exists.
Only the missing ``core.rule_executor.execute_rule_plan`` symbol is converted
to a strict XFAIL.  Once that symbol is present, signature errors, DTO/schema
errors, internal import failures and behavioural defects are ordinary test
failures.

The executor is a pure Core boundary.  It consumes a frozen ScoringRequest and
ports, and returns ``rule-execution-result@1`` with complete criterion outcomes,
rule decisions, score contributions and review issues.  Aggregation/persistence
remain callers' responsibilities.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
import importlib
import inspect

import pytest

from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.tests.m2_contract_fixtures import PROFILE_KEY, PROFILE_VERSION
from backend.app.tests.m3_contract_fixtures import (
    CHECKER_KEY,
    CHECKER_VERSION,
    checker_manifest_payload,
    idempotency_projection,
    plan_hash_projection,
    scoring_request_payload,
)


EXECUTOR_MODULE = "backend.app.services.scoring.core.rule_executor"
EXECUTOR_SYMBOL = "execute_rule_plan"
RESULT_SCHEMA_VERSION = "rule-execution-result@1"
OCCURRENCE_SCHEME = "occurrence-id-v1"

CRITERION_CODE = "QUALITY"
RULE_CODE = "proposal.quality_gap.v1"
ALLOWED_FINDING_CODES = ("GAP", "GAP_A", "GAP_B", "GAP_C")
CHECKER_OBSERVATION_SCHEMA = {
    "schema_version": "deterministic-observation-schema@1",
    "allowed_observation_codes": ["REQUIRED_FIELD_MISSING"],
    "allowed_finding_codes": list(ALLOWED_FINDING_CODES),
}


def _m4_checker_manifest_entry() -> dict:
    entry = deepcopy(checker_manifest_payload()[CHECKER_KEY])
    entry["observation_schema"] = deepcopy(CHECKER_OBSERVATION_SCHEMA)
    return entry


class M4CapabilityUnavailable(RuntimeError):
    """The only exception an M4 capability gate may turn into XFAIL."""


def _probe_symbol(module_name: str, symbol: str):
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        target_missing = exc.name == module_name or (
            exc.name is not None and module_name.startswith(exc.name + ".")
        )
        if not target_missing:
            raise
        return None
    # Avoid module-level __getattr__ magic: the public symbol must really exist.
    return vars(module).get(symbol)


_EXECUTE_RULE_PLAN = _probe_symbol(EXECUTOR_MODULE, EXECUTOR_SYMBOL)

requires_rule_executor = pytest.mark.xfail(
    condition=_EXECUTE_RULE_PLAN is None,
    reason=f"M4 capability is not implemented: {EXECUTOR_MODULE}.{EXECUTOR_SYMBOL}",
    raises=M4CapabilityUnavailable,
    strict=True,
)


def _require_executor():
    if _EXECUTE_RULE_PLAN is None:
        raise M4CapabilityUnavailable(
            f"{EXECUTOR_MODULE}.{EXECUTOR_SYMBOL} is not implemented"
        )
    return _EXECUTE_RULE_PLAN


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    method = getattr(value, "to_mapping", None)
    if callable(method):
        return _plain(method())
    return value


def _result_mapping(value) -> dict:
    mapped = _plain(value)
    assert isinstance(mapped, dict), "execute_rule_plan must return a DTO/mapping"
    return mapped


@dataclass(frozen=True, slots=True)
class _TechnicalProposalProfile:
    profile_key: str = PROFILE_KEY
    profile_version: str = PROFILE_VERSION

    def build_prompt_extensions(self, *, submission_snapshot, document_snapshot):
        assert submission_snapshot["profile_key"] == self.profile_key
        assert document_snapshot["profile_key"] == self.profile_key
        return {
            "metadata": {
                "project_name": submission_snapshot["metadata"]["project_name"]
            }
        }


class _CheckerRegistry:
    """Explicit registry port whose checkers return observations, never points."""

    def __init__(self, results=None):
        self.results = deepcopy(results or {})
        self.resolve_calls: list[dict] = []
        self.checker_calls: list[dict] = []
        self.manifest_calls: list[dict] = []
        self.resolved_observation_schemas: list[dict] = []

    def resolve(
        self,
        *,
        checker_key,
        checker_version,
        checker_params,
        profile_key,
        document_schema_version,
    ):
        self.resolve_calls.append(
            {
                "checker_key": checker_key,
                "checker_version": checker_version,
                "checker_params": deepcopy(checker_params),
                "profile_key": profile_key,
                "document_schema_version": document_schema_version,
            }
        )
        if checker_key != CHECKER_KEY or checker_version != CHECKER_VERSION:
            raise KeyError(f"unknown checker {checker_key}@{checker_version}")

        def checker(*, document, params):
            # Deliberately no rule/max_points/effect argument: the checker has
            # observation authority only.
            self.checker_calls.append(
                {"document": deepcopy(document), "params": deepcopy(params)}
            )
            fixture_key = params["fixture_key"]
            return deepcopy(self.results[fixture_key])

        # M4 resolution remains callable-compatible with M3 while exposing the
        # frozen schema that authorizes deterministic observation/finding codes.
        checker.observation_schema = deepcopy(CHECKER_OBSERVATION_SCHEMA)
        checker.checker_key = CHECKER_KEY
        checker.checker_version = CHECKER_VERSION
        self.resolved_observation_schemas.append(
            deepcopy(checker.observation_schema)
        )
        return checker

    def manifest(self, *, profile_key, document_schema_version):
        self.manifest_calls.append(
            {
                "profile_key": profile_key,
                "document_schema_version": document_schema_version,
            }
        )
        if (
            profile_key != PROFILE_KEY
            or document_schema_version != "document-snapshot@1"
        ):
            return {}
        return {CHECKER_KEY: _m4_checker_manifest_entry()}


class _SemanticRuntime:
    def __init__(self, responses=None):
        self.responses = deepcopy(responses or {})
        self.envelopes: list[dict] = []

    def score(self, *, envelope):
        mapped = _plain(envelope)
        assert isinstance(mapped, dict)
        self.envelopes.append(mapped)
        rule_code = mapped["atomic_rule_snapshot"]["rule_code"]
        return deepcopy(self.responses[rule_code])


def _criterion(
    *,
    code: str = CRITERION_CODE,
    mode: str = "deduct",
    max_score: str = "100",
) -> dict:
    return {
        "criterion_code": code,
        "name": f"Criterion {code}",
        "max_score": max_score,
        "weight": None,
        "assessment_mode": mode,
    }


def _level(code: str, points: str, order: int) -> dict:
    return {
        "level_code": code,
        "points": points,
        "descriptor": f"Published level {code}",
        "positive_example": None,
        "negative_example": None,
        "display_order": order,
    }


def _rule(
    *,
    code: str = RULE_CODE,
    criterion_code: str = CRITERION_CODE,
    direction: str = "deduct",
    effect_type: str = "score",
    judge_type: str = "semantic",
    repeat_policy: str | None = "once",
    max_points: str | None = "2",
    cap_points: str | None = None,
    depends_on: tuple[str, ...] = (),
    mutex_group: str | None = None,
    levels: list[dict] | None = None,
    evidence_requirement: str = "required",
) -> dict:
    if levels is None:
        levels = []
    if judge_type == "deterministic":
        checker_key = CHECKER_KEY
        checker_version = CHECKER_VERSION
        checker_params = {"fixture_key": code}
        evidence_mode = "deterministic_observation"
    else:
        checker_key = None
        checker_version = None
        checker_params = {}
        evidence_mode = "source_quote"
    evidence_policy = {
        "mode": evidence_mode,
        "requirement": evidence_requirement,
        "minimum_coverage": "1",
    }
    if judge_type == "semantic":
        # Semantic finding codes are rule-authored.  Deterministic finding
        # authorization belongs exclusively to the checker observation schema.
        evidence_policy["allowed_finding_codes"] = list(ALLOWED_FINDING_CODES)
    return {
        # M3's @1 contract is frozen.  M4 uses @2 because finding-code
        # authorization is part of the immutable scoring identity.
        "schema_version": "atomic-rule-snapshot@2",
        "rule_code": code,
        "criterion_code": criterion_code,
        "direction": direction,
        "effect_type": effect_type,
        "judge_type": judge_type,
        "checker_key": checker_key,
        "checker_version": checker_version,
        "checker_params": checker_params,
        # M4 freezes semantic finding authorization in AtomicRule @2.  The @1
        # DTO remains readable; deterministic @2 rules still do not own an
        # allow-list because their checker registration is authoritative.
        "evidence_policy": evidence_policy,
        "max_points": max_points,
        "repeat_policy": repeat_policy,
        "cap_points": cap_points,
        "depends_on_rule_codes": list(depends_on),
        "mutex_group": mutex_group,
        "levels": deepcopy(levels),
    }


def _deduct_rule(**overrides) -> dict:
    return _rule(**overrides)


def _as_frozen_v1_rule(rule: dict) -> dict:
    """Project an M4 fixture onto the exact M3 AtomicRuleSnapshot contract."""

    value = deepcopy(rule)
    value["schema_version"] = "atomic-rule-snapshot@1"
    value["evidence_policy"].pop("allowed_finding_codes", None)
    return value


def _band_rule(**overrides) -> dict:
    values = {
        "direction": "band",
        "effect_type": "score",
        "repeat_policy": None,
        "max_points": None,
        "cap_points": None,
        "levels": [_level("HIGH", "100", 0), _level("LOW", "60", 1)],
    }
    values.update(overrides)
    return _rule(**values)


def _none_rule(*, effect_type: str, **overrides) -> dict:
    values = {
        "direction": "none",
        "effect_type": effect_type,
        "repeat_policy": None,
        "max_points": None,
        "cap_points": None,
        "levels": [],
    }
    values.update(overrides)
    return _rule(**values)


def _refresh_request_identity(request: dict) -> None:
    plan = request["plan"]
    rubric_projection = {
        "criteria": [node["criterion_snapshot"] for node in plan["nodes"]],
        "rules": [node["atomic_rule_snapshot"] for node in plan["nodes"]],
    }
    plan["rubric_snapshot_hash"] = canonical_sha256(
        {"scheme": "compiled-rubric-snapshot-v1", **rubric_projection}
    )
    plan["rubric_version_hash"] = canonical_sha256(
        {
            "scheme": plan["rubric_hash_scheme"],
            "business_profile_key": plan["business_profile_key"],
            **rubric_projection,
        }
    )
    plan["plan_hash"] = canonical_sha256(plan_hash_projection(plan))
    request["idempotency_key"] = canonical_sha256(idempotency_projection(request))


def _request_for(
    *criterion_rules: tuple[dict, dict],
    dependency_order: list[str] | None = None,
) -> dict:
    assert criterion_rules
    request = scoring_request_payload()
    nodes_by_code = {}
    for criterion, rule in criterion_rules:
        assert criterion["criterion_code"] == rule["criterion_code"]
        nodes_by_code[rule["rule_code"]] = {
            "node_kind": "atomic_rule",
            "criterion_code": criterion["criterion_code"],
            "rule_code": rule["rule_code"],
            "criterion_snapshot": deepcopy(criterion),
            "atomic_rule_snapshot": deepcopy(rule),
        }
    order = dependency_order or sorted(nodes_by_code)
    assert set(order) == set(nodes_by_code)
    request["plan"]["nodes"] = [deepcopy(nodes_by_code[code]) for code in order]
    request["plan"]["dependency_order"] = list(order)
    used_checkers = {
        rule["checker_key"]
        for _, rule in criterion_rules
        if rule["checker_key"] is not None
    }
    request["plan"]["checker_manifest"] = {
        key: _m4_checker_manifest_entry() for key in sorted(used_checkers)
    }
    _refresh_request_identity(request)
    return request


def _unit(request: dict, index: int = 0) -> dict:
    return request["document"]["evidence_units"][index]


def _source_quote(unit: dict, *, quote: str | None = None) -> dict:
    return {
        "type": "source_quote",
        "evidence_unit_id": unit["evidence_unit_id"],
        "quote": unit["normalized_text"] if quote is None else quote,
    }


def _quote_occurrence(
    request: dict,
    index: int = 0,
    *,
    finding_code: str = "GAP",
    quote: str | None = None,
    occurrence_id: str | None = None,
    points: str | None = None,
) -> dict:
    unit = _unit(request, index)
    item = {
        "finding_code": finding_code,
        "evidence": [_source_quote(unit, quote=quote)],
        "locator": deepcopy(unit["locator"]),
    }
    if occurrence_id is not None:
        item["occurrence_id"] = occurrence_id
    if points is not None:
        item["points"] = points
    return item


def _semantic_response(
    rule_code: str,
    *,
    status: str = "triggered",
    occurrences: list[dict] | None = None,
    level_code=None,
    **untrusted_fields,
) -> dict:
    value = {
        "schema_version": "semantic-rule-response@2",
        "rule_code": rule_code,
        "status": status,
        "level_code": level_code,
        "occurrences": deepcopy(occurrences or []),
    }
    value.update(deepcopy(untrusted_fields))
    return value


def _observation(
    request: dict,
    *,
    status: str = "triggered",
    finding_code: str = "GAP",
    index: int = 0,
    **extra,
) -> dict:
    unit = _unit(request, index)
    value = {
        "status": status,
        "observation_code": "REQUIRED_FIELD_MISSING",
        "finding_code": finding_code,
        "measured_value": status != "triggered",
        "expected_value": True,
        "locator": {
            "kind": "section",
            "section_path": deepcopy(unit["section_path"]),
            "section_ordinal": unit["section_ordinal"],
        },
    }
    value.update(deepcopy(extra))
    return value


def _checker_result(*observations: dict, **extra) -> dict:
    value = {
        "schema_version": "deterministic-checker-result@1",
        "observations": [deepcopy(item) for item in observations],
    }
    value.update(deepcopy(extra))
    return value


def _execute(
    request: dict,
    *,
    checker_results=None,
    semantic_responses=None,
    registry=None,
    runtime=None,
) -> tuple[dict, _CheckerRegistry, _SemanticRuntime]:
    execute_rule_plan = _require_executor()
    checker_registry = registry or _CheckerRegistry(checker_results)
    llm_runtime = runtime or _SemanticRuntime(semantic_responses)
    result = execute_rule_plan(
        request=deepcopy(request),
        checker_registry=checker_registry,
        llm_runtime=llm_runtime,
        profile=_TechnicalProposalProfile(),
    )
    return _result_mapping(result), checker_registry, llm_runtime


def _criterion_outcome(result: dict, code: str = CRITERION_CODE) -> dict:
    matches = [
        item for item in result["criterion_outcomes"] if item["criterion_code"] == code
    ]
    assert len(matches) == 1
    return matches[0]


def _decision(result: dict, code: str = RULE_CODE) -> dict:
    matches = [item for item in result["rule_decisions"] if item["rule_code"] == code]
    assert len(matches) == 1
    return matches[0]


def _issues(result: dict, code: str) -> list[dict]:
    return [item for item in result["review_issues"] if item["code"] == code]


def _occurrence_payload(
    request: dict,
    rule: dict,
    *,
    finding_code: str,
    evidence_unit_ids: list[str],
    locator: dict,
) -> dict:
    return {
        "scheme": OCCURRENCE_SCHEME,
        "document_snapshot_hash": request["document"]["document_snapshot_hash"],
        "rubric_hash_scheme": request["plan"]["rubric_hash_scheme"],
        "rubric_version_hash": request["plan"]["rubric_version_hash"],
        "criterion_code": rule["criterion_code"],
        "rule_code": rule["rule_code"],
        "finding_code": finding_code,
        "evidence_unit_ids": sorted(set(evidence_unit_ids)),
        "locator": deepcopy(locator),
    }


def _assert_complete_result_shape(result: dict) -> None:
    assert set(result) == {
        "schema_version",
        "status",
        "criterion_outcomes",
        "rule_decisions",
        "score_contributions",
        "review_issues",
    }
    assert result["schema_version"] == RESULT_SCHEMA_VERSION
    assert result["status"] in {"completed", "review_required", "blocked"}
    for item in result["criterion_outcomes"]:
        assert set(item) == {
            "criterion_code",
            "status",
            "auto_score",
            "final_score",
            "max_score",
        }
    for item in result["rule_decisions"]:
        assert set(item) == {
            "version_hash",
            "rule_code",
            "criterion_code",
            "direction",
            "effect_type",
            "status",
            "selected_level_code",
            "evidence_refs",
            "occurrences",
            "calculated_effect",
        }
        for occurrence in item["occurrences"]:
            assert set(occurrence) == {
                "occurrence_id",
                "occurrence_payload",
                "evidence_refs",
                "calculated_effect",
            }
    for item in result["score_contributions"]:
        assert set(item) == {
            "criterion_code",
            "rule_code",
            "occurrence_id",
            "kind",
            "amount",
        }
    for item in result["review_issues"]:
        assert set(item) == {
            "code",
            "severity",
            "criterion_code",
            "rule_code",
            "message",
        }


@requires_rule_executor
def test_execute_rule_plan_has_a_pure_keyword_only_ports_signature():
    execute_rule_plan = _require_executor()
    signature = inspect.signature(execute_rule_plan)

    assert list(signature.parameters) == [
        "request",
        "checker_registry",
        "llm_runtime",
        "profile",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )


@requires_rule_executor
def test_plan_v2_reads_frozen_atomic_rule_v1_and_new_v2_nodes_together():
    legacy_rule = _as_frozen_v1_rule(
        _deduct_rule(code="proposal.legacy_v1.v1", judge_type="deterministic")
    )
    m4_rule = _deduct_rule(code="proposal.finding_allowlist.v2")
    criterion = _criterion()
    request = _request_for(
        (criterion, legacy_rule),
        (criterion, m4_rule),
        dependency_order=[legacy_rule["rule_code"], m4_rule["rule_code"]],
    )
    legacy_observation = _observation(request, status="not_triggered")
    # @1 did not authorize a finding-code enum.  An untriggered observation
    # therefore carries no occurrence finding identity.
    legacy_observation.pop("finding_code")

    result, _, _ = _execute(
        request,
        checker_results={
            legacy_rule["rule_code"]: _checker_result(legacy_observation)
        },
        semantic_responses={
            m4_rule["rule_code"]: _semantic_response(
                m4_rule["rule_code"], status="not_triggered"
            )
        },
    )

    assert request["plan"]["schema_version"] == "rule-execution-plan@2"
    assert [
        node["atomic_rule_snapshot"]["schema_version"]
        for node in request["plan"]["nodes"]
    ] == ["atomic-rule-snapshot@1", "atomic-rule-snapshot@2"]
    assert [item["status"] for item in result["rule_decisions"]] == [
        "not_triggered",
        "not_triggered",
    ]
    assert _criterion_outcome(result)["auto_score"] == "100"


@requires_rule_executor
def test_rule_execution_result_freezes_complete_audit_objects():
    rule = _deduct_rule()
    request = _request_for((_criterion(), rule))
    response = _semantic_response(
        rule["rule_code"], occurrences=[_quote_occurrence(request)]
    )

    result, _, _ = _execute(
        request, semantic_responses={rule["rule_code"]: response}
    )

    _assert_complete_result_shape(result)
    assert result["status"] == "completed"
    assert _criterion_outcome(result) == {
        "criterion_code": CRITERION_CODE,
        "status": "calculated",
        "auto_score": "98",
        "final_score": "98",
        "max_score": "100",
    }
    decision = _decision(result)
    assert decision["version_hash"] == request["plan"]["rubric_version_hash"]
    assert decision["criterion_code"] == CRITERION_CODE
    assert decision["direction"] == "deduct"
    assert decision["effect_type"] == "score"
    assert decision["status"] == "triggered"
    assert decision["selected_level_code"] is None
    assert decision["calculated_effect"] == "-2"
    assert len(decision["occurrences"]) == 1
    assert result["score_contributions"] == [
        {
            "criterion_code": CRITERION_CODE,
            "rule_code": None,
            "occurrence_id": None,
            "kind": "base",
            "amount": "100",
        },
        {
            "criterion_code": CRITERION_CODE,
            "rule_code": RULE_CODE,
            "occurrence_id": decision["occurrences"][0]["occurrence_id"],
            "kind": "deduction",
            "amount": "-2",
        },
    ]
    assert result["review_issues"] == []


@requires_rule_executor
@pytest.mark.parametrize(
    ("observation_status", "expected_status"),
    (
        ("triggered", "triggered"),
        ("not_triggered", "not_triggered"),
        ("not_applicable", "not_applicable"),
    ),
)
def test_deterministic_observation_maps_to_system_rule_decision_status(
    observation_status,
    expected_status,
):
    rule = _deduct_rule(judge_type="deterministic")
    request = _request_for((_criterion(), rule))
    observation = _observation(request, status=observation_status)

    result, _, _ = _execute(
        request,
        checker_results={rule["rule_code"]: _checker_result(observation)},
    )

    assert _decision(result)["status"] == expected_status
    expected_score = "98" if observation_status == "triggered" else "100"
    assert _criterion_outcome(result)["auto_score"] == expected_score


@requires_rule_executor
def test_invalid_evidence_produces_invalid_decision_instead_of_full_score():
    rule = _deduct_rule()
    request = _request_for((_criterion(), rule))
    occurrence = _quote_occurrence(request, quote="该句并不存在于授权证据单元")

    result, _, _ = _execute(
        request,
        semantic_responses={
            rule["rule_code"]: _semantic_response(
                rule["rule_code"], occurrences=[occurrence]
            )
        },
    )

    assert _decision(result)["status"] == "invalid"
    assert _criterion_outcome(result) == {
        "criterion_code": CRITERION_CODE,
        "status": "invalid",
        "auto_score": None,
        "final_score": None,
        "max_score": "100",
    }
    assert result["status"] == "blocked"
    assert _issues(result, "REQUIRED_EVIDENCE_INVALID")


@requires_rule_executor
def test_unsatisfied_dependency_derives_skipped_status_in_core():
    prerequisite = _deduct_rule(code="proposal.prerequisite.v1")
    dependent = _deduct_rule(
        code="proposal.dependent.v1",
        depends_on=(prerequisite["rule_code"],),
    )
    criterion = _criterion()
    request = _request_for(
        (criterion, prerequisite),
        (criterion, dependent),
        dependency_order=[prerequisite["rule_code"], dependent["rule_code"]],
    )

    result, _, runtime = _execute(
        request,
        semantic_responses={
            prerequisite["rule_code"]: _semantic_response(
                prerequisite["rule_code"], status="not_triggered"
            ),
            # If called, this would trigger.  Core must skip it without asking
            # the model because the AND dependency is unsatisfied.
            dependent["rule_code"]: _semantic_response(
                dependent["rule_code"],
                occurrences=[_quote_occurrence(request)],
            ),
        },
    )

    assert _decision(result, prerequisite["rule_code"])["status"] == "not_triggered"
    assert _decision(result, dependent["rule_code"])["status"] == "skipped"
    assert [
        envelope["atomic_rule_snapshot"]["rule_code"]
        for envelope in runtime.envelopes
    ] == [prerequisite["rule_code"]]


@requires_rule_executor
def test_dependency_and_uses_stable_topology_not_plan_input_order():
    root_a = _deduct_rule(
        code="proposal.root_a.v1", judge_type="deterministic", max_points="1"
    )
    root_b = _deduct_rule(
        code="proposal.root_b.v1", judge_type="deterministic", max_points="2"
    )
    dependent = _deduct_rule(
        code="proposal.dependent.v1",
        judge_type="deterministic",
        max_points="3",
        # Deliberately reverse dependency declaration order as well.
        depends_on=(root_b["rule_code"], root_a["rule_code"]),
    )
    criterion = _criterion()
    request = _request_for(
        (criterion, dependent),
        (criterion, root_b),
        (criterion, root_a),
        # An executor that blindly loops this list would run the child first.
        dependency_order=[
            dependent["rule_code"],
            root_b["rule_code"],
            root_a["rule_code"],
        ],
    )
    checker_results = {
        root_a["rule_code"]: _checker_result(
            _observation(request, finding_code="GAP_A", index=0)
        ),
        root_b["rule_code"]: _checker_result(
            _observation(request, finding_code="GAP_B", index=1)
        ),
        dependent["rule_code"]: _checker_result(
            _observation(request, finding_code="GAP_C", index=2)
        ),
    }

    result, registry, _ = _execute(request, checker_results=checker_results)

    stable_order = [
        root_a["rule_code"],
        root_b["rule_code"],
        dependent["rule_code"],
    ]
    assert [item["rule_code"] for item in result["rule_decisions"]] == stable_order
    assert [
        call["params"]["fixture_key"] for call in registry.checker_calls
    ] == stable_order
    assert _criterion_outcome(result)["auto_score"] == "94"


@requires_rule_executor
def test_dependency_and_skips_child_without_calling_its_checker():
    root_a = _deduct_rule(
        code="proposal.root_a.v1", judge_type="deterministic"
    )
    root_b = _deduct_rule(
        code="proposal.root_b.v1", judge_type="deterministic"
    )
    dependent = _deduct_rule(
        code="proposal.dependent.v1",
        judge_type="deterministic",
        depends_on=(root_a["rule_code"], root_b["rule_code"]),
    )
    criterion = _criterion()
    request = _request_for(
        (criterion, dependent),
        (criterion, root_b),
        (criterion, root_a),
        dependency_order=[
            dependent["rule_code"],
            root_b["rule_code"],
            root_a["rule_code"],
        ],
    )
    checker_results = {
        root_a["rule_code"]: _checker_result(
            _observation(request, status="triggered", finding_code="GAP_A")
        ),
        root_b["rule_code"]: _checker_result(
            _observation(
                request,
                status="not_triggered",
                finding_code="GAP_B",
                index=1,
            )
        ),
        # Present only to prove it is not resolved/invoked.
        dependent["rule_code"]: _checker_result(
            _observation(request, status="triggered", finding_code="GAP_C", index=2)
        ),
    }

    result, registry, _ = _execute(request, checker_results=checker_results)

    assert [
        (item["rule_code"], item["status"]) for item in result["rule_decisions"]
    ] == [
        (root_a["rule_code"], "triggered"),
        (root_b["rule_code"], "not_triggered"),
        (dependent["rule_code"], "skipped"),
    ]
    assert [
        call["params"]["fixture_key"] for call in registry.checker_calls
    ] == [root_a["rule_code"], root_b["rule_code"]]
    assert _criterion_outcome(result)["auto_score"] == "98"


@requires_rule_executor
def test_invalid_prerequisite_skips_dependent_and_preserves_original_issue():
    prerequisite = _deduct_rule(code="proposal.invalid_prerequisite.v1")
    dependent = _deduct_rule(
        code="proposal.invalid_dependent.v1",
        judge_type="deterministic",
        depends_on=(prerequisite["rule_code"],),
    )
    criterion = _criterion()
    request = _request_for(
        (criterion, dependent),
        (criterion, prerequisite),
        # Deliberately reverse the plan list; dependency topology is authoritative.
        dependency_order=[dependent["rule_code"], prerequisite["rule_code"]],
    )
    invalid_occurrence = _quote_occurrence(
        request, quote="this quote is not present in the authorized unit"
    )
    registry = _CheckerRegistry(
        {
            dependent["rule_code"]: _checker_result(
                _observation(request, status="triggered")
            )
        }
    )

    result, registry, runtime = _execute(
        request,
        registry=registry,
        semantic_responses={
            prerequisite["rule_code"]: _semantic_response(
                prerequisite["rule_code"], occurrences=[invalid_occurrence]
            )
        },
    )

    assert [
        (item["rule_code"], item["status"]) for item in result["rule_decisions"]
    ] == [
        (prerequisite["rule_code"], "invalid"),
        (dependent["rule_code"], "skipped"),
    ]
    assert registry.resolve_calls == []
    assert registry.checker_calls == []
    assert [
        envelope["atomic_rule_snapshot"]["rule_code"]
        for envelope in runtime.envelopes
    ] == [prerequisite["rule_code"]]
    assert result["status"] == "blocked"
    assert _criterion_outcome(result) == {
        "criterion_code": CRITERION_CODE,
        "status": "invalid",
        "auto_score": None,
        "final_score": None,
        "max_score": "100",
    }
    original_issues = _issues(result, "REQUIRED_EVIDENCE_INVALID")
    assert len(original_issues) == 1
    assert original_issues[0]["rule_code"] == prerequisite["rule_code"]
    assert result["score_contributions"] == []


@requires_rule_executor
def test_blocking_prerequisite_skips_dependent_and_preserves_block_issue():
    prerequisite = _none_rule(
        code="proposal.blocking_prerequisite.v1",
        effect_type="block_submission",
    )
    dependent = _deduct_rule(
        code="proposal.blocked_dependent.v1",
        judge_type="deterministic",
        depends_on=(prerequisite["rule_code"],),
    )
    criterion = _criterion()
    request = _request_for(
        (criterion, dependent),
        (criterion, prerequisite),
        dependency_order=[dependent["rule_code"], prerequisite["rule_code"]],
    )
    registry = _CheckerRegistry(
        {
            dependent["rule_code"]: _checker_result(
                _observation(request, status="triggered")
            )
        }
    )

    result, registry, runtime = _execute(
        request,
        registry=registry,
        semantic_responses={
            prerequisite["rule_code"]: _semantic_response(
                prerequisite["rule_code"],
                occurrences=[_quote_occurrence(request)],
            )
        },
    )

    assert [
        (item["rule_code"], item["status"]) for item in result["rule_decisions"]
    ] == [
        (prerequisite["rule_code"], "triggered"),
        (dependent["rule_code"], "skipped"),
    ]
    assert registry.resolve_calls == []
    assert registry.checker_calls == []
    assert [
        envelope["atomic_rule_snapshot"]["rule_code"]
        for envelope in runtime.envelopes
    ] == [prerequisite["rule_code"]]
    assert result["status"] == "blocked"
    assert _criterion_outcome(result) == {
        "criterion_code": CRITERION_CODE,
        "status": "blocked",
        "auto_score": None,
        "final_score": None,
        "max_score": "100",
    }
    original_issues = _issues(result, "BLOCK_SUBMISSION")
    assert len(original_issues) == 1
    assert original_issues[0]["rule_code"] == prerequisite["rule_code"]
    assert result["score_contributions"] == []


@requires_rule_executor
@pytest.mark.parametrize(
    ("trigger_a", "trigger_b", "expected_score"),
    (
        (False, False, "100"),
        (True, False, "98"),
        (False, True, "97"),
        (True, True, None),
    ),
)
def test_mutex_group_allows_zero_or_one_trigger_and_fails_closed_on_two(
    trigger_a,
    trigger_b,
    expected_score,
):
    rule_a = _deduct_rule(
        code="proposal.mutex_a.v1",
        judge_type="deterministic",
        max_points="2",
        mutex_group="quality-gap-choice",
    )
    rule_b = _deduct_rule(
        code="proposal.mutex_b.v1",
        judge_type="deterministic",
        max_points="3",
        mutex_group="quality-gap-choice",
    )
    criterion = _criterion()
    request = _request_for(
        (criterion, rule_a),
        (criterion, rule_b),
        dependency_order=[rule_a["rule_code"], rule_b["rule_code"]],
    )
    checker_results = {
        rule_a["rule_code"]: _checker_result(
            _observation(
                request,
                status="triggered" if trigger_a else "not_triggered",
                finding_code="GAP_A",
                index=0,
            )
        ),
        rule_b["rule_code"]: _checker_result(
            _observation(
                request,
                status="triggered" if trigger_b else "not_triggered",
                finding_code="GAP_B",
                index=1,
            )
        ),
    }

    result, _, _ = _execute(request, checker_results=checker_results)

    if expected_score is not None:
        assert result["status"] == "completed"
        assert _criterion_outcome(result)["auto_score"] == expected_score
        assert not _issues(result, "MUTEX_CONFLICT")
    else:
        assert result["status"] == "blocked"
        assert _criterion_outcome(result) == {
            "criterion_code": CRITERION_CODE,
            "status": "invalid",
            "auto_score": None,
            "final_score": None,
            "max_score": "100",
        }
        assert [item["status"] for item in result["rule_decisions"]] == [
            "triggered",
            "triggered",
        ]
        assert _issues(result, "MUTEX_CONFLICT")
        assert result["score_contributions"] == []


@requires_rule_executor
@pytest.mark.parametrize(
    ("direction", "effect_type"),
    (
        ("deduct", "score"),
        ("band", "score"),
        ("none", "review"),
        ("none", "block_submission"),
        ("none", "report_only"),
    ),
)
def test_direction_effect_legal_matrix_is_executable(direction, effect_type):
    if direction == "band":
        criterion = _criterion(mode="band")
        rule = _band_rule()
        response = _semantic_response(
            rule["rule_code"],
            level_code="HIGH",
            occurrences=[_quote_occurrence(_request_for((criterion, rule)))],
        )
    elif direction == "deduct":
        criterion = _criterion()
        rule = _deduct_rule()
        request = _request_for((criterion, rule))
        response = _semantic_response(rule["rule_code"], status="not_triggered")
    else:
        criterion = _criterion(mode="review_only")
        rule = _none_rule(effect_type=effect_type)
        request = _request_for((criterion, rule))
        response = _semantic_response(rule["rule_code"], status="not_triggered")
    request = _request_for((criterion, rule))

    result, _, _ = _execute(
        request, semantic_responses={rule["rule_code"]: response}
    )

    assert not _issues(result, "RULE_EFFECT_MATRIX_INVALID")
    assert _decision(result)["status"] != "invalid"


@requires_rule_executor
@pytest.mark.parametrize(
    ("direction", "effect_type"),
    (
        ("band", "review"),
        ("band", "block_submission"),
        ("band", "report_only"),
        ("deduct", "review"),
        ("deduct", "block_submission"),
        ("deduct", "report_only"),
        ("none", "score"),
    ),
)
def test_illegal_direction_effect_pair_fails_closed(direction, effect_type):
    if direction == "band":
        rule = _band_rule(effect_type=effect_type)
        criterion = _criterion(mode="band")
    elif direction == "deduct":
        rule = _deduct_rule(effect_type=effect_type)
        criterion = _criterion()
    else:
        rule = _none_rule(effect_type=effect_type)
        criterion = _criterion(mode="review_only")
    request = _request_for((criterion, rule))

    result, _, _ = _execute(request, semantic_responses={})

    assert result["status"] == "blocked"
    assert _decision(result)["status"] == "invalid"
    assert _issues(result, "RULE_EFFECT_MATRIX_INVALID")
    assert _criterion_outcome(result)["auto_score"] is None


@requires_rule_executor
@pytest.mark.parametrize(
    "effect_type", ("score", "review", "block_submission", "report_only")
)
def test_bonus_is_reserved_and_always_blocked_in_v1(effect_type):
    rule = _rule(direction="bonus", effect_type=effect_type)
    request = _request_for((_criterion(), rule))

    result, _, _ = _execute(request, semantic_responses={})

    assert result["status"] == "blocked"
    assert _decision(result)["status"] == "invalid"
    assert _issues(result, "BONUS_NOT_ENABLED")
    assert result["score_contributions"] == []


@requires_rule_executor
def test_deduct_criterion_starts_at_max_and_applies_rules_in_plan_order():
    rule_a = _deduct_rule(
        code="proposal.gap_a.v1", judge_type="deterministic", max_points="2"
    )
    rule_b = _deduct_rule(
        code="proposal.gap_b.v1", judge_type="deterministic", max_points="3"
    )
    criterion = _criterion(max_score="100")
    request = _request_for(
        (criterion, rule_a),
        (criterion, rule_b),
        dependency_order=[rule_a["rule_code"], rule_b["rule_code"]],
    )
    observations = {
        rule_a["rule_code"]: _checker_result(
            _observation(request, finding_code="GAP_A", index=0)
        ),
        rule_b["rule_code"]: _checker_result(
            _observation(request, finding_code="GAP_B", index=1)
        ),
    }

    result, _, _ = _execute(request, checker_results=observations)

    assert _criterion_outcome(result)["auto_score"] == "95"
    assert [
        (item["kind"], item["rule_code"], item["amount"])
        for item in result["score_contributions"]
    ] == [
        ("base", None, "100"),
        ("deduction", rule_a["rule_code"], "-2"),
        ("deduction", rule_b["rule_code"], "-3"),
    ]
    assert [item["rule_code"] for item in result["rule_decisions"]] == [
        rule_a["rule_code"],
        rule_b["rule_code"],
    ]


@requires_rule_executor
def test_band_uses_only_a_published_level_and_never_a_reported_score():
    rule = _band_rule()
    request = _request_for((_criterion(mode="band"), rule))
    response = _semantic_response(
        rule["rule_code"],
        level_code="LOW",
        occurrences=[_quote_occurrence(request, points="999")],
        points="999",
    )

    result, _, _ = _execute(
        request, semantic_responses={rule["rule_code"]: response}
    )

    assert _criterion_outcome(result)["auto_score"] == "60"
    decision = _decision(result)
    assert decision["selected_level_code"] == "LOW"
    assert decision["calculated_effect"] == "60"
    assert [item["kind"] for item in result["score_contributions"]] == ["band"]
    assert result["score_contributions"][0]["amount"] == "60"


@requires_rule_executor
@pytest.mark.parametrize("level_code", (None, "UNKNOWN", ["HIGH", "LOW"]))
def test_band_requires_exactly_one_existing_published_level(level_code):
    rule = _band_rule()
    request = _request_for((_criterion(mode="band"), rule))
    response = _semantic_response(
        rule["rule_code"],
        level_code=level_code,
        occurrences=[_quote_occurrence(request)],
    )

    result, _, _ = _execute(
        request, semantic_responses={rule["rule_code"]: response}
    )

    assert _decision(result)["status"] == "invalid"
    assert _criterion_outcome(result)["auto_score"] is None
    assert _issues(result, "BAND_SELECTION_INVALID")


def _deduct_and_none_request(effect_type: str) -> tuple[dict, dict, dict]:
    score_rule = _deduct_rule(code="proposal.score.v1")
    none_rule = _none_rule(code=f"proposal.{effect_type}.v1", effect_type=effect_type)
    criterion = _criterion()
    request = _request_for(
        (criterion, score_rule),
        (criterion, none_rule),
        dependency_order=[score_rule["rule_code"], none_rule["rule_code"]],
    )
    return request, score_rule, none_rule


@requires_rule_executor
def test_review_effect_requires_review_without_becoming_a_score_effect():
    request, score_rule, review_rule = _deduct_and_none_request("review")
    responses = {
        score_rule["rule_code"]: _semantic_response(
            score_rule["rule_code"], status="not_triggered"
        ),
        review_rule["rule_code"]: _semantic_response(
            review_rule["rule_code"],
            occurrences=[_quote_occurrence(request)],
        ),
    }

    result, _, _ = _execute(request, semantic_responses=responses)

    assert result["status"] == "review_required"
    assert _criterion_outcome(result) == {
        "criterion_code": CRITERION_CODE,
        "status": "review_required",
        "auto_score": "100",
        "final_score": None,
        "max_score": "100",
    }
    assert _decision(result, review_rule["rule_code"])["calculated_effect"] is None
    assert _issues(result, "REVIEW_REQUIRED")
    assert all(
        item["rule_code"] != review_rule["rule_code"]
        for item in result["score_contributions"]
    )


@requires_rule_executor
def test_block_submission_effect_clears_criterion_scores_and_blocks_result():
    request, score_rule, block_rule = _deduct_and_none_request("block_submission")
    responses = {
        score_rule["rule_code"]: _semantic_response(
            score_rule["rule_code"], status="not_triggered"
        ),
        block_rule["rule_code"]: _semantic_response(
            block_rule["rule_code"],
            occurrences=[_quote_occurrence(request)],
        ),
    }

    result, _, _ = _execute(request, semantic_responses=responses)

    assert result["status"] == "blocked"
    assert _criterion_outcome(result) == {
        "criterion_code": CRITERION_CODE,
        "status": "blocked",
        "auto_score": None,
        "final_score": None,
        "max_score": "100",
    }
    assert _issues(result, "BLOCK_SUBMISSION")
    assert _decision(result, block_rule["rule_code"])["calculated_effect"] is None


@requires_rule_executor
def test_report_only_effect_preserves_score_and_emits_an_info_issue():
    request, score_rule, report_rule = _deduct_and_none_request("report_only")
    responses = {
        score_rule["rule_code"]: _semantic_response(
            score_rule["rule_code"], status="not_triggered"
        ),
        report_rule["rule_code"]: _semantic_response(
            report_rule["rule_code"],
            occurrences=[_quote_occurrence(request)],
        ),
    }

    result, _, _ = _execute(request, semantic_responses=responses)

    assert result["status"] == "completed"
    assert _criterion_outcome(result)["auto_score"] == "100"
    assert _criterion_outcome(result)["final_score"] == "100"
    issues = _issues(result, "REPORT_ONLY")
    assert len(issues) == 1 and issues[0]["severity"] == "info"
    assert _decision(result, report_rule["rule_code"])["calculated_effect"] is None


@requires_rule_executor
def test_review_only_criterion_never_starts_from_max_or_auto_awards_points():
    rule = _none_rule(effect_type="review")
    request = _request_for((_criterion(mode="review_only"), rule))
    response = _semantic_response(
        rule["rule_code"], occurrences=[_quote_occurrence(request)]
    )

    result, _, _ = _execute(
        request, semantic_responses={rule["rule_code"]: response}
    )

    assert _criterion_outcome(result) == {
        "criterion_code": CRITERION_CODE,
        "status": "review_required",
        "auto_score": None,
        "final_score": None,
        "max_score": "100",
    }
    assert result["score_contributions"] == []


@requires_rule_executor
@pytest.mark.parametrize(
    ("repeat_policy", "cap_points", "expected_score", "expected_amounts"),
    (
        ("once", None, "98", [Decimal("-2")]),
        ("per_occurrence", None, "94", [Decimal("-2")] * 3),
        ("capped", "5", "95", [Decimal("-2"), Decimal("-2"), Decimal("-1")]),
    ),
)
def test_repeat_policy_allocates_only_rule_authorized_points(
    repeat_policy,
    cap_points,
    expected_score,
    expected_amounts,
):
    rule = _deduct_rule(repeat_policy=repeat_policy, cap_points=cap_points)
    request = _request_for((_criterion(), rule))
    occurrences = [_quote_occurrence(request, index) for index in range(3)]
    response = _semantic_response(rule["rule_code"], occurrences=occurrences)

    result, _, _ = _execute(
        request, semantic_responses={rule["rule_code"]: response}
    )

    assert _criterion_outcome(result)["auto_score"] == expected_score
    deductions = [
        item
        for item in result["score_contributions"]
        if item["kind"] == "deduction"
    ]
    # Allocation order is occurrence_id order.  capped may therefore end with
    # a partial final effect even when cap_points is not divisible by max_points.
    assert [item["occurrence_id"] for item in deductions] == sorted(
        item["occurrence_id"] for item in deductions
    )
    assert [Decimal(item["amount"]) for item in deductions] == expected_amounts
    assert Decimal(_decision(result)["calculated_effect"]) == sum(
        expected_amounts, Decimal("0")
    )


@requires_rule_executor
def test_per_occurrence_runtime_overflow_is_invalid_and_never_clamped():
    rule = _deduct_rule(repeat_policy="per_occurrence", max_points="40")
    request = _request_for((_criterion(), rule))
    response = _semantic_response(
        rule["rule_code"],
        occurrences=[_quote_occurrence(request, index) for index in range(3)],
    )

    result, _, _ = _execute(
        request, semantic_responses={rule["rule_code"]: response}
    )

    assert result["status"] == "blocked"
    assert _decision(result)["status"] == "invalid"
    assert _criterion_outcome(result)["auto_score"] is None
    assert _issues(result, "DEDUCTION_EXCEEDS_CRITERION_MAX")
    assert not any(
        item["kind"] == "deduction" for item in result["score_contributions"]
    )


@requires_rule_executor
def test_multiple_rules_cannot_cumulatively_drive_criterion_below_zero():
    rule_a = _deduct_rule(
        code="proposal.cumulative_a.v1",
        judge_type="deterministic",
        max_points="60",
    )
    rule_b = _deduct_rule(
        code="proposal.cumulative_b.v1",
        judge_type="deterministic",
        max_points="60",
    )
    criterion = _criterion(max_score="100")
    request = _request_for(
        (criterion, rule_a),
        (criterion, rule_b),
        dependency_order=[rule_a["rule_code"], rule_b["rule_code"]],
    )
    observations = {
        rule_a["rule_code"]: _checker_result(
            _observation(request, finding_code="GAP_A", index=0)
        ),
        rule_b["rule_code"]: _checker_result(
            _observation(request, finding_code="GAP_B", index=1)
        ),
    }

    result, _, _ = _execute(request, checker_results=observations)

    assert result["status"] == "blocked"
    assert _criterion_outcome(result)["auto_score"] is None
    assert _decision(result, rule_a["rule_code"])["calculated_effect"] == "-60"
    assert _decision(result, rule_b["rule_code"])["status"] == "invalid"
    assert _decision(result, rule_b["rule_code"])["calculated_effect"] is None
    assert _issues(result, "DEDUCTION_EXCEEDS_CRITERION_MAX")
    assert result["score_contributions"] == []


@requires_rule_executor
def test_occurrence_id_is_core_derived_stable_and_deduplicated():
    rule = _deduct_rule(repeat_policy="per_occurrence")
    request = _request_for((_criterion(), rule))
    first = _quote_occurrence(
        request,
        occurrence_id="f" * 64,
        points="999",
    )
    duplicate = deepcopy(first)
    # Provider ordering and forged identity cannot create another occurrence.
    duplicate["evidence"] = list(reversed(duplicate["evidence"]))
    response = _semantic_response(
        rule["rule_code"], occurrences=[duplicate, first]
    )
    unit = _unit(request)
    expected_payload = _occurrence_payload(
        request,
        rule,
        finding_code="GAP",
        evidence_unit_ids=[unit["evidence_unit_id"]],
        locator=unit["locator"],
    )
    expected_id = canonical_sha256(expected_payload)

    result, _, _ = _execute(
        request, semantic_responses={rule["rule_code"]: response}
    )

    decision = _decision(result)
    assert len(decision["occurrences"]) == 1
    occurrence = decision["occurrences"][0]
    assert occurrence["occurrence_id"] == expected_id
    assert occurrence["occurrence_id"] != "f" * 64
    assert occurrence["occurrence_payload"] == expected_payload
    assert _criterion_outcome(result)["auto_score"] == "98"


@requires_rule_executor
def test_occurrence_digest_collision_with_distinct_payloads_fails_closed(
    monkeypatch,
):
    execute_rule_plan = _require_executor()
    executor_module = importlib.import_module(EXECUTOR_MODULE)
    hash_aliases = [
        name
        for name, value in vars(executor_module).items()
        if value is canonical_sha256
    ]
    assert hash_aliases, (
        "rule_executor must use an injectable module-level canonical_sha256 "
        "alias for occurrence identity"
    )
    rule = _deduct_rule(repeat_policy="per_occurrence")
    request = _request_for((_criterion(), rule))
    response = _semantic_response(
        rule["rule_code"],
        occurrences=[
            _quote_occurrence(request, 0),
            _quote_occurrence(request, 1),
        ],
    )
    collision_payloads: list[dict] = []
    forced_digest = "d" * 64

    def colliding_hash(value):
        payload = _plain(value)
        if isinstance(payload, dict) and payload.get("scheme") == OCCURRENCE_SCHEME:
            collision_payloads.append(deepcopy(payload))
            return forced_digest
        return canonical_sha256(value)

    for name in hash_aliases:
        monkeypatch.setattr(executor_module, name, colliding_hash)
    # The cached callable must share the module globals patched above.  This
    # assertion prevents a test-only fake from claiming the injection landed.
    assert execute_rule_plan is _EXECUTE_RULE_PLAN

    result, _, _ = _execute(
        request,
        semantic_responses={rule["rule_code"]: response},
    )

    assert len(collision_payloads) >= 2, "injected occurrence hash was not called"
    distinct_payload_hashes = {
        canonical_sha256(payload) for payload in collision_payloads
    }
    assert len(distinct_payload_hashes) == 2
    assert result["status"] == "blocked"
    assert _decision(result)["status"] == "invalid"
    assert _decision(result)["calculated_effect"] is None
    assert _criterion_outcome(result) == {
        "criterion_code": CRITERION_CODE,
        "status": "invalid",
        "auto_score": None,
        "final_score": None,
        "max_score": "100",
    }
    assert _issues(result, "OCCURRENCE_HASH_COLLISION")
    assert result["score_contributions"] == []


@requires_rule_executor
def test_model_reported_points_are_untrusted_for_semantic_deduction():
    rule = _deduct_rule(max_points="3")
    request = _request_for((_criterion(), rule))
    response = _semantic_response(
        rule["rule_code"],
        occurrences=[_quote_occurrence(request, points="90")],
        points="90",
        calculated_effect="-90",
    )

    result, _, _ = _execute(
        request, semantic_responses={rule["rule_code"]: response}
    )

    assert _criterion_outcome(result)["auto_score"] == "97"
    assert _decision(result)["calculated_effect"] == "-3"
    assert [
        item["amount"]
        for item in result["score_contributions"]
        if item["kind"] == "deduction"
    ] == ["-3"]


@requires_rule_executor
def test_model_forged_rule_ref_has_zero_effect_and_fails_closed():
    rule = _deduct_rule()
    request = _request_for((_criterion(), rule))
    response = _semantic_response(
        "proposal.unpublished_forgery.v1",
        occurrences=[_quote_occurrence(request, points="100")],
        rule_ref="proposal.unpublished_forgery.v1",
        points="100",
    )

    result, _, _ = _execute(
        request, semantic_responses={rule["rule_code"]: response}
    )

    assert result["status"] == "blocked"
    assert _decision(result)["status"] == "invalid"
    assert _decision(result)["calculated_effect"] is None
    assert _criterion_outcome(result)["auto_score"] is None
    assert _issues(result, "UNKNOWN_RULE")
    assert not any(
        item["kind"] == "deduction" for item in result["score_contributions"]
    )


@requires_rule_executor
@pytest.mark.parametrize("tampering", ("unit", "quote", "finding_code"))
def test_semantic_occurrence_requires_authorized_evidence_and_finding_code(tampering):
    rule = _deduct_rule()
    request = _request_for((_criterion(), rule))
    occurrence = _quote_occurrence(request)
    if tampering == "unit":
        occurrence["evidence"][0]["evidence_unit_id"] = "e" * 64
    elif tampering == "quote":
        occurrence["evidence"][0]["quote"] = "forged quote"
    else:
        occurrence["finding_code"] = "FREE_TEXT_BYPASS"
    response = _semantic_response(rule["rule_code"], occurrences=[occurrence])

    result, _, _ = _execute(
        request, semantic_responses={rule["rule_code"]: response}
    )

    assert _decision(result)["status"] == "invalid"
    assert _decision(result)["evidence_refs"] == []
    assert _criterion_outcome(result)["auto_score"] is None
    expected_issue = (
        "FINDING_CODE_NOT_AUTHORIZED"
        if tampering == "finding_code"
        else "REQUIRED_EVIDENCE_INVALID"
    )
    assert _issues(result, expected_issue)


@requires_rule_executor
def test_authorized_quote_is_bound_to_validated_unit_and_canonical_locator():
    rule = _deduct_rule()
    request = _request_for((_criterion(), rule))
    response = _semantic_response(
        rule["rule_code"], occurrences=[_quote_occurrence(request)]
    )

    result, _, _ = _execute(
        request, semantic_responses={rule["rule_code"]: response}
    )

    unit = _unit(request)
    decision = _decision(result)
    assert len(decision["evidence_refs"]) == 1
    evidence = decision["evidence_refs"][0]
    assert set(evidence) == {
        "evidence_unit_id",
        "evidence_type",
        "locator",
        "payload_hash",
    }
    assert evidence["evidence_unit_id"] == unit["evidence_unit_id"]
    assert evidence["evidence_type"] == "source_quote"
    assert evidence["locator"] == unit["locator"]
    assert len(evidence["payload_hash"]) == 64
    assert decision["occurrences"][0]["evidence_refs"] == decision["evidence_refs"]


@requires_rule_executor
def test_checker_returns_observation_only_and_rule_executor_authorizes_points():
    rule = _deduct_rule(judge_type="deterministic", max_points="7")
    assert "allowed_finding_codes" not in rule["evidence_policy"]
    request = _request_for((_criterion(), rule))
    assert request["plan"]["checker_manifest"][CHECKER_KEY][
        "observation_schema"
    ] == CHECKER_OBSERVATION_SCHEMA
    observation = _observation(request)
    registry = _CheckerRegistry(
        {rule["rule_code"]: _checker_result(observation)}
    )
    runtime_manifest = registry.manifest(
        profile_key=PROFILE_KEY,
        document_schema_version=request["document"]["schema_version"],
    )
    assert runtime_manifest[CHECKER_KEY][
        "observation_schema"
    ] == CHECKER_OBSERVATION_SCHEMA

    result, registry, runtime = _execute(request, registry=registry)

    assert len(registry.resolve_calls) == len(registry.checker_calls) == 1
    assert registry.resolved_observation_schemas == [CHECKER_OBSERVATION_SCHEMA]
    assert set(registry.checker_calls[0]) == {"document", "params"}
    serialized_checker_input = repr(registry.checker_calls[0])
    assert "max_points" not in serialized_checker_input
    assert "effect_type" not in serialized_checker_input
    assert runtime.envelopes == []
    assert _criterion_outcome(result)["auto_score"] == "93"
    assert _decision(result)["calculated_effect"] == "-7"


@requires_rule_executor
def test_unknown_deterministic_finding_is_not_authorized_by_a_forged_rule_allowlist():
    rule = _deduct_rule(judge_type="deterministic")
    assert "allowed_finding_codes" not in rule["evidence_policy"]
    # A tampered/unvalidated plan tries to smuggle deterministic authority into
    # the rule.  Runtime authorization must still come only from the registry's
    # frozen observation schema.
    rule["evidence_policy"]["allowed_finding_codes"] = ["FORGED_RULE_CODE"]
    request = _request_for((_criterion(), rule))
    registry_schema = request["plan"]["checker_manifest"][CHECKER_KEY][
        "observation_schema"
    ]
    assert "FORGED_RULE_CODE" not in registry_schema["allowed_finding_codes"]
    observation = _observation(
        request,
        status="triggered",
        finding_code="FORGED_RULE_CODE",
    )

    result, registry, _ = _execute(
        request,
        checker_results={rule["rule_code"]: _checker_result(observation)},
    )

    assert registry.resolved_observation_schemas == [CHECKER_OBSERVATION_SCHEMA]
    assert result["status"] == "blocked"
    assert _decision(result)["status"] == "invalid"
    assert _decision(result)["evidence_refs"] == []
    assert _decision(result)["calculated_effect"] is None
    assert _criterion_outcome(result)["auto_score"] is None
    assert _issues(result, "FINDING_CODE_NOT_AUTHORIZED")
    assert result["score_contributions"] == []


@requires_rule_executor
@pytest.mark.parametrize("forged_field", ("points", "rule_code", "calculated_effect"))
def test_checker_scoring_fields_are_rejected_as_non_observation_output(forged_field):
    rule = _deduct_rule(judge_type="deterministic")
    request = _request_for((_criterion(), rule))
    observation = _observation(request)
    observation[forged_field] = "99"

    result, _, _ = _execute(
        request,
        checker_results={rule["rule_code"]: _checker_result(observation)},
    )

    assert result["status"] == "blocked"
    assert _decision(result)["status"] == "invalid"
    assert _decision(result)["calculated_effect"] is None
    assert _issues(result, "CHECKER_OUTPUT_SCHEMA_INVALID")
    assert _criterion_outcome(result)["auto_score"] is None


@requires_rule_executor
def test_score_submission_delegates_to_rule_executor_and_preserves_full_audit(
    monkeypatch,
):
    """The public Core entry must stop using the hard-coded M3 rule subset."""

    real_execute = _require_executor()
    executor_module = importlib.import_module(EXECUTOR_MODULE)
    engine_module = importlib.import_module(
        "backend.app.services.scoring.core.engine"
    )
    rule = _deduct_rule(repeat_policy="per_occurrence", max_points="2")
    request = _request_for((_criterion(), rule))
    response = _semantic_response(
        rule["rule_code"],
        occurrences=[
            _quote_occurrence(request, 0),
            _quote_occurrence(request, 1),
        ],
    )
    registry = _CheckerRegistry()
    runtime = _SemanticRuntime({rule["rule_code"]: response})
    calls: list[dict] = []
    raw_results: list[dict] = []

    def spy(*, request, checker_registry, llm_runtime, profile):
        calls.append(
            {
                "request": _plain(request),
                "checker_registry": checker_registry,
                "llm_runtime": llm_runtime,
                "profile": profile,
            }
        )
        result = real_execute(
            request=request,
            checker_registry=checker_registry,
            llm_runtime=llm_runtime,
            profile=profile,
        )
        raw_results.append(_result_mapping(result))
        return result

    # Support either ``import rule_executor`` or ``from rule_executor import``
    # wiring while still requiring an actual delegation call.
    monkeypatch.setattr(executor_module, EXECUTOR_SYMBOL, spy)
    for name, value in tuple(vars(engine_module).items()):
        if value is real_execute:
            monkeypatch.setattr(engine_module, name, spy)

    outcome = engine_module.score_submission(
        request=deepcopy(request),
        checker_registry=registry,
        llm_runtime=runtime,
        profile=_TechnicalProposalProfile(),
    )

    assert len(calls) == len(raw_results) == 1
    assert calls[0]["request"] == request
    assert calls[0]["checker_registry"] is registry
    assert calls[0]["llm_runtime"] is runtime
    public = _result_mapping(outcome)
    raw = raw_results[0]
    # M3 scoring-outcome@1 stays frozen and parseable.  Occurrence/effect audit
    # fields require a new closed transport schema rather than extending @1.
    assert public["schema_version"] == "scoring-outcome@2"
    assert public["status"] == "completed"
    assert public["unrounded_total"] == "96"
    assert public["final_total"] == "96"
    assert public["criterion_outcomes"] == raw["criterion_outcomes"]
    assert public["rule_decisions"] == raw["rule_decisions"]
    assert public["score_contributions"] == raw["score_contributions"]
    assert public["review_issues"] == raw["review_issues"]
