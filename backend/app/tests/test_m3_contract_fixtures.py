"""Self-tests for the shared M3 executable-contract fixtures.

These tests deliberately recompute every content identity instead of treating
the value emitted by a fixture factory as its own oracle.  They stay green
before the M3 production slice exists and make later strict-xfail tests safe to
share one internally consistent rubric, plan and request graph.
"""

from __future__ import annotations

from copy import deepcopy
import re

import pytest

from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.tests.m2_contract_fixtures import PROFILE_KEY, PROFILE_VERSION
from backend.app.tests.m3_contract_fixtures import (
    CHECKER_KEY,
    CHECKER_VERSION,
    DETERMINISTIC_RULE_CODE,
    ENGINE_CONTRACT_VERSION,
    IDEMPOTENCY_SCHEME,
    PLAN_HASH_SCHEME,
    PLAN_SCHEMA_VERSION,
    POLICY_COMPILER_VERSION,
    SEMANTIC_RULE_CODE,
    checker_manifest_payload,
    checker_registration_payload,
    compiled_rubric_snapshot_projection,
    expected_plan_payload,
    idempotency_projection,
    plan_hash_projection,
    published_rubric_payload,
    published_version_content_payload,
    scoring_request_payload,
)


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def test_m3_rubric_fixture_is_one_minimal_deterministic_and_semantic_slice():
    rubric = published_rubric_payload()
    criteria = {item["criterion_code"]: item for item in rubric["criteria"]}
    rules = {item["rule_code"]: item for item in rubric["atomic_rules"]}

    assert rubric["rubric_source_kind"] == "published_version"
    assert rubric["hash_scheme"] == "rubric-content-v2"
    assert rubric["business_profile_key"] == PROFILE_KEY
    assert rubric["version_hash"] == canonical_sha256(
        published_version_content_payload()
    )
    assert sum(int(item["max_score"]) for item in criteria.values()) == 100
    assert set(rules) == {DETERMINISTIC_RULE_CODE, SEMANTIC_RULE_CODE}

    deterministic = rules[DETERMINISTIC_RULE_CODE]
    assert deterministic["judge_type"] == "deterministic"
    assert deterministic["direction"] == "deduct"
    assert deterministic["repeat_policy"] == "once"
    assert deterministic["checker_key"] == CHECKER_KEY
    assert deterministic["checker_version"] is None

    semantic = rules[SEMANTIC_RULE_CODE]
    assert semantic["judge_type"] == "semantic"
    assert semantic["direction"] == "band"
    assert semantic["checker_key"] is None
    assert {level["level_code"] for level in semantic["levels"]} == {
        "FIT_HIGH",
        "FIT_LOW",
    }


def test_m3_rubric_snapshot_hash_binds_the_complete_detached_snapshot():
    rubric = published_rubric_payload()
    supplied = rubric["rubric_snapshot_hash"]

    assert supplied == canonical_sha256(compiled_rubric_snapshot_projection(rubric))
    assert SHA256_RE.fullmatch(supplied)


def test_m3_published_version_hash_uses_the_full_provenance_graph_not_snapshot():
    rubric = published_rubric_payload()
    version_content = published_version_content_payload()

    assert set(version_content) == {
        "rubric",
        "criteria",
        "compilation",
        "artifacts",
        "version",
        "rules",
    }
    assert version_content["compilation"]["parser_version"]
    assert version_content["artifacts"]
    assert version_content["rules"]
    assert set(version_content["rubric"]) == {
        "total_score",
        "description",
        "format_spec",
    }
    assert set(version_content["criteria"][0]) == {
        "code",
        "name",
        "max_score",
        "weight",
        "description",
        "evidence_hints",
        "deduction_rules",
        "display_order",
        "criterion_type",
        "scoring_mode",
        "applies_to",
        "rubric_levels",
        "sub_checks",
        "dimension",
        "deduction_rules_structured",
    }
    assert set(version_content["compilation"]) == {
        "parser_version",
        "compiler_version",
        "model_provider",
        "model_name",
        "sampling_params",
        "prompt_version",
        "raw_parse_output",
        "raw_model_output",
        "validation_result",
        "blockers",
        "warnings",
    }
    artifacts = {
        item["artifact"]["artifact_type"]: item
        for item in version_content["artifacts"]
    }
    assert set(artifacts) == {"excel", "docx"}
    assert set(artifacts["excel"]["artifact"]) == {
        "artifact_type",
        "file_name",
        "file_hash",
        "file_size_bytes",
    }
    assert set(artifacts["excel"]["source_rules"][0]) == {
        "source_rule_code",
        "sheet_name",
        "row_number",
        "cell_locator",
        "raw_text",
    }
    assert set(artifacts["docx"]["templates"][0]) == {
        "item_code",
        "kind",
        "section_path",
        "raw_text",
        "normalized_constraint",
        "strictness",
        "source_locator",
        "source_hash",
        "parse_confidence",
    }
    assert set(version_content["version"]) == {
        "workflow_profile",
        "global_policy",
        "business_profile_key",
    }
    assert set(version_content["rules"][0]) == {
        "criterion_code",
        "rule",
        "levels",
        "source_rules",
        "template_links",
    }
    assert set(version_content["rules"][0]["rule"]) == {
        "rule_code",
        "name",
        "rule_text",
        "direction",
        "effect_type",
        "max_points",
        "repeat_policy",
        "cap_points",
        "judge_type",
        "checker_key",
        "checker_params",
        "evidence_policy",
        "positive_example",
        "negative_example",
        "boundary_example",
        "strictness",
        "applies_to",
        "mutex_group",
        "depends_on_rule_codes",
        "creation_method",
    }
    deterministic_rule = next(
        item
        for item in version_content["rules"]
        if item["rule"]["rule_code"] == DETERMINISTIC_RULE_CODE
    )
    assert deterministic_rule["source_rules"] == artifacts["excel"]["source_rules"]
    assert set(deterministic_rule["template_links"][0]) == {"link", "template"}
    assert set(deterministic_rule["template_links"][0]["link"]) == {
        "relationship_type",
        "match_method",
        "match_confidence",
        "rationale",
    }
    assert deterministic_rule["template_links"][0]["template"] == artifacts[
        "docx"
    ]["templates"][0]
    assert rubric["version_hash"] == canonical_sha256(version_content)
    assert rubric["version_hash"] != rubric["rubric_snapshot_hash"]


