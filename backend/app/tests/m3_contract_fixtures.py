"""Shared, internally consistent fixtures for the M3 vertical-slice contract.

The factories are pure mappings so M3 tests remain importable before the
loader, registry, plan builder, engine and persistence adapters exist.  Hashes
are independently derived here to keep production implementations from
becoming their own test oracle.
"""

from __future__ import annotations

from copy import deepcopy

from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.tests.m2_contract_fixtures import (
    PROFILE_KEY,
    PROFILE_VERSION,
    runtime_identity_payload,
    technical_document_payload,
    technical_policy_snapshot_payload,
    technical_submission_payload,
)


PLAN_SCHEMA_VERSION = "rule-execution-plan@2"
PLAN_HASH_SCHEME = "rule-execution-plan-v1"
POLICY_COMPILER_VERSION = "scoring-policy-compiler@1"
ENGINE_CONTRACT_VERSION = "scoring-core@1"
IDEMPOTENCY_SCHEME = "scoring-request-idempotency-v1"

DETERMINISTIC_RULE_CODE = "proposal.risk_owner.v1"
SEMANTIC_RULE_CODE = "proposal.solution_fit.v1"
CHECKER_KEY = "technical_proposal.required_fields.v1"
CHECKER_VERSION = "1.0.0"


def deterministic_criterion_payload() -> dict:
    return {
        "criterion_code": "RISK_CONTROL",
        "name": "Risk control completeness",
        "max_score": "20",
        "weight": None,
        "assessment_mode": "deduct",
    }


def semantic_criterion_payload() -> dict:
    return {
        "criterion_code": "SOLUTION_FIT",
        "name": "Solution fit",
        "max_score": "80",
        "weight": None,
        "assessment_mode": "band",
    }


def deterministic_rule_payload() -> dict:
    return {
        "schema_version": "atomic-rule-snapshot@1",
        "rule_code": DETERMINISTIC_RULE_CODE,
        "criterion_code": "RISK_CONTROL",
        "direction": "deduct",
        "effect_type": "score",
        "judge_type": "deterministic",
        "checker_key": CHECKER_KEY,
        # The published rubric authorizes a versioned checker key.  Concrete
        # implementation identity belongs to the registry-backed execution
        # plan, not to the pre-plan database snapshot.
        "checker_version": None,
        "checker_params": {"required_fields": ["owner"]},
        "evidence_policy": {
            "mode": "scoped_absence",
            "requirement": "required",
            "minimum_coverage": "1",
        },
        "max_points": "10",
        "repeat_policy": "once",
        "cap_points": None,
        "depends_on_rule_codes": [],
        "mutex_group": None,
        "levels": [],
    }


def semantic_rule_payload() -> dict:
    # M3 only freezes one semantic band leaf.  General direction/effect,
    # repeat/cap/dependency/mutex execution remains the M4 contract.
    return {
        "schema_version": "atomic-rule-snapshot@1",
        "rule_code": SEMANTIC_RULE_CODE,
        "criterion_code": "SOLUTION_FIT",
        "direction": "band",
        "effect_type": "score",
        "judge_type": "semantic",
        "checker_key": None,
        "checker_version": None,
        "checker_params": {},
        "evidence_policy": {
            "mode": "source_quote",
            "requirement": "required",
            "minimum_coverage": "1",
        },
        "max_points": None,
        "repeat_policy": None,
        "cap_points": None,
        "depends_on_rule_codes": [],
        "mutex_group": None,
        "levels": [
            {
                "level_code": "FIT_HIGH",
                "points": "80",
                "descriptor": "Requirements and solution are explicitly aligned.",
                "positive_example": None,
                "negative_example": None,
                "display_order": 0,
            },
            {
                "level_code": "FIT_LOW",
                "points": "40",
                "descriptor": "The solution only partially addresses requirements.",
                "positive_example": None,
                "negative_example": None,
                "display_order": 1,
            },
        ],
    }


