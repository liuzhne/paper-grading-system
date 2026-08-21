from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend.app.services.scoring.adapters.legacy_rubric import LegacyRubricAdapter
from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.contracts import CompositeCriterionNode
from backend.app.services.scoring.core.contracts import LegacyDirectCriterionNode
from backend.app.services.scoring.core.contracts import RuleExecutionPlan
from backend.app.services.scoring.core.engine import score_submission
from backend.app.services.scoring.core.policy import compile_scoring_policy
from backend.app.tests.m3_contract_fixtures import idempotency_projection
from backend.app.tests.m3_contract_fixtures import plan_hash_projection
from backend.app.tests.m3_contract_fixtures import scoring_request_payload


@dataclass(frozen=True)
class _Profile:
    profile_key: str = "technical_proposal"
    profile_version: str = "technical-proposal-test-profile@1"
    prompt_version: str = "technical-proposal-prompt@1"

    def build_prompt_extensions(self, *, submission_snapshot, document_snapshot):
        return {"metadata": {}, "profile_extensions": {}}


class _NoCheckers:
    def resolve(self, **_kwargs):
        raise AssertionError("compatibility nodes must not resolve AtomicRule checkers")


class _CompatibilityRuntime:
    def __init__(self, responses):
        self.responses = deepcopy(responses)
        self.calls = []

    def score_legacy_criterion(self, *, request, criterion):
        self.calls.append(deepcopy(criterion))
        return deepcopy(self.responses[criterion["code"]])

    def score(self, *, envelope):
        raise AssertionError("compatibility nodes use the explicit legacy port")


def _legacy_criterion(code, maximum, *, scoring_mode="llm_direct"):
    return {
        "code": code,
        "name": code + " display name",
        "max_score": str(maximum),
        "description": "Compatibility-only criterion",
        "evidence_hints": ["evidence"],
        "criterion_type": "llm_judgment",
        "scoring_mode": scoring_mode,
        "applies_to": "global",
    }


def _direct_node(code="DIRECT", maximum="10", *, weight=None):
    node = {
        "node_kind": "legacy_direct_criterion",
        "criterion_code": code,
        "rule_code": "legacy.direct." + code.casefold(),
        "criterion_snapshot": {
            "criterion_code": code,
            "name": code + " display name",
            "max_score": maximum,
            "weight": weight,
            "assessment_mode": "legacy_direct",
        },
        "legacy_criterion": _legacy_criterion(code, maximum),
        "evidence_requirement": "required",
    }
    return LegacyDirectCriterionNode.from_mapping(node).to_mapping()


def _composite_node(*, parent_max="10", first_max="4", second_max="6"):
    node = {
        "node_kind": "composite_criterion",
        "criterion_code": "COMPOSITE",
        "rule_code": "legacy.composite.composite",
        "criterion_snapshot": {
            "criterion_code": "COMPOSITE",
            "name": "Composite display name",
            "max_score": parent_max,
            "weight": None,
            "assessment_mode": "legacy_composite",
        },
        "children": [
            _legacy_criterion("CHILD-1", first_max),
            _legacy_criterion("CHILD-2", second_max),
        ],
        "evidence_requirement": "required",
    }
    return CompositeCriterionNode.from_mapping(node).to_mapping()


def _compat_request(*nodes):
    request = scoring_request_payload()
    plan = request["plan"]
    policy_input = deepcopy(plan["policy_snapshot"])
    policy_input.pop("policy_hash", None)
    total = sum(
        (Decimal(node["criterion_snapshot"]["max_score"]) for node in nodes),
        Decimal("0"),
    )
    policy_input["aggregation"]["total_score"] = str(total)
    policy = compile_scoring_policy(policy_input, total_score=total).to_mapping()
    plan["schema_version"] = "rule-execution-plan@3"
    plan["rubric_source_kind"] = "legacy_unversioned"
    plan["rubric_version_id"] = None
    plan["rubric_version_hash"] = None
    plan["rubric_hash_scheme"] = None
    plan["nodes"] = [deepcopy(node) for node in nodes]
    plan["dependency_order"] = [node["rule_code"] for node in nodes]
    plan["checker_manifest"] = {}
    plan["policy_snapshot"] = policy
    plan["policy_hash"] = policy["policy_hash"]
    plan["plan_hash"] = canonical_sha256(plan_hash_projection(plan))
    request["idempotency_key"] = canonical_sha256(idempotency_projection(request))
    return request