@pytest.mark.parametrize(
    "branch",
    (
        "rubric",
        "criterion",
        "compilation",
        "artifact",
        "source-rule",
        "template",
        "version",
        "atomic-rule",
        "level",
        "rule-source-link",
        "rule-template-link",
    ),
)
def test_m3_published_version_hash_binds_every_0011_provenance_branch(branch):
    original = published_version_content_payload()
    changed = deepcopy(original)
    deterministic_rule = next(
        item
        for item in changed["rules"]
        if item["rule"]["rule_code"] == DETERMINISTIC_RULE_CODE
    )
    semantic_rule = next(
        item
        for item in changed["rules"]
        if item["rule"]["rule_code"] == SEMANTIC_RULE_CODE
    )

    if branch == "rubric":
        changed["rubric"]["description"] += " Changed."
    elif branch == "criterion":
        changed["criteria"][0]["name"] += " changed"
    elif branch == "compilation":
        changed["compilation"]["parser_version"] += "+changed"
    elif branch == "artifact":
        changed["artifacts"][0]["artifact"]["file_hash"] = "0" * 64
    elif branch == "source-rule":
        changed["artifacts"][0]["source_rules"][0]["raw_text"] += " Changed."
    elif branch == "template":
        changed["artifacts"][1]["templates"][0]["source_hash"] = "0" * 64
    elif branch == "version":
        changed["version"]["workflow_profile"] = "excel_only"
    elif branch == "atomic-rule":
        deterministic_rule["rule"]["checker_params"] = {
            "required_fields": ["owner", "mitigation"]
        }
    elif branch == "level":
        semantic_rule["levels"][0]["descriptor"] += " Changed."
    elif branch == "rule-source-link":
        deterministic_rule["source_rules"][0]["cell_locator"] = (
            "Atomic Rules!A3:N3"
        )
    else:
        deterministic_rule["template_links"][0]["link"]["rationale"] += (
            " Changed."
        )

    assert canonical_sha256(changed) != canonical_sha256(original)


def test_m3_version_content_v1_is_byte_compatible_and_v2_binds_profile():
    v1 = published_version_content_payload(
        hash_scheme="rubric-content-v1",
        profile_key="thesis",
    )
    v2_thesis = published_version_content_payload(
        hash_scheme="rubric-content-v2",
        profile_key="thesis",
    )
    v2_technical = published_version_content_payload(
        hash_scheme="rubric-content-v2",
        profile_key=PROFILE_KEY,
    )

    assert "business_profile_key" not in v1["version"]
    assert v2_thesis["version"]["business_profile_key"] == "thesis"
    assert v2_technical["version"]["business_profile_key"] == PROFILE_KEY
    assert canonical_sha256(v2_thesis) != canonical_sha256(v2_technical)


def test_m3_checker_registration_has_reproducible_package_identity():
    registration = checker_registration_payload()
    package_identity = {
        key: deepcopy(registration[key])
        for key in (
            "checker_key",
            "checker_version",
            "entrypoint",
            "runtime_contract_version",
            "dependency_manifest_hash",
            "artifact_manifest",
        )
    }
    package_identity["scheme"] = "checker-package-sha256-v1"

    assert registration["implementation_hash"] == canonical_sha256(package_identity)
    paths = [item["path"] for item in registration["artifact_manifest"]]
    assert paths == sorted(paths)
    assert len(paths) == len(set(paths))
    assert all(path and not path.startswith("/") and "\\" not in path for path in paths)
    assert registration["supported_profiles"] == [PROFILE_KEY]
    assert registration["supported_document_schemas"] == ["document-snapshot@1"]