def published_version_content_payload(
    *,
    hash_scheme: str = "rubric-content-v2",
    profile_key: str = PROFILE_KEY,
) -> dict:
    """Explicit full provenance projection used by the published version hash.

    This mirrors the stable lifecycle graph shape and is intentionally larger
    than ``CompiledRubricSnapshot``.  Keeping both projections in the fixture
    prevents a loader from substituting the executable snapshot hash for the
    authoritative published-version hash.
    """

    if hash_scheme not in {"rubric-content-v1", "rubric-content-v2"}:
        raise ValueError("unsupported rubric content hash scheme")
    policy = technical_policy_snapshot_payload(total_score="100", rounding_digits=2)
    source_rule = {
        "source_rule_code": "RISK-OWNER-01",
        "sheet_name": "Atomic Rules",
        "row_number": 2,
        "cell_locator": "Atomic Rules!A2:N2",
        "raw_text": "Risk control must identify an accountable owner.",
    }
    template_item = {
        "item_code": "TPL-RISK-OWNER-01",
        "kind": "content",
        "section_path": ["总体方案", "风险控制"],
        "raw_text": "Each risk must name an accountable owner.",
        "normalized_constraint": {"required_fields": ["owner"]},
        "strictness": "required",
        "source_locator": {"paragraph_id": "w:p-42", "comment_id": "7"},
        "source_hash": "8" * 64,
        "parse_confidence": "0.9500",
    }
    template_link = {
        "relationship_type": "constraint",
        "match_method": "exact",
        "match_confidence": "1.0000",
        "rationale": "The template explicitly requires the rule field.",
    }
    content = {
        "rubric": {
            "total_score": "100.00",
            "description": "Technical proposal scoring contract.",
            "format_spec": {},
        },
        "criteria": [
            {
                "code": "RISK_CONTROL",
                "name": "Risk control completeness",
                "max_score": "20.00",
                "weight": None,
                "description": None,
                "evidence_hints": [],
                "deduction_rules": [],
                "display_order": 0,
                "criterion_type": "deterministic",
                "scoring_mode": "deductive",
                "applies_to": "risk_control",
                "rubric_levels": [],
                "sub_checks": [],
                "dimension": "content",
                "deduction_rules_structured": [],
            },
            {
                "code": "SOLUTION_FIT",
                "name": "Solution fit",
                "max_score": "80.00",
                "weight": None,
                "description": None,
                "evidence_hints": [],
                "deduction_rules": [],
                "display_order": 1,
                "criterion_type": "llm_judgment",
                "scoring_mode": "banded",
                "applies_to": "requirements_understanding",
                "rubric_levels": [],
                "sub_checks": [],
                "dimension": "content",
                "deduction_rules_structured": [],
            },
        ],
        "compilation": {
            "parser_version": "technical-proposal-rubric-parser@1",
            "compiler_version": "technical-proposal-rubric-compiler@1",
            "model_provider": None,
            "model_name": None,
            "sampling_params": {},
            "prompt_version": "technical-proposal-rubric-prompt@1",
            "raw_parse_output": {},
            "raw_model_output": {},
            "validation_result": {"valid": True},
            "blockers": [],
            "warnings": [],
        },
        "artifacts": [
            {
                "artifact": {
                    "artifact_type": "excel",
                    "file_name": "technical-proposal-rubric.xlsx",
                    "file_hash": "7" * 64,
                    "file_size_bytes": 2048,
                },
                "source_rules": [deepcopy(source_rule)],
                "templates": [],
            },
            {
                "artifact": {
                    "artifact_type": "docx",
                    "file_name": "technical-proposal-template.docx",
                    "file_hash": "9" * 64,
                    "file_size_bytes": 4096,
                },
                "source_rules": [],
                "templates": [deepcopy(template_item)],
            },
        ],
        "version": {
            "workflow_profile": "template_driven",
            "global_policy": deepcopy(policy),
        },
        "rules": [],
    }
    if hash_scheme == "rubric-content-v2":
        content["version"]["business_profile_key"] = profile_key

    criterion_by_code = {item["code"]: item for item in content["criteria"]}
    for snapshot in (deterministic_rule_payload(), semantic_rule_payload()):
        content["rules"].append(
            {
                "criterion_code": snapshot["criterion_code"],
                "rule": {
                    "rule_code": snapshot["rule_code"],
                    "name": snapshot["rule_code"],
                    "rule_text": criterion_by_code[snapshot["criterion_code"]]["name"],
                    "direction": snapshot["direction"],
                    "effect_type": snapshot["effect_type"],
                    "max_points": snapshot["max_points"],
                    "repeat_policy": snapshot["repeat_policy"],
                    "cap_points": snapshot["cap_points"],
                    "judge_type": snapshot["judge_type"],
                    "checker_key": snapshot["checker_key"],
                    "checker_params": deepcopy(snapshot["checker_params"]),
                    "evidence_policy": deepcopy(snapshot["evidence_policy"]),
                    "positive_example": None,
                    "negative_example": None,
                    "boundary_example": None,
                    "strictness": "required",
                    "applies_to": criterion_by_code[snapshot["criterion_code"]][
                        "applies_to"
                    ],
                    "mutex_group": snapshot["mutex_group"],
                    "depends_on_rule_codes": deepcopy(
                        snapshot["depends_on_rule_codes"]
                    ),
                    "creation_method": "compiler",
                },
                "levels": [
                    {
                        "level_code": item["level_code"],
                        "points": item["points"],
                        "descriptor": item["descriptor"],
                        "positive_example": item["positive_example"],
                        "negative_example": item["negative_example"],
                        "display_order": item["display_order"],
                    }
                    for item in snapshot["levels"]
                ],
                "source_rules": (
                    [deepcopy(source_rule)]
                    if snapshot["rule_code"] == DETERMINISTIC_RULE_CODE
                    else []
                ),
                "template_links": (
                    [
                        {
                            "link": deepcopy(template_link),
                            "template": deepcopy(template_item),
                        }
                    ]
                    if snapshot["rule_code"] == DETERMINISTIC_RULE_CODE
                    else []
                ),
            }
        )
    content["criteria"].sort(key=lambda item: (item["display_order"], item["code"]))
    content["rules"].sort(key=lambda item: item["rule"]["rule_code"])
    return content