def _valid_response(request, code, score, **extra):
    evidence = request["document"]["evidence_units"][0]
    return {
        "direct_score": score,
        "evidence": [
            {
                "type": "source_quote",
                "evidence_unit_id": evidence["evidence_unit_id"],
                "quote": evidence["normalized_text"],
            }
        ],
        "need_manual_review": False,
        **extra,
    }


def test_legacy_adapter_maps_direct_and_hybrid_to_immutable_nodes():
    adapter = LegacyRubricAdapter()
    direct = SimpleNamespace(**_legacy_criterion("DIRECT", 10))
    hybrid = SimpleNamespace(
        code="HYBRID",
        name="Hybrid",
        max_score=10,
        weight=None,
        description="Hybrid compatibility",
        evidence_hints=["evidence"],
        criterion_type="hybrid",
        scoring_mode="llm_direct",
        applies_to="global",
        sub_checks=[
            {"kind": "llm_judgment", "name": "first", "max_points": 4},
            {"kind": "llm_judgment", "name": "second", "max_points": 6},
        ],
    )

    nodes = adapter.adapt_compatibility_nodes(
        criteria=[direct, hybrid],
        rubric_source_kind="legacy_unversioned",
    )

    assert isinstance(nodes[0], LegacyDirectCriterionNode)
    assert isinstance(nodes[1], CompositeCriterionNode)
    assert nodes[0].criterion_code == "DIRECT"
    assert [item["max_score"] for item in nodes[1].to_mapping()["children"]] == [
        "4",
        "6",
    ]
    with pytest.raises(AttributeError):
        nodes[0].criterion_code = "tampered"


def test_unmapped_deterministic_and_deductive_criteria_use_compatibility_nodes():
    adapter = LegacyRubricAdapter()
    deterministic = SimpleNamespace(
        **{
            **_legacy_criterion("DET", 10),
            "criterion_type": "deterministic",
            "deduction_rules_structured": [],
        }
    )
    deductive = SimpleNamespace(
        **{
            **_legacy_criterion("DED", 10, scoring_mode="deductive"),
            "deduction_rules_structured": [
                {
                    "match": "missing:method",
                    "points": "2",
                    "checker_key": "",
                    "checker_params": {},
                }
            ],
        }
    )

    nodes = adapter.adapt_compatibility_nodes(
        criteria=[deterministic, deductive],
        rubric_source_kind="legacy_unversioned",
    )

    assert [node.criterion_code for node in nodes] == ["DET", "DED"]
    assert all(isinstance(node, LegacyDirectCriterionNode) for node in nodes)


def test_explicit_consistent_checker_relationship_stays_atomic():
    adapter = LegacyRubricAdapter()
    explicit = SimpleNamespace(
        **{
            **_legacy_criterion("DET", 10, scoring_mode="deductive"),
            "criterion_type": "deterministic",
            "deduction_rules_structured": [
                {
                    "match": "missing:owner",
                    "points": "2",
                    "checker_key": "thesis.legacy_required_fields.v1",
                    "checker_params": {
                        "criterion_code": "DET",
                        "applies_to": "global",
                    },
                },
                {
                    "match": "missing:reviewer",
                    "points": "3",
                    "checker_key": "thesis.legacy_required_fields.v1",
                    "checker_params": {
                        "criterion_code": "DET",
                        "applies_to": "global",
                    },
                },
            ],
        }
    )

    assert adapter.adapt_compatibility_nodes(
        criteria=[explicit],
        rubric_source_kind="legacy_unversioned",
    ) == ()


def test_conflicting_checker_relationships_fail_closed_to_compatibility():
    adapter = LegacyRubricAdapter()
    conflicting = SimpleNamespace(
        **{
            **_legacy_criterion("DET", 10, scoring_mode="deductive"),
            "criterion_type": "deterministic",
            "deduction_rules_structured": [
                {
                    "match": "missing:owner",
                    "points": "2",
                    "checker_key": "thesis.legacy_required_fields.v1",
                    "checker_params": {"criterion_code": "DET"},
                },
                {
                    "match": "missing:reviewer",
                    "points": "3",
                    "checker_key": "thesis.section_word_count.v1",
                    "checker_params": {"criterion_code": "DET"},
                },
            ],
        }
    )

    nodes = adapter.adapt_compatibility_nodes(
        criteria=[conflicting],
        rubric_source_kind="legacy_unversioned",
    )
    assert len(nodes) == 1
    assert isinstance(nodes[0], LegacyDirectCriterionNode)