def test_m3_plan_fixture_is_stably_sorted_and_contains_executable_snapshots():
    rubric = published_rubric_payload()
    plan = expected_plan_payload()
    rule_by_code = {item["rule_code"]: item for item in rubric["atomic_rules"]}
    criterion_by_code = {
        item["criterion_code"]: item for item in rubric["criteria"]
    }

    assert plan["schema_version"] == PLAN_SCHEMA_VERSION
    assert plan["hash_scheme"] == PLAN_HASH_SCHEME
    assert plan["rubric_source_kind"] == rubric["rubric_source_kind"]
    assert plan["rubric_version_id"] == rubric["rubric_version_id"]
    assert plan["rubric_version_hash"] == rubric["version_hash"]
    assert plan["rubric_hash_scheme"] == rubric["hash_scheme"]
    assert plan["dependency_order"] == sorted(rule_by_code)
    assert [node["rule_code"] for node in plan["nodes"]] == plan["dependency_order"]
    for node in plan["nodes"]:
        rule = rule_by_code[node["rule_code"]]
        expected_rule = deepcopy(rule)
        if expected_rule["judge_type"] == "deterministic":
            assert expected_rule["checker_version"] is None
            expected_rule["checker_version"] = CHECKER_VERSION
        assert node["atomic_rule_snapshot"] == expected_rule
        assert node["criterion_snapshot"] == criterion_by_code[rule["criterion_code"]]

    deterministic_node = next(
        node
        for node in plan["nodes"]
        if node["rule_code"] == DETERMINISTIC_RULE_CODE
    )
    assert deterministic_node["atomic_rule_snapshot"]["checker_version"] == (
        CHECKER_VERSION
    )

    assert plan["policy_snapshot"]["policy_hash"] == plan["policy_hash"]
    assert plan["policy_compiler_version"] == POLICY_COMPILER_VERSION
    assert plan["engine_contract_version"] == ENGINE_CONTRACT_VERSION
    assert plan["checker_manifest"] == checker_manifest_payload()
    assert len(
        {
            rubric["version_hash"],
            rubric["rubric_snapshot_hash"],
            plan["plan_hash"],
        }
    ) == 3


def test_m3_plan_hash_is_recomputed_from_the_declared_projection():
    plan = expected_plan_payload()
    projection = plan_hash_projection(plan)

    assert projection["scheme"] == plan["hash_scheme"]
    assert set(projection) == (
        set(plan) - {"plan_hash", "policy_snapshot", "hash_scheme"}
    ) | {"scheme"}
    assert plan["plan_hash"] == canonical_sha256(projection)
    assert SHA256_RE.fullmatch(plan["plan_hash"])

    changed = deepcopy(plan)
    changed["engine_contract_version"] = "scoring-core@2"
    assert canonical_sha256(plan_hash_projection(changed)) != plan["plan_hash"]


@pytest.mark.parametrize(
    "mutation",
    (
        "plan-hash-scheme",
        "rubric",
        "rubric-version-id",
        "rubric-version-hash",
        "rubric-hash-scheme",
        "policy",
        "policy-compiler",
        "profile-key",
        "profile-version",
        "checker",
        "engine",
        "node",
        "dependency-order",
    ),
)
def test_m3_plan_hash_changes_when_any_executable_identity_changes(mutation):
    original = expected_plan_payload()
    changed = deepcopy(original)
    if mutation == "plan-hash-scheme":
        changed["hash_scheme"] = "rule-execution-plan-v2"
    elif mutation == "rubric":
        changed["rubric_snapshot_hash"] = "0" * 64
    elif mutation == "rubric-version-id":
        changed["rubric_version_id"] += "-changed"
    elif mutation == "rubric-version-hash":
        changed["rubric_version_hash"] = "6" * 64
    elif mutation == "rubric-hash-scheme":
        changed["rubric_hash_scheme"] = "rubric-content-v3"
    elif mutation == "policy":
        changed["policy_hash"] = "1" * 64
    elif mutation == "policy-compiler":
        changed["policy_compiler_version"] += "+changed"
    elif mutation == "profile-key":
        changed["business_profile_key"] = "another_profile"
    elif mutation == "profile-version":
        changed["business_profile_version"] += "+changed"
    elif mutation == "checker":
        changed["checker_manifest"][CHECKER_KEY]["implementation_hash"] = "2" * 64
    elif mutation == "engine":
        changed["engine_contract_version"] += "+changed"
    elif mutation == "node":
        changed["nodes"][0]["atomic_rule_snapshot"]["checker_params"] = {
            "required_fields": ["owner", "mitigation"]
        }
    else:
        changed["dependency_order"].reverse()

    assert canonical_sha256(plan_hash_projection(changed)) != original["plan_hash"]


