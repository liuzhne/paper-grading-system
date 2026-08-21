"""Deterministic compiler from a published rubric snapshot to plan@2."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.contracts import (
    CompiledRubricSnapshot,
    RuleExecutionPlan,
)


PLAN_SCHEMA_VERSION = "rule-execution-plan@2"
PLAN_HASH_SCHEME = "rule-execution-plan-v1"


def _mapping(value, *, label: str) -> dict:
    def thaw(item):
        if isinstance(item, Mapping):
            return {str(key): thaw(nested) for key, nested in item.items()}
        if isinstance(item, (list, tuple)):
            return [thaw(nested) for nested in item]
        return deepcopy(item)

    if isinstance(value, Mapping):
        return thaw(value)
    method = getattr(value, "to_mapping", None)
    if not callable(method):
        raise TypeError(f"{label} must be a mapping or expose to_mapping()")
    mapped = method()
    if not isinstance(mapped, Mapping):
        raise TypeError(f"{label}.to_mapping() must return a mapping")
    return thaw(mapped)


def _rubric_snapshot_projection(value: Mapping[str, object]) -> dict[str, object]:
    content = deepcopy(dict(value))
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


def _plan_hash_projection(value: Mapping[str, object]) -> dict[str, object]:
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


def _dependency_order(rules: Mapping[str, Mapping[str, object]]) -> list[str]:
    dependencies: dict[str, set[str]] = {}
    for code, rule in rules.items():
        current = set(rule["depends_on_rule_codes"])
        unknown = current - set(rules)
        if unknown:
            raise ValueError(
                f"atomic rule {code} has unknown dependencies: {sorted(unknown)}"
            )
        if code in current:
            raise ValueError(f"atomic rule {code} cannot depend on itself")
        dependencies[code] = current

    ordered: list[str] = []
    remaining = {code: set(items) for code, items in dependencies.items()}
    while remaining:
        ready = sorted(code for code, items in remaining.items() if not items)
        if not ready:
            raise ValueError("atomic rule dependency graph contains a cycle")
        for code in ready:
            ordered.append(code)
            remaining.pop(code)
        for items in remaining.values():
            items.difference_update(ready)
    return ordered


class RuleExecutionPlanBuilder:
    """Compile only fully identified, registry-resolvable published rubrics."""

    def __init__(
        self,
        *,
        checker_registry,
        policy_compiler_version: str,
        engine_contract_version: str,
    ) -> None:
        if checker_registry is None:
            raise TypeError("checker_registry is required")
        if not isinstance(policy_compiler_version, str) or not policy_compiler_version:
            raise ValueError("policy_compiler_version is required")
        if not isinstance(engine_contract_version, str) or not engine_contract_version:
            raise ValueError("engine_contract_version is required")
        self._registry = checker_registry
        self._policy_compiler_version = policy_compiler_version
        self._engine_contract_version = engine_contract_version

    def build(self, *, rubric, profile, document_schema_version: str):
        raw = _mapping(rubric, label="rubric")
        snapshot = CompiledRubricSnapshot.from_mapping(raw).to_mapping()
        if snapshot["rubric_source_kind"] != "published_version":
            raise ValueError("execution plans require a published rubric source")
        if snapshot["hash_scheme"] not in {"rubric-content-v1", "rubric-content-v2"}:
            raise ValueError("unsupported rubric hash scheme")

        expected_snapshot_hash = canonical_sha256(_rubric_snapshot_projection(snapshot))
        if snapshot["rubric_snapshot_hash"] != expected_snapshot_hash:
            raise ValueError("rubric snapshot hash does not match executable content")

        profile_key = getattr(profile, "profile_key", None)
        profile_version = getattr(profile, "profile_version", None)
        if not isinstance(profile_key, str) or not profile_key:
            raise ValueError("business profile key is required")
        if not isinstance(profile_version, str) or not profile_version:
            raise ValueError("business profile version is required")
        if profile_key != snapshot["business_profile_key"]:
            raise ValueError("business profile does not match published rubric")
        if not isinstance(document_schema_version, str) or not document_schema_version:
            raise ValueError("document schema version is required")

        criteria = {
            item["criterion_code"]: deepcopy(item)
            for item in snapshot["criteria"]
        }
        if len(criteria) != len(snapshot["criteria"]):
            raise ValueError("rubric contains duplicate criterion codes")
        rules = {item["rule_code"]: deepcopy(item) for item in snapshot["atomic_rules"]}
        if len(rules) != len(snapshot["atomic_rules"]):
            raise ValueError("rubric contains duplicate atomic rule codes")
        for rule in rules.values():
            if rule["criterion_code"] not in criteria:
                raise ValueError("atomic rule references an unknown criterion")
            if (
                rule["schema_version"] == "atomic-rule-snapshot@2"
                and rule["judge_type"] == "semantic"
                and not rule["evidence_policy"].get("allowed_finding_codes")
            ):
                # M4 plans must carry a closed semantic finding enum.  The
                # rule code is the stable fallback for provenance graphs that
                # predate a dedicated import column.
                rule["evidence_policy"]["allowed_finding_codes"] = [
                    rule["rule_code"]
                ]
            rule["levels"] = sorted(
                rule["levels"],
                key=lambda item: (item["display_order"], item["level_code"]),
            )

        available_manifest = _mapping(
            self._registry.manifest(
                profile_key=profile_key,
                document_schema_version=document_schema_version,
            ),
            label="checker manifest",
        )
        used_manifest: dict[str, object] = {}
        for code in sorted(rules):
            rule = rules[code]
            checker_key = rule["checker_key"]
            if rule["judge_type"] == "deterministic":
                if checker_key not in available_manifest:
                    if not available_manifest:
                        raise ValueError(
                            "document schema or profile is unsupported by checker registry"
                        )
                    raise KeyError(f"unknown or unsupported checker: {checker_key}")
                entry = deepcopy(available_manifest[checker_key])
                checker_version = entry["checker_version"]
                self._registry.resolve(
                    checker_key=checker_key,
                    checker_version=checker_version,
                    checker_params=deepcopy(rule["checker_params"]),
                    profile_key=profile_key,
                    document_schema_version=document_schema_version,
                )
                rule["checker_version"] = checker_version
                used_manifest[checker_key] = entry
            elif checker_key is not None or rule["checker_version"] is not None:
                raise ValueError("semantic rule must not resolve a deterministic checker")

        order = _dependency_order(rules)
        nodes = [
            {
                "node_kind": "atomic_rule",
                "criterion_code": rules[rule_code]["criterion_code"],
                "rule_code": rule_code,
                "criterion_snapshot": deepcopy(
                    criteria[rules[rule_code]["criterion_code"]]
                ),
                "atomic_rule_snapshot": deepcopy(rules[rule_code]),
            }
            for rule_code in order
        ]
        plan: dict[str, object] = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "hash_scheme": PLAN_HASH_SCHEME,
            "rubric_source_kind": snapshot["rubric_source_kind"],
            "rubric_version_id": snapshot["rubric_version_id"],
            "rubric_version_hash": snapshot["version_hash"],
            "rubric_hash_scheme": snapshot["hash_scheme"],
            "business_profile_key": profile_key,
            "business_profile_version": profile_version,
            "rubric_snapshot_hash": snapshot["rubric_snapshot_hash"],
            "policy_snapshot": deepcopy(snapshot["global_policy"]),
            "policy_hash": snapshot["global_policy"]["policy_hash"],
            "policy_compiler_version": self._policy_compiler_version,
            "nodes": nodes,
            "dependency_order": order,
            "checker_manifest": {
                key: used_manifest[key] for key in sorted(used_manifest)
            },
            "engine_contract_version": self._engine_contract_version,
        }
        plan["plan_hash"] = canonical_sha256(_plan_hash_projection(plan))
        return RuleExecutionPlan.from_mapping(plan)


__all__ = ["RuleExecutionPlanBuilder"]