def compiled_rubric_snapshot_projection(value: dict) -> dict:
    """Canonical executable snapshot projection with stable set ordering."""

    content = deepcopy(value)
    content.pop("rubric_snapshot_hash", None)
    content["criteria"] = sorted(
        content["criteria"], key=lambda item: item["criterion_code"]
    )
    content["atomic_rules"] = sorted(
        content["atomic_rules"], key=lambda item: item["rule_code"]
    )
    for rule in content["atomic_rules"]:
        rule["levels"] = sorted(
            rule["levels"],
            key=lambda item: (item["display_order"], item["level_code"]),
        )
    return {"scheme": "compiled-rubric-snapshot-v1", **content}


def published_rubric_payload(
    *,
    hash_scheme: str = "rubric-content-v2",
    profile_key: str = PROFILE_KEY,
) -> dict:
    policy = technical_policy_snapshot_payload(total_score="100", rounding_digits=2)
    version_content = published_version_content_payload(
        hash_scheme=hash_scheme,
        profile_key=profile_key,
    )
    base = {
        "schema_version": "compiled-rubric-snapshot@1",
        "rubric_source_kind": "published_version",
        "rubric_version_id": "rubric-version-technical-proposal-001",
        "hash_scheme": hash_scheme,
        "business_profile_key": profile_key,
        "total_score": "100",
        "criteria": [
            deterministic_criterion_payload(),
            semantic_criterion_payload(),
        ],
        "atomic_rules": [
            deterministic_rule_payload(),
            semantic_rule_payload(),
        ],
        "global_policy": policy,
    }
    base["version_hash"] = canonical_sha256(version_content)
    base["rubric_snapshot_hash"] = canonical_sha256(
        compiled_rubric_snapshot_projection(base)
    )
    return base


def checker_registration_payload(*, checker_version: str = CHECKER_VERSION) -> dict:
    artifact_manifest = [
        {
            "path": "profiles/technical_proposal/checkers/required_fields.py",
            "sha256": "1" * 64,
        }
    ]
    package_identity = {
        "scheme": "checker-package-sha256-v1",
        "checker_key": CHECKER_KEY,
        "checker_version": checker_version,
        "entrypoint": "technical_proposal.required_fields:check",
        "runtime_contract_version": "checker-runtime@1",
        "dependency_manifest_hash": "2" * 64,
        "artifact_manifest": artifact_manifest,
    }
    return {
        "schema_version": "checker-registration@1",
        **package_identity,
        "implementation_hash": canonical_sha256(package_identity),
        "params_schema": "required-fields-params@1",
        "supported_document_schemas": ["document-snapshot@1"],
        "supported_profiles": [PROFILE_KEY],
        "observation_schema": "required-fields-observation@1",
    }


def checker_manifest_payload(*, checker_version: str = CHECKER_VERSION) -> dict:
    registration = checker_registration_payload(checker_version=checker_version)
    return {
        registration["checker_key"]: {
            "checker_version": registration["checker_version"],
            "implementation_hash": registration["implementation_hash"],
            "params_schema": registration["params_schema"],
            "supported_document_schemas": deepcopy(
                registration["supported_document_schemas"]
            ),
            "supported_profiles": deepcopy(registration["supported_profiles"]),
            "observation_schema": registration["observation_schema"],
        }
    }


def plan_hash_projection(value: dict) -> dict:
    return {
        "scheme": value["hash_scheme"],
        "schema_version": value["schema_version"],
        "rubric_source_kind": value["rubric_source_kind"],
        "rubric_version_id": value["rubric_version_id"],
        "rubric_version_hash": value["rubric_version_hash"],
        "rubric_hash_scheme": value["rubric_hash_scheme"],
        "rubric_snapshot_hash": value["rubric_snapshot_hash"],
        "policy_hash": value["policy_hash"],
        "policy_compiler_version": value["policy_compiler_version"],
        "business_profile_key": value["business_profile_key"],
        "business_profile_version": value["business_profile_version"],
        "checker_manifest": value["checker_manifest"],
        "engine_contract_version": value["engine_contract_version"],
        "nodes": value["nodes"],
        "dependency_order": value["dependency_order"],
    }