def test_m3_request_idempotency_binds_every_replay_identity_and_generation():
    request = scoring_request_payload()
    projection = idempotency_projection(request)

    assert projection["scheme"] == IDEMPOTENCY_SCHEME
    assert request["idempotency_key"] == canonical_sha256(projection)
    assert SHA256_RE.fullmatch(request["idempotency_key"])
    assert set(projection) == {
        "scheme",
        "submission_snapshot_hash",
        "source_artifact_hash",
        "document_snapshot_hash",
        "normalized_content_hash",
        "rubric_source_kind",
        "rubric_version_id",
        "rubric_version_hash",
        "rubric_hash_scheme",
        "rubric_snapshot_hash",
        "execution_plan_hash",
        "policy_hash",
        "business_profile_key",
        "business_profile_version",
        "runtime_identity",
        "rescore_generation",
    }

    rescore = scoring_request_payload(rescore_generation=1)
    assert rescore["idempotency_key"] != request["idempotency_key"]


@pytest.mark.parametrize(
    "mutation",
    (
        "submission",
        "source-artifact",
        "document",
        "normalized-content",
        "rubric",
        "rubric-version-id",
        "rubric-version-hash",
        "rubric-hash-scheme",
        "plan",
        "policy",
        "profile-key",
        "profile-version",
        "runtime",
        "generation",
    ),
)
def test_m3_idempotency_key_changes_for_every_authoritative_input(mutation):
    original = scoring_request_payload()
    changed = deepcopy(original)
    if mutation == "submission":
        changed["submission"]["submission_id"] += "-changed"
    elif mutation == "source-artifact":
        changed["submission"]["source_artifact_hash"] = "0" * 64
    elif mutation == "document":
        changed["document"]["document_snapshot_hash"] = "1" * 64
    elif mutation == "normalized-content":
        changed["document"]["content_hash"] = "2" * 64
    elif mutation == "rubric":
        changed["plan"]["rubric_snapshot_hash"] = "3" * 64
    elif mutation == "rubric-version-id":
        changed["plan"]["rubric_version_id"] += "-changed"
    elif mutation == "rubric-version-hash":
        changed["plan"]["rubric_version_hash"] = "6" * 64
    elif mutation == "rubric-hash-scheme":
        changed["plan"]["rubric_hash_scheme"] = "rubric-content-v3"
    elif mutation == "plan":
        changed["plan"]["plan_hash"] = "4" * 64
    elif mutation == "policy":
        changed["plan"]["policy_hash"] = "5" * 64
    elif mutation == "profile-key":
        changed["submission"]["profile_key"] = "another_profile"
    elif mutation == "profile-version":
        changed["document"]["profile_version"] += "+changed"
    elif mutation == "runtime":
        changed["runtime_identity"]["provider"]["model_version"] += "+changed"
    else:
        changed["rescore_generation"] += 1

    changed_key = canonical_sha256(idempotency_projection(changed))
    assert changed_key != original["idempotency_key"]


def test_m3_fixture_graph_uses_one_profile_and_content_addressed_document():
    request = scoring_request_payload()
    submission = request["submission"]
    document = request["document"]
    plan = request["plan"]
    runtime = request["runtime_identity"]

    assert submission["profile_key"] == PROFILE_KEY
    assert document["profile_key"] == PROFILE_KEY
    assert document["profile_version"] == PROFILE_VERSION
    assert plan["business_profile_key"] == PROFILE_KEY
    assert plan["business_profile_version"] == PROFILE_VERSION
    assert runtime["profile_key"] == PROFILE_KEY
    assert runtime["profile_version"] == PROFILE_VERSION

    for value in (
        submission["source_artifact_hash"],
        document["content_hash"],
        document["document_snapshot_hash"],
        plan["rubric_snapshot_hash"],
        plan["policy_hash"],
        plan["plan_hash"],
        runtime["provider"]["artifact_hash"],
        request["idempotency_key"],
    ):
        assert SHA256_RE.fullmatch(value)


def test_m3_plan_and_rubric_snapshots_do_not_leak_orm_or_local_storage_fields():
    forbidden = {
        "database_id",
        "file_path",
        "local_path",
        "orm_id",
        "paper_id",
        "parsed_paper_id",
        "session",
        "storage_path",
    }

    def all_keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from all_keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from all_keys(item)

    keys = set(all_keys(published_rubric_payload())) | set(
        all_keys(expected_plan_payload())
    )
    assert not (forbidden & keys)