def test_compatibility_nodes_are_rejected_for_formal_versions():
    adapter = LegacyRubricAdapter()
    direct = SimpleNamespace(**_legacy_criterion("DIRECT", 10))
    with pytest.raises(ValueError, match="legacy_unversioned"):
        adapter.adapt_compatibility_nodes(
            criteria=[direct],
            rubric_source_kind="published_version",
        )

    request = _compat_request(_direct_node())
    request["plan"]["rubric_source_kind"] = "published_version"
    request["plan"]["rubric_version_id"] = "version-1"
    request["plan"]["rubric_version_hash"] = "a" * 64
    request["plan"]["rubric_hash_scheme"] = "rubric-content-v1"
    request["plan"]["plan_hash"] = canonical_sha256(
        plan_hash_projection(request["plan"])
    )
    with pytest.raises(ValueError, match="legacy_unversioned"):
        RuleExecutionPlan.from_mapping(request["plan"])


def test_direct_score_is_bounded_evidence_gated_and_ignores_reported_points():
    node = _direct_node()
    request = _compat_request(node)
    runtime = _CompatibilityRuntime(
        {
            "DIRECT": _valid_response(
                request,
                "DIRECT",
                "7.5",
                points="999",
                calculated_effect="999",
            )
        }
    )

    outcome = score_submission(
        request=request,
        checker_registry=_NoCheckers(),
        llm_runtime=runtime,
        profile=_Profile(),
    ).to_mapping()

    assert outcome["status"] == "completed"
    assert outcome["criterion_outcomes"][0]["auto_score"] == "7.5"
    assert outcome["final_total"] == "7.5"
    assert outcome["score_contributions"][0]["amount"] == "7.5"

    invalid_runtime = _CompatibilityRuntime(
        {"DIRECT": _valid_response(request, "DIRECT", "10.01")}
    )
    invalid = score_submission(
        request=request,
        checker_registry=_NoCheckers(),
        llm_runtime=invalid_runtime,
        profile=_Profile(),
    ).to_mapping()
    assert invalid["status"] == "blocked"
    assert invalid["criterion_outcomes"][0]["auto_score"] is None
    assert invalid["final_total"] is None


def test_direct_required_evidence_failure_blocks_total():
    node = _direct_node()
    request = _compat_request(node)
    response = _valid_response(request, "DIRECT", "8")
    response["evidence"][0]["quote"] = "forged quote"

    outcome = score_submission(
        request=request,
        checker_registry=_NoCheckers(),
        llm_runtime=_CompatibilityRuntime({"DIRECT": response}),
        profile=_Profile(),
    ).to_mapping()

    assert outcome["status"] == "blocked"
    assert outcome["criterion_outcomes"][0]["status"] == "invalid"
    assert outcome["final_total"] is None
    assert outcome["review_issues"][0]["code"] == "REQUIRED_EVIDENCE_INVALID"


def test_composite_scores_children_independently_and_blocks_overflow():
    node = _composite_node()
    request = _compat_request(node)
    runtime = _CompatibilityRuntime(
        {
            "CHILD-1": _valid_response(request, "CHILD-1", "4"),
            "CHILD-2": _valid_response(request, "CHILD-2", "6"),
        }
    )

    completed = score_submission(
        request=request,
        checker_registry=_NoCheckers(),
        llm_runtime=runtime,
        profile=_Profile(),
    ).to_mapping()

    assert completed["criterion_outcomes"][0]["auto_score"] == "10"
    assert [item["amount"] for item in completed["score_contributions"]] == [
        "4",
        "6",
    ]
    assert [item["code"] for item in runtime.calls] == ["CHILD-1", "CHILD-2"]

    overflow_node = _composite_node(parent_max="9")
    overflow_request = _compat_request(overflow_node)
    overflow = score_submission(
        request=overflow_request,
        checker_registry=_NoCheckers(),
        llm_runtime=_CompatibilityRuntime(
            {
                "CHILD-1": _valid_response(overflow_request, "CHILD-1", "4"),
                "CHILD-2": _valid_response(overflow_request, "CHILD-2", "6"),
            }
        ),
        profile=_Profile(),
    ).to_mapping()
    assert overflow["status"] == "blocked"
    assert overflow["criterion_outcomes"][0]["auto_score"] is None
    assert overflow["final_total"] is None


def test_parent_weight_is_present_only_on_parent_criterion():
    node = _direct_node(weight="0.5")
    mapping = LegacyDirectCriterionNode.from_mapping(node).to_mapping()

    assert mapping["criterion_snapshot"]["weight"] == "0.5"
    assert "weight" not in mapping["legacy_criterion"]