def expected_plan_payload() -> dict:
    rubric = published_rubric_payload()
    criterion_by_code = {
        item["criterion_code"]: item for item in rubric["criteria"]
    }
    rule_by_code = {item["rule_code"]: item for item in rubric["atomic_rules"]}
    dependency_order = sorted(rule_by_code)
    nodes = []
    for rule_code in dependency_order:
        rule_snapshot = deepcopy(rule_by_code[rule_code])
        if rule_snapshot["judge_type"] == "deterministic":
            assert rule_snapshot["checker_key"] == CHECKER_KEY
            assert rule_snapshot["checker_version"] is None
            rule_snapshot["checker_version"] = CHECKER_VERSION
        nodes.append(
            {
                "node_kind": "atomic_rule",
                "criterion_code": rule_by_code[rule_code]["criterion_code"],
                "rule_code": rule_code,
                "criterion_snapshot": deepcopy(
                    criterion_by_code[rule_by_code[rule_code]["criterion_code"]]
                ),
                "atomic_rule_snapshot": rule_snapshot,
            }
        )
    value = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "hash_scheme": PLAN_HASH_SCHEME,
        "rubric_source_kind": rubric["rubric_source_kind"],
        "rubric_version_id": rubric["rubric_version_id"],
        "rubric_version_hash": rubric["version_hash"],
        "rubric_hash_scheme": rubric["hash_scheme"],
        "business_profile_key": PROFILE_KEY,
        "business_profile_version": PROFILE_VERSION,
        "rubric_snapshot_hash": rubric["rubric_snapshot_hash"],
        "policy_snapshot": deepcopy(rubric["global_policy"]),
        "policy_hash": rubric["global_policy"]["policy_hash"],
        "policy_compiler_version": POLICY_COMPILER_VERSION,
        "nodes": nodes,
        "dependency_order": dependency_order,
        "checker_manifest": checker_manifest_payload(),
        "engine_contract_version": ENGINE_CONTRACT_VERSION,
    }
    value["plan_hash"] = canonical_sha256(plan_hash_projection(value))
    return value


def idempotency_projection(value: dict) -> dict:
    submission = value["submission"]
    document = value["document"]
    plan = value["plan"]
    runtime = value["runtime_identity"]
    return {
        "scheme": IDEMPOTENCY_SCHEME,
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
        "runtime_identity": runtime,
        "rescore_generation": value["rescore_generation"],
    }


def scoring_request_payload(*, rescore_generation: int = 0) -> dict:
    value = {
        "schema_version": "scoring-request@2",
        "submission": technical_submission_payload(),
        "document": technical_document_payload(),
        "plan": expected_plan_payload(),
        "runtime_identity": runtime_identity_payload(),
        "rescore_generation": rescore_generation,
    }
    value["idempotency_key"] = canonical_sha256(idempotency_projection(value))
    return value


def deterministic_missing_owner_observation() -> dict:
    return {
        "checker_key": CHECKER_KEY,
        "checker_version": CHECKER_VERSION,
        "observation_code": "REQUIRED_FIELD_MISSING",
        "measured_value": False,
        "expected_value": True,
        "locator": {
            "kind": "section",
            "section_path": ["总体方案", "风险控制"],
            "section_ordinal": 1,
        },
    }


def semantic_high_band_response() -> dict:
    document = technical_document_payload()
    evidence = document["evidence_units"][0]
    return {
        "schema_version": "semantic-rule-response@1",
        "rule_code": SEMANTIC_RULE_CODE,
        "status": "triggered",
        "level_code": "FIT_HIGH",
        "evidence": [
            {
                "type": "source_quote",
                "evidence_unit_id": evidence["evidence_unit_id"],
                "quote": evidence["normalized_text"],
                "location": "需求理解",
            }
        ],
    }


__all__ = [
    "CHECKER_KEY",
    "CHECKER_VERSION",
    "DETERMINISTIC_RULE_CODE",
    "ENGINE_CONTRACT_VERSION",
    "IDEMPOTENCY_SCHEME",
    "PLAN_HASH_SCHEME",
    "PLAN_SCHEMA_VERSION",
    "POLICY_COMPILER_VERSION",
    "SEMANTIC_RULE_CODE",
    "checker_manifest_payload",
    "checker_registration_payload",
    "compiled_rubric_snapshot_projection",
    "deterministic_missing_owner_observation",
    "deterministic_rule_payload",
    "expected_plan_payload",
    "idempotency_projection",
    "plan_hash_projection",
    "published_rubric_payload",
    "published_version_content_payload",
    "scoring_request_payload",
    "semantic_high_band_response",
    "semantic_rule_payload",
]
