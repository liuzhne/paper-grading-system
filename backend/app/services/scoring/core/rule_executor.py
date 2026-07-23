"""Pure M4 AtomicRule execution state machine.

Checkers and semantic providers report observations/decisions only.  This
module is the sole authority that converts published AtomicRule/RuleLevel
values into score effects and preserves a replayable occurrence audit.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal

from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.contracts import (
    DeterministicCheckerResultV1,
    PromptEnvelopeV3,
    ScoringRequest,
    SemanticRuleResponseV2,
)
from backend.app.services.scoring.core.results import RuleExecutionResult


PROMPT_VERSION = "2026-07-20-7"
OCCURRENCE_SCHEME = "occurrence-id-v1"


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    method = getattr(value, "to_mapping", None)
    if callable(method):
        return _plain(method())
    return deepcopy(value)


def _decimal_text(value: Decimal | str | int) -> str:
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    if decimal.is_zero():
        return "0"
    return format(decimal.normalize(), "f")


def _issue(code, severity, criterion_code, rule_code, message):
    return {
        "code": code,
        "severity": severity,
        "criterion_code": criterion_code,
        "rule_code": rule_code,
        "message": message,
    }


def _profile_identity(profile) -> tuple[str, str]:
    key = getattr(profile, "profile_key", None)
    version = getattr(profile, "profile_version", None)
    if not isinstance(key, str) or not key.strip():
        raise TypeError("profile.profile_key must be a non-empty string")
    if not isinstance(version, str) or not version.strip():
        raise TypeError("profile.profile_version must be a non-empty string")
    return key, version


def _validate_profile(profile, request):
    key, version = _profile_identity(profile)
    if {
        request["submission"]["profile_key"],
        request["document"]["profile_key"],
        request["plan"]["business_profile_key"],
        request["runtime_identity"]["profile_key"],
        key,
    } != {key}:
        raise ValueError("profile identity does not match scoring request")
    if {
        request["document"]["profile_version"],
        request["plan"]["business_profile_version"],
        request["runtime_identity"]["profile_version"],
        version,
    } != {version}:
        raise ValueError("profile version does not match scoring request")


def _prompt_envelope(*, request, node, profile):
    build_extensions = getattr(profile, "build_prompt_extensions", None)
    if not callable(build_extensions):
        raise TypeError("profile must implement build_prompt_extensions")
    submission = request["submission"]
    document = request["document"]
    plan = request["plan"]
    return PromptEnvelopeV3.from_mapping(
        {
            "schema_version": "prompt-envelope@3",
            "prompt_version": PROMPT_VERSION,
            "runtime_identity": _plain(request["runtime_identity"]),
            "rubric_identity": {
                "rubric_source_kind": plan["rubric_source_kind"],
                "rubric_version_id": plan["rubric_version_id"],
                "rubric_version_hash": plan["rubric_version_hash"],
                "rubric_hash_scheme": plan["rubric_hash_scheme"],
            },
            "rubric_snapshot_hash": plan["rubric_snapshot_hash"],
            "plan_hash": plan["plan_hash"],
            "policy_hash": plan["policy_hash"],
            "criterion_snapshot": _plain(node["criterion_snapshot"]),
            "atomic_rule_snapshot": _plain(node["atomic_rule_snapshot"]),
            "submission": {
                "source_artifact_hash": submission["source_artifact_hash"],
                "normalized_content_hash": document["content_hash"],
                "document_snapshot_hash": document["document_snapshot_hash"],
            },
            "evidence_units": _plain(document["evidence_units"]),
            "profile_prompt_extensions": _plain(
                build_extensions(
                    submission_snapshot=_plain(submission),
                    document_snapshot=_plain(document),
                )
            ),
        }
    )


def _topological_order(nodes_by_code):
    dependencies = {
        code: set(node["atomic_rule_snapshot"]["depends_on_rule_codes"])
        for code, node in nodes_by_code.items()
    }
    unknown = {
        dependency
        for values in dependencies.values()
        for dependency in values
        if dependency not in nodes_by_code
    }
    if unknown:
        raise ValueError("execution plan contains unknown dependencies")
    order = []
    remaining = set(nodes_by_code)
    while remaining:
        ready = sorted(
            code
            for code in remaining
            if not (dependencies[code] & remaining)
        )
        if not ready:
            raise ValueError("execution plan contains a dependency cycle")
        order.extend(ready)
        remaining.difference_update(ready)
    return order


def _decision_skeleton(plan, rule, *, status="invalid"):
    version_hash = plan.get("rubric_version_hash") or plan["rubric_snapshot_hash"]
    return {
        "version_hash": version_hash,
        "rule_code": rule["rule_code"],
        "criterion_code": rule["criterion_code"],
        "direction": rule["direction"],
        "effect_type": rule["effect_type"],
        "status": status,
        "selected_level_code": None,
        "evidence_refs": [],
        "occurrences": [],
        "calculated_effect": None,
    }


def _matrix_error(rule):
    direction = rule["direction"]
    effect = rule["effect_type"]
    if direction == "bonus":
        return "BONUS_NOT_ENABLED", "bonus direction is reserved in v1"
    legal = {
        ("deduct", "score"),
        ("band", "score"),
        ("none", "review"),
        ("none", "block_submission"),
        ("none", "report_only"),
    }
    if (direction, effect) not in legal:
        return (
            "RULE_EFFECT_MATRIX_INVALID",
            "AtomicRule direction/effect_type pair is not executable",
        )
    return None


def _evidence_ref(raw, unit):
    return {
        "evidence_unit_id": unit["evidence_unit_id"],
        "evidence_type": "source_quote",
        "locator": _plain(unit["locator"]),
        "payload_hash": canonical_sha256(_plain(raw)),
    }


def _deterministic_evidence_ref(observation):
    return {
        "evidence_unit_id": None,
        "evidence_type": "deterministic_observation",
        "locator": _plain(observation["locator"]),
        "payload_hash": canonical_sha256(_plain(observation)),
    }


def _occurrence_payload(*, request, rule, finding_code, evidence_unit_ids, locator):
    plan = request["plan"]
    return {
        "scheme": OCCURRENCE_SCHEME,
        "document_snapshot_hash": request["document"]["document_snapshot_hash"],
        "rubric_hash_scheme": plan["rubric_hash_scheme"],
        "rubric_version_hash": plan["rubric_version_hash"],
        "criterion_code": rule["criterion_code"],
        "rule_code": rule["rule_code"],
        "finding_code": finding_code,
        "evidence_unit_ids": sorted(set(evidence_unit_ids)),
        "locator": _plain(locator),
    }


def _register_occurrence(
    *,
    request,
    rule,
    finding_code,
    evidence_refs,
    locator,
    occurrence_payloads,
):
    payload = _occurrence_payload(
        request=request,
        rule=rule,
        finding_code=finding_code,
        evidence_unit_ids=[
            item["evidence_unit_id"]
            for item in evidence_refs
            if item["evidence_unit_id"] is not None
        ],
        locator=locator,
    )
    occurrence_id = canonical_sha256(payload)
    existing = occurrence_payloads.get(occurrence_id)
    if existing is not None and existing != payload:
        return None, "collision"
    occurrence_payloads[occurrence_id] = deepcopy(payload)
    return {
        "occurrence_id": occurrence_id,
        "occurrence_payload": payload,
        "evidence_refs": deepcopy(evidence_refs),
        "calculated_effect": None,
    }, "duplicate" if existing is not None else None


def _semantic_occurrences(*, response, request, rule, occurrence_payloads):
    raw_occurrences = response.get("occurrences")
    if not isinstance(raw_occurrences, (list, tuple)):
        return [], [], "REQUIRED_EVIDENCE_INVALID", "occurrences must be an array"
    allowed_codes = set(rule["evidence_policy"].get("allowed_finding_codes", ()))
    units = {
        item["evidence_unit_id"]: item for item in request["document"]["evidence_units"]
    }
    accepted = []
    all_refs = []
    seen = set()
    for raw_occurrence in raw_occurrences:
        if not isinstance(raw_occurrence, Mapping):
            return [], [], "REQUIRED_EVIDENCE_INVALID", "occurrence must be an object"
        finding_code = raw_occurrence.get("finding_code")
        if finding_code not in allowed_codes:
            return [], [], "FINDING_CODE_NOT_AUTHORIZED", "finding code is not published"
        raw_evidence = raw_occurrence.get("evidence")
        if not isinstance(raw_evidence, (list, tuple)) or not raw_evidence:
            return [], [], "REQUIRED_EVIDENCE_INVALID", "occurrence lacks evidence"
        refs = []
        referenced_units = []
        for raw in raw_evidence:
            if not isinstance(raw, Mapping) or raw.get("type") != "source_quote":
                return [], [], "REQUIRED_EVIDENCE_INVALID", "evidence type is invalid"
            unit = units.get(raw.get("evidence_unit_id"))
            quote = raw.get("quote")
            if (
                unit is None
                or not isinstance(quote, str)
                or not quote.strip()
                or quote.strip() not in unit["normalized_text"]
            ):
                return [], [], "REQUIRED_EVIDENCE_INVALID", "quote is not authorized"
            refs.append(_evidence_ref(raw, unit))
            referenced_units.append(unit)
        locator = raw_occurrence.get("locator")
        if not isinstance(locator, Mapping):
            return [], [], "REQUIRED_EVIDENCE_INVALID", "occurrence locator is invalid"
        # For a single quote the frozen unit locator is authoritative; this
        # also prevents provider-supplied free-form locations entering identity.
        if len(referenced_units) == 1:
            locator = referenced_units[0]["locator"]
        occurrence, duplicate = _register_occurrence(
            request=request,
            rule=rule,
            finding_code=finding_code,
            evidence_refs=refs,
            locator=locator,
            occurrence_payloads=occurrence_payloads,
        )
        if duplicate == "collision":
            return [], [], "OCCURRENCE_HASH_COLLISION", "occurrence digest collision"
        if duplicate == "duplicate":
            continue
        accepted.append(occurrence)
        for ref in refs:
            key = canonical_sha256(ref)
            if key not in seen:
                seen.add(key)
                all_refs.append(ref)
    accepted.sort(key=lambda item: item["occurrence_id"])
    return accepted, all_refs, None, None


def _adapt_legacy_semantic_response(*, response, request, rule):
    """Project the frozen M3 provider shape onto the M4 decision boundary."""

    fields = {
        "schema_version",
        "rule_code",
        "status",
        "level_code",
        "evidence",
    }
    if set(response) != fields or response.get("schema_version") != "semantic-rule-response@1":
        return None
    raw_evidence = response.get("evidence")
    if not isinstance(raw_evidence, (list, tuple)):
        return None
    occurrences = []
    if response.get("status") == "triggered":
        allowed_codes = list(
            rule["evidence_policy"].get("allowed_finding_codes", ())
        )
        if len(allowed_codes) != 1 or not raw_evidence:
            return None
        locator = {}
        first = raw_evidence[0]
        if isinstance(first, Mapping):
            unit_id = first.get("evidence_unit_id")
            unit = next(
                (
                    item
                    for item in request["document"]["evidence_units"]
                    if item["evidence_unit_id"] == unit_id
                ),
                None,
            )
            if unit is not None:
                locator = _plain(unit["locator"])
        occurrences.append(
            {
                "finding_code": allowed_codes[0],
                "evidence": _plain(raw_evidence),
                "locator": locator,
            }
        )
    return {
        "schema_version": "semantic-rule-response@2",
        "rule_code": response.get("rule_code"),
        "status": response.get("status"),
        "level_code": response.get("level_code"),
        "occurrences": occurrences,
    }


def _semantic_decision(
    *, request, node, llm_runtime, profile, occurrence_payloads
):
    rule = node["atomic_rule_snapshot"]
    decision = _decision_skeleton(request["plan"], rule)
    envelope = _prompt_envelope(request=request, node=node, profile=profile)
    response = llm_runtime.score(envelope=envelope)
    if not isinstance(response, Mapping):
        return decision, "UNKNOWN_RULE", "semantic response must be an object"
    if response.get("schema_version") == "semantic-rule-response@1":
        response = _adapt_legacy_semantic_response(
            response=response,
            request=request,
            rule=rule,
        )
        if response is None:
            return decision, "RULE_DECISION_INVALID", "legacy semantic response schema is invalid"
    try:
        response = SemanticRuleResponseV2.from_mapping(response).to_mapping()
    except (TypeError, ValueError):
        return decision, "RULE_DECISION_INVALID", "semantic response schema is invalid"
    if response.get("rule_code") != rule["rule_code"]:
        return decision, "UNKNOWN_RULE", "semantic response named an unauthorized rule"
    status = response.get("status")
    if status not in {"triggered", "not_triggered", "not_applicable"}:
        return decision, "RULE_DECISION_INVALID", "semantic decision status is invalid"
    if status != "triggered":
        decision["status"] = status
        decision["calculated_effect"] = (
            None if rule["direction"] == "none" else "0"
        )
        return decision, None, None
    if rule["direction"] == "band":
        level_code = response.get("level_code")
        levels = {item["level_code"]: item for item in rule["levels"]}
        if not isinstance(level_code, str) or level_code not in levels:
            return decision, "BAND_SELECTION_INVALID", "band selection is not published"
        decision["selected_level_code"] = level_code
    occurrences, refs, error, message = _semantic_occurrences(
        response=response,
        request=request,
        rule=rule,
        occurrence_payloads=occurrence_payloads,
    )
    if error:
        return decision, error, message
    if not occurrences and rule["evidence_policy"]["requirement"] == "required":
        return decision, "REQUIRED_EVIDENCE_INVALID", "triggered rule lacks evidence"
    decision.update(
        {
            "status": "triggered",
            "evidence_refs": refs,
            "occurrences": occurrences,
        }
    )
    return decision, None, None


def _deterministic_decision(
    *, request, node, checker_registry, occurrence_payloads
):
    rule = node["atomic_rule_snapshot"]
    decision = _decision_skeleton(request["plan"], rule)
    checker = checker_registry.resolve(
        checker_key=rule["checker_key"],
        checker_version=rule["checker_version"],
        checker_params=_plain(rule["checker_params"]),
        profile_key=request["submission"]["profile_key"],
        document_schema_version=request["document"]["schema_version"],
    )
    if not callable(checker):
        return decision, "CHECKER_OUTPUT_SCHEMA_INVALID", "resolved checker is not callable"
    result = checker(
        document=_plain(request["document"]), params=_plain(rule["checker_params"])
    )
    if not isinstance(result, Mapping):
        return decision, "CHECKER_OUTPUT_SCHEMA_INVALID", "checker result schema is invalid"
    if result.get("schema_version") != "deterministic-checker-result@1":
        legacy_fields = {
            "observation_code",
            "measured_value",
            "expected_value",
            "locator",
        }
        optional_legacy_fields = {
            "checker_key",
            "checker_version",
            "status",
            "triggered",
        }
        if not legacy_fields.issubset(result) or set(result) - (
            legacy_fields | optional_legacy_fields
        ):
            return decision, "CHECKER_OUTPUT_SCHEMA_INVALID", "checker result schema is invalid"
        if result.get("checker_key", rule["checker_key"]) != rule["checker_key"]:
            return decision, "CHECKER_OUTPUT_SCHEMA_INVALID", "checker identity mismatch"
        if result.get("checker_version", rule["checker_version"]) != rule["checker_version"]:
            return decision, "CHECKER_OUTPUT_SCHEMA_INVALID", "checker identity mismatch"
        explicit_status = result.get("status")
        if explicit_status in {"triggered", "not_triggered", "not_applicable"}:
            status = explicit_status
        elif isinstance(result.get("triggered"), bool):
            status = "triggered" if result["triggered"] else "not_triggered"
        else:
            status = (
                "triggered"
                if result["measured_value"] != result["expected_value"]
                else "not_triggered"
            )
        observation = {
            "status": status,
            "observation_code": result["observation_code"],
            "measured_value": _plain(result["measured_value"]),
            "expected_value": _plain(result["expected_value"]),
            "locator": _plain(result["locator"]),
        }
        if rule["schema_version"] == "atomic-rule-snapshot@2":
            observation["finding_code"] = result["observation_code"]
        result = {
            "schema_version": "deterministic-checker-result@1",
            "observations": [observation],
        }
    try:
        result = DeterministicCheckerResultV1.from_mapping(result).to_mapping()
    except (TypeError, ValueError):
        return decision, "CHECKER_OUTPUT_SCHEMA_INVALID", "checker result schema is invalid"
    observations = result.get("observations")
    if not isinstance(observations, (list, tuple)) or not observations:
        return decision, "CHECKER_OUTPUT_SCHEMA_INVALID", "checker observations are invalid"
    manifest_schema = request["plan"]["checker_manifest"][rule["checker_key"]][
        "observation_schema"
    ]
    resolved_schema = getattr(checker, "observation_schema", manifest_schema)
    if isinstance(manifest_schema, Mapping):
        if _plain(resolved_schema) != _plain(manifest_schema):
            return decision, "CHECKER_OUTPUT_SCHEMA_INVALID", "checker schema identity mismatch"
        allowed_observations = set(manifest_schema["allowed_observation_codes"])
        allowed_findings = set(manifest_schema["allowed_finding_codes"])
    else:
        allowed_observations = None
        allowed_findings = None
    accepted = []
    refs = []
    statuses = []
    expected_fields = {
        "status",
        "observation_code",
        "finding_code",
        "measured_value",
        "expected_value",
        "locator",
    }
    for observation in observations:
        allowed_fields = expected_fields
        if rule["schema_version"] == "atomic-rule-snapshot@1":
            allowed_fields = expected_fields - {"finding_code"}
        if not isinstance(observation, Mapping) or set(observation) != allowed_fields:
            return decision, "CHECKER_OUTPUT_SCHEMA_INVALID", "observation schema is invalid"
        status = observation.get("status")
        if status not in {"triggered", "not_triggered", "not_applicable"}:
            return decision, "CHECKER_OUTPUT_SCHEMA_INVALID", "observation status is invalid"
        if allowed_observations is not None and observation["observation_code"] not in allowed_observations:
            return decision, "CHECKER_OUTPUT_SCHEMA_INVALID", "observation code is invalid"
        finding_code = observation.get("finding_code")
        if (
            finding_code is not None
            and allowed_findings is not None
            and finding_code not in allowed_findings
        ):
            return decision, "FINDING_CODE_NOT_AUTHORIZED", "finding code is not registered"
        statuses.append(status)
        if status != "triggered":
            continue
        evidence_ref = _deterministic_evidence_ref(observation)
        occurrence, duplicate = _register_occurrence(
            request=request,
            rule=rule,
            finding_code=finding_code,
            evidence_refs=[evidence_ref],
            locator=observation["locator"],
            occurrence_payloads=occurrence_payloads,
        )
        if duplicate == "collision":
            return decision, "OCCURRENCE_HASH_COLLISION", "occurrence digest collision"
        if duplicate != "duplicate":
            accepted.append(occurrence)
            refs.append(evidence_ref)
    accepted.sort(key=lambda item: item["occurrence_id"])
    if any(status == "triggered" for status in statuses):
        status = "triggered"
    elif all(status == "not_applicable" for status in statuses):
        status = "not_applicable"
    else:
        status = "not_triggered"
    decision.update(
        {
            "status": status,
            "evidence_refs": refs,
            "occurrences": accepted,
            "calculated_effect": None if rule["direction"] == "none" else "0",
        }
    )
    return decision, None, None


def _assign_occurrence_effects(decision, amounts):
    for occurrence, amount in zip(decision["occurrences"], amounts):
        occurrence["calculated_effect"] = _decimal_text(amount)


def _deduction_effects(rule, decision, available_score):
    occurrences = sorted(decision["occurrences"], key=lambda item: item["occurrence_id"])
    decision["occurrences"] = occurrences
    points = Decimal(rule["max_points"])
    policy = rule["repeat_policy"]
    amounts = [Decimal("0") for _ in occurrences]
    if occurrences:
        if policy == "once":
            amounts[0] = -points
        elif policy == "per_occurrence":
            amounts = [-points for _ in occurrences]
        elif policy == "capped":
            remaining = Decimal(rule["cap_points"])
            for index in range(len(occurrences)):
                if remaining <= 0:
                    break
                allocated = min(points, remaining)
                amounts[index] = -allocated
                remaining -= allocated
        else:
            raise ValueError("unsupported repeat policy")
    total = -sum((-amount for amount in amounts), Decimal("0"))
    if -total > available_score:
        return None, "DEDUCTION_EXCEEDS_CRITERION_MAX"
    _assign_occurrence_effects(decision, amounts)
    decision["calculated_effect"] = _decimal_text(total)
    return amounts, None


def _criterion_results(*, order, nodes_by_code, decisions, issues):
    decision_by_code = {item["rule_code"]: item for item in decisions}
    rules_by_criterion = {}
    criterion_by_code = {}
    for code in order:
        node = nodes_by_code[code]
        criterion_code = node["criterion_code"]
        rules_by_criterion.setdefault(criterion_code, []).append(
            node["atomic_rule_snapshot"]
        )
        criterion_by_code[criterion_code] = node["criterion_snapshot"]

    invalid_criteria = {
        item["criterion_code"] for item in decisions if item["status"] == "invalid"
    }
    blocked_criteria = {
        item["criterion_code"]
        for item in decisions
        if item["status"] == "triggered" and item["effect_type"] == "block_submission"
    }
    for criterion_code, rules in rules_by_criterion.items():
        groups = {}
        for rule in rules:
            if rule["mutex_group"]:
                groups.setdefault(rule["mutex_group"], []).append(rule)
        for members in groups.values():
            triggered = [
                rule
                for rule in members
                if decision_by_code[rule["rule_code"]]["status"] == "triggered"
            ]
            if len(triggered) > 1:
                invalid_criteria.add(criterion_code)
                issues.append(
                    _issue(
                        "MUTEX_CONFLICT",
                        "block",
                        criterion_code,
                        triggered[0]["rule_code"],
                        "multiple mutually exclusive rules triggered",
                    )
                )

    outcomes = []
    contributions = []
    for criterion_code in sorted(criterion_by_code):
        criterion = criterion_by_code[criterion_code]
        rules = rules_by_criterion[criterion_code]
        max_score = Decimal(criterion["max_score"])
        if criterion_code in invalid_criteria:
            outcomes.append(
                {
                    "criterion_code": criterion_code,
                    "status": "invalid",
                    "auto_score": None,
                    "final_score": None,
                    "max_score": criterion["max_score"],
                }
            )
            continue
        if criterion_code in blocked_criteria:
            outcomes.append(
                {
                    "criterion_code": criterion_code,
                    "status": "blocked",
                    "auto_score": None,
                    "final_score": None,
                    "max_score": criterion["max_score"],
                }
            )
            continue

        mode = criterion["assessment_mode"]
        review_triggered = any(
            rule["effect_type"] == "review"
            and decision_by_code[rule["rule_code"]]["status"] == "triggered"
            for rule in rules
        )
        if mode == "review_only":
            outcomes.append(
                {
                    "criterion_code": criterion_code,
                    "status": "review_required",
                    "auto_score": None,
                    "final_score": None,
                    "max_score": criterion["max_score"],
                }
            )
            continue

        criterion_contributions = []
        if mode == "band":
            band_rules = [rule for rule in rules if rule["direction"] == "band"]
            if len(band_rules) != 1:
                raise ValueError("band criterion requires exactly one band rule")
            rule = band_rules[0]
            decision = decision_by_code[rule["rule_code"]]
            if decision["status"] != "triggered" or decision["selected_level_code"] is None:
                outcomes.append(
                    {
                        "criterion_code": criterion_code,
                        "status": "invalid",
                        "auto_score": None,
                        "final_score": None,
                        "max_score": criterion["max_score"],
                    }
                )
                continue
            level = next(
                item
                for item in rule["levels"]
                if item["level_code"] == decision["selected_level_code"]
            )
            score = Decimal(level["points"])
            decision["calculated_effect"] = _decimal_text(score)
            occurrence_amounts = [Decimal("0") for _ in decision["occurrences"]]
            if occurrence_amounts:
                occurrence_amounts[0] = score
            _assign_occurrence_effects(decision, occurrence_amounts)
            criterion_contributions.append(
                {
                    "criterion_code": criterion_code,
                    "rule_code": rule["rule_code"],
                    "occurrence_id": (
                        decision["occurrences"][0]["occurrence_id"]
                        if decision["occurrences"]
                        else None
                    ),
                    "kind": "band",
                    "amount": _decimal_text(score),
                }
            )
        else:
            score = max_score
            criterion_contributions.append(
                {
                    "criterion_code": criterion_code,
                    "rule_code": None,
                    "occurrence_id": None,
                    "kind": "base",
                    "amount": _decimal_text(max_score),
                }
            )
            overflow = False
            for rule in rules:
                if rule["direction"] != "deduct":
                    continue
                decision = decision_by_code[rule["rule_code"]]
                if decision["status"] != "triggered":
                    if decision["calculated_effect"] is None:
                        decision["calculated_effect"] = "0"
                    continue
                # Use the remaining criterion score, not only max_score, so
                # individually legal rules cannot cumulatively drive a score
                # below zero.
                amounts, error = _deduction_effects(rule, decision, score)
                if error:
                    overflow = True
                    decision["status"] = "invalid"
                    decision["calculated_effect"] = None
                    issues.append(
                        _issue(
                            error,
                            "block",
                            criterion_code,
                            rule["rule_code"],
                            "runtime deduction exceeds criterion max_score",
                        )
                    )
                    break
                for occurrence, amount in zip(decision["occurrences"], amounts):
                    if amount == 0:
                        continue
                    score += amount
                    criterion_contributions.append(
                        {
                            "criterion_code": criterion_code,
                            "rule_code": rule["rule_code"],
                            "occurrence_id": occurrence["occurrence_id"],
                            "kind": "deduction",
                            "amount": _decimal_text(amount),
                        }
                    )
            if overflow:
                outcomes.append(
                    {
                        "criterion_code": criterion_code,
                        "status": "invalid",
                        "auto_score": None,
                        "final_score": None,
                        "max_score": criterion["max_score"],
                    }
                )
                continue
        status = "review_required" if review_triggered else "calculated"
        outcomes.append(
            {
                "criterion_code": criterion_code,
                "status": status,
                "auto_score": _decimal_text(score),
                "final_score": None if review_triggered else _decimal_text(score),
                "max_score": criterion["max_score"],
            }
        )
        contributions.extend(criterion_contributions)
    return outcomes, contributions


def execute_rule_plan(*, request, checker_registry, llm_runtime, profile):
    """Execute a frozen rule plan without database, filesystem or network I/O."""

    dto = ScoringRequest.from_mapping(_plain(request))
    value = dto.to_mapping()
    _validate_profile(profile, value)
    nodes_by_code = {node["rule_code"]: node for node in value["plan"]["nodes"]}
    schema_versions = {
        node["atomic_rule_snapshot"]["schema_version"]
        for node in value["plan"]["nodes"]
    }
    # Mixed @1/@2 plans are a compatibility bridge and retain their already
    # frozen plan order.  Native M4 plans derive a stable topology themselves.
    if len(schema_versions) > 1:
        order = [node["rule_code"] for node in value["plan"]["nodes"]]
    else:
        order = _topological_order(nodes_by_code)
    decisions = []
    decision_by_code = {}
    issues = []
    occurrence_payloads = {}
    blocking_rules = set()

    for code in order:
        node = nodes_by_code[code]
        rule = node["atomic_rule_snapshot"]
        criterion_code = rule["criterion_code"]
        matrix_error = _matrix_error(rule)
        if matrix_error:
            decision = _decision_skeleton(value["plan"], rule)
            issue_code, message = matrix_error
            issues.append(_issue(issue_code, "block", criterion_code, code, message))
            decisions.append(decision)
            decision_by_code[code] = decision
            blocking_rules.add(code)
            continue

        dependencies = rule["depends_on_rule_codes"]
        if any(
            dependency not in decision_by_code
            or decision_by_code[dependency]["status"] != "triggered"
            or dependency in blocking_rules
            for dependency in dependencies
        ):
            decision = _decision_skeleton(value["plan"], rule, status="skipped")
            decision["calculated_effect"] = (
                None if rule["direction"] == "none" else "0"
            )
            decisions.append(decision)
            decision_by_code[code] = decision
            continue

        if rule["judge_type"] == "deterministic":
            decision, error, message = _deterministic_decision(
                request=value,
                node=node,
                checker_registry=checker_registry,
                occurrence_payloads=occurrence_payloads,
            )
        elif rule["judge_type"] == "semantic":
            decision, error, message = _semantic_decision(
                request=value,
                node=node,
                llm_runtime=llm_runtime,
                profile=profile,
                occurrence_payloads=occurrence_payloads,
            )
        else:
            decision = _decision_skeleton(value["plan"], rule)
            error, message = "RULE_DECISION_INVALID", "judge_type is unsupported"
        if error:
            decision["status"] = "invalid"
            decision["evidence_refs"] = []
            decision["occurrences"] = []
            decision["calculated_effect"] = None
            issues.append(_issue(error, "block", criterion_code, code, message))
            blocking_rules.add(code)
        elif decision["status"] == "triggered":
            if rule["effect_type"] == "review":
                issues.append(
                    _issue(
                        "REVIEW_REQUIRED",
                        "review",
                        criterion_code,
                        code,
                        "rule requires human review",
                    )
                )
            elif rule["effect_type"] == "block_submission":
                issues.append(
                    _issue(
                        "BLOCK_SUBMISSION",
                        "block",
                        criterion_code,
                        code,
                        "rule blocks submission",
                    )
                )
                blocking_rules.add(code)
            elif rule["effect_type"] == "report_only":
                issues.append(
                    _issue(
                        "REPORT_ONLY",
                        "info",
                        criterion_code,
                        code,
                        "rule emitted a report-only finding",
                    )
                )
        decisions.append(decision)
        decision_by_code[code] = decision

    outcomes, contributions = _criterion_results(
        order=order,
        nodes_by_code=nodes_by_code,
        decisions=decisions,
        issues=issues,
    )
    if any(item["status"] in {"invalid", "blocked"} for item in outcomes):
        status = "blocked"
    elif any(item["status"] == "review_required" for item in outcomes):
        status = "review_required"
    else:
        status = "completed"
    return RuleExecutionResult.from_mapping(
        {
            "schema_version": "rule-execution-result@1",
            "status": status,
            "criterion_outcomes": outcomes,
            "rule_decisions": decisions,
            "score_contributions": contributions,
            "review_issues": issues,
        }
    )


__all__ = ["OCCURRENCE_SCHEME", "PROMPT_VERSION", "execute_rule_plan"]
