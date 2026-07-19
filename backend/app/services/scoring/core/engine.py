"""Pure M3 scoring vertical slice.

The module intentionally executes only the two rule shapes admitted by M3:
one deterministic, once-only deduction and one semantic band selection.  The
general AtomicRule state machine remains an M4 concern.  All infrastructure is
supplied through explicit arguments, so scoring is deterministic and replayable.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.contracts import PromptEnvelopeV3, ScoringRequest
from backend.app.services.scoring.core.policy import aggregate_scores, compile_scoring_policy
from backend.app.services.scoring.core.results import ScoringOutcome


# This value is deliberately mirrored by services.cache.llm_cache.  Importing
# that adapter from Core would violate the M2 dependency boundary.
PROMPT_VERSION = "2026-07-19-6"


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    method = getattr(value, "to_mapping", None)
    if callable(method):
        return _plain(method())
    return value


def _decimal_text(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def _profile_identity(profile) -> tuple[str, str]:
    key = getattr(profile, "profile_key", None)
    version = getattr(profile, "profile_version", None)
    if not isinstance(key, str) or not key.strip():
        raise TypeError("profile.profile_key must be a non-empty string")
    if not isinstance(version, str) or not version.strip():
        raise TypeError("profile.profile_version must be a non-empty string")
    return key, version


def _validate_profile(profile, request: Mapping[str, object]) -> None:
    key, version = _profile_identity(profile)
    identities = {
        request["submission"]["profile_key"],
        request["document"]["profile_key"],
        request["plan"]["business_profile_key"],
        request["runtime_identity"]["profile_key"],
        key,
    }
    if len(identities) != 1:
        raise ValueError("profile identity does not match scoring request")
    versions = {
        request["document"]["profile_version"],
        request["plan"]["business_profile_version"],
        request["runtime_identity"]["profile_version"],
        version,
    }
    if len(versions) != 1:
        raise ValueError("profile version does not match scoring request")


def _request_identity(request: Mapping[str, object]) -> dict:
    plan = request["plan"]
    document = request["document"]
    return {
        "idempotency_key": request["idempotency_key"],
        "document_snapshot_hash": document["document_snapshot_hash"],
        "rubric_snapshot_hash": plan["rubric_snapshot_hash"],
        "plan_hash": plan["plan_hash"],
        "policy_hash": plan["policy_hash"],
        "profile_key": request["submission"]["profile_key"],
    }


def _issue(code: str, criterion_code: str, rule_code: str, message: str) -> dict:
    return {
        "code": code,
        "severity": "block",
        "criterion_code": criterion_code,
        "rule_code": rule_code,
        "message": message,
    }


def _deterministic_triggered(observation: Mapping[str, object]) -> bool:
    explicit = observation.get("triggered")
    if isinstance(explicit, bool):
        return explicit
    if "measured_value" in observation and "expected_value" in observation:
        return observation["measured_value"] != observation["expected_value"]
    return str(observation.get("status", "")).casefold() in {
        "triggered",
        "failed",
        "missing",
    }


def _semantic_evidence_refs(
    response: Mapping[str, object],
    evidence_units: list[Mapping[str, object]],
) -> tuple[list[dict], bool]:
    by_id = {item["evidence_unit_id"]: item for item in evidence_units}
    raw_items = response.get("evidence")
    if not isinstance(raw_items, (list, tuple)) or not raw_items:
        return [], False
    refs = []
    for raw in raw_items:
        if not isinstance(raw, Mapping) or raw.get("type") != "source_quote":
            return [], False
        unit_id = raw.get("evidence_unit_id")
        quote = raw.get("quote")
        unit = by_id.get(unit_id)
        if (
            unit is None
            or not isinstance(quote, str)
            or not quote.strip()
            or quote.strip() not in unit["normalized_text"]
        ):
            return [], False
        refs.append(
            {
                "evidence_unit_id": unit_id,
                "evidence_type": "source_quote",
                "locator": _plain(unit["locator"]),
                "payload_hash": canonical_sha256(_plain(raw)),
            }
        )
    return refs, True


def _prompt_envelope(
    *,
    request: Mapping[str, object],
    node: Mapping[str, object],
    profile,
) -> PromptEnvelopeV3:
    submission = request["submission"]
    document = request["document"]
    plan = request["plan"]
    build_extensions = getattr(profile, "build_prompt_extensions", None)
    if not callable(build_extensions):
        raise TypeError("profile must implement build_prompt_extensions")
    extensions = build_extensions(
        submission_snapshot=_plain(submission),
        document_snapshot=_plain(document),
    )
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
            # M3 has no retriever port in score_submission yet.  Every unit in
            # this list is nevertheless an authorized, frozen snapshot unit;
            # the provider may cite only this set.
            "evidence_units": _plain(document["evidence_units"]),
            "profile_prompt_extensions": _plain(extensions),
        }
    )


def _blocked_outcome(
    *,
    request: Mapping[str, object],
    criterion_states: list[dict],
    decisions: list[dict],
    contributions: list[dict],
    issues: list[dict],
) -> ScoringOutcome:
    return ScoringOutcome.from_mapping(
        {
            "schema_version": "scoring-outcome@1",
            "request_identity": _request_identity(request),
            "criterion_outcomes": criterion_states,
            "rule_decisions": decisions,
            "score_contributions": contributions,
            "review_issues": issues,
            "unrounded_total": None,
            "final_total": None,
            "grade": None,
            "status": "blocked",
            "audit_identity": _plain(request["runtime_identity"]),
        }
    )


def score_submission(*, request, checker_registry, llm_runtime, profile) -> ScoringOutcome:
    """Score the deliberately small M3 rule subset without infrastructure I/O."""

    dto = ScoringRequest.from_mapping(_plain(request))
    value = dto.to_mapping()
    _validate_profile(profile, value)

    plan = value["plan"]
    nodes_by_code = {node["rule_code"]: node for node in plan["nodes"]}
    if set(nodes_by_code) != set(plan["dependency_order"]):
        raise ValueError("execution plan dependency order does not match its nodes")

    criterion_states: list[dict] = []
    decisions: list[dict] = []
    contributions: list[dict] = []
    issues: list[dict] = []
    aggregate_items: list[dict] = []

    for rule_code in plan["dependency_order"]:
        node = nodes_by_code[rule_code]
        criterion = node["criterion_snapshot"]
        rule = node["atomic_rule_snapshot"]
        criterion_code = criterion["criterion_code"]
        max_score = Decimal(criterion["max_score"])

        if rule["judge_type"] == "deterministic":
            if rule["direction"] != "deduct" or rule["repeat_policy"] != "once":
                raise ValueError("M3 supports only once-only deterministic deductions")
            checker = checker_registry.resolve(
                checker_key=rule["checker_key"],
                checker_version=rule["checker_version"],
                checker_params=_plain(rule["checker_params"]),
                profile_key=value["submission"]["profile_key"],
                document_schema_version=value["document"]["schema_version"],
            )
            if not callable(checker):
                raise TypeError("checker registry returned a non-callable checker")
            observation = checker(
                document=_plain(value["document"]),
                params=_plain(rule["checker_params"]),
            )
            if not isinstance(observation, Mapping):
                raise TypeError("checker observation must be a mapping")
            triggered = _deterministic_triggered(observation)
            deduction = Decimal(rule["max_points"]) if triggered else Decimal("0")
            score = max_score - deduction
            decisions.append(
                {
                    "rule_code": rule_code,
                    "status": "triggered" if triggered else "not_triggered",
                    "evidence_refs": [],
                }
            )
            contributions.append(
                {
                    "criterion_code": criterion_code,
                    "rule_code": None,
                    "kind": "base",
                    "amount": _decimal_text(max_score),
                }
            )
            if triggered:
                contributions.append(
                    {
                        "criterion_code": criterion_code,
                        "rule_code": rule_code,
                        "kind": "deduction",
                        "amount": _decimal_text(-deduction),
                    }
                )
        elif rule["judge_type"] == "semantic":
            if rule["direction"] != "band":
                raise ValueError("M3 supports only semantic band selection")
            envelope = _prompt_envelope(request=value, node=node, profile=profile)
            response = llm_runtime.score(envelope=envelope)
            if not isinstance(response, Mapping):
                response = {}

            failure_code = None
            failure_message = None
            if response.get("rule_code") != rule_code:
                failure_code = "UNKNOWN_RULE"
                failure_message = "semantic response named an unauthorized rule"
            levels = {item["level_code"]: item for item in rule["levels"]}
            level = levels.get(response.get("level_code"))
            if failure_code is None and level is None:
                failure_code = "UNKNOWN_LEVEL"
                failure_message = "semantic response selected an unknown band"
            evidence_refs, evidence_valid = _semantic_evidence_refs(
                response,
                envelope.to_mapping()["evidence_units"],
            )
            if failure_code is None and not evidence_valid:
                failure_code = "REQUIRED_EVIDENCE_INVALID"
                failure_message = "semantic response lacks valid required evidence"

            if failure_code is not None:
                decisions.append(
                    {
                        "rule_code": rule_code,
                        "status": "invalid",
                        "evidence_refs": [],
                    }
                )
                criterion_states.append(
                    {
                        "criterion_code": criterion_code,
                        "status": "invalid",
                        "auto_score": None,
                        "final_score": None,
                        "max_score": criterion["max_score"],
                    }
                )
                issues.append(
                    _issue(failure_code, criterion_code, rule_code, failure_message)
                )
                # Preserve already calculated criteria as audit facts, but a
                # required invalid semantic result blocks all aggregate fields.
                return _blocked_outcome(
                    request=value,
                    criterion_states=criterion_states,
                    decisions=decisions,
                    contributions=contributions,
                    issues=issues,
                )

            score = Decimal(level["points"])
            decisions.append(
                {
                    "rule_code": rule_code,
                    "status": "triggered",
                    "evidence_refs": evidence_refs,
                }
            )
            contributions.append(
                {
                    "criterion_code": criterion_code,
                    "rule_code": rule_code,
                    "kind": "band",
                    "amount": _decimal_text(score),
                }
            )
        else:
            raise ValueError("unsupported M3 judge_type")

        state = {
            "criterion_code": criterion_code,
            "status": "calculated",
            "auto_score": _decimal_text(score),
            "final_score": _decimal_text(score),
            "max_score": criterion["max_score"],
        }
        criterion_states.append(state)
        aggregate_items.append(
            {
                "criterion_code": criterion_code,
                "max_score": criterion["max_score"],
                "weight": criterion["weight"],
                "raw_score": _decimal_text(score),
                "final_score": _decimal_text(score),
                "auto_score_status": "calculated",
            }
        )

    policy = compile_scoring_policy(
        plan["policy_snapshot"],
        total_score=plan["policy_snapshot"]["aggregation"]["total_score"],
    )
    aggregated = aggregate_scores(policy, aggregate_items)
    return ScoringOutcome.from_mapping(
        {
            "schema_version": "scoring-outcome@1",
            "request_identity": _request_identity(value),
            "criterion_outcomes": criterion_states,
            "rule_decisions": decisions,
            "score_contributions": contributions,
            "review_issues": issues,
            "unrounded_total": _decimal_text(aggregated.unrounded_total),
            "final_total": _decimal_text(aggregated.rounded_total),
            "grade": aggregated.grade,
            "status": "completed",
            "audit_identity": _plain(value["runtime_identity"]),
        }
    )


__all__ = ["PROMPT_VERSION", "score_submission"]
