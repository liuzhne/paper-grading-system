"""Fail-closed execution for legacy direct and composite criterion nodes.

The provider reports a bounded direct score plus source evidence.  It never
authorizes point effects: bounds, ordered child sums, parent overflow checks,
weights and final aggregation are all calculated here in Core.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.contracts import ScoringRequest
from backend.app.services.scoring.core.policy import aggregate_scores
from backend.app.services.scoring.core.policy import compile_scoring_policy
from backend.app.services.scoring.core.results import ScoringOutcome
from backend.app.services.scoring.core.rule_executor import execute_rule_plan


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    method = getattr(value, "to_mapping", None)
    if callable(method):
        return _plain(method())
    return value


def _decimal_text(value):
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    if decimal.is_zero():
        return "0"
    return format(decimal.normalize(), "f")


def _request_identity(request):
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


def _issue(code, severity, criterion_code, rule_code, message):
    return {
        "code": code,
        "severity": severity,
        "criterion_code": criterion_code,
        "rule_code": rule_code,
        "message": message,
    }


def _decision(request, node, *, status, evidence_refs, effect=None):
    plan = request["plan"]
    return {
        "version_hash": plan["rubric_snapshot_hash"],
        "rule_code": node["rule_code"],
        "criterion_code": node["criterion_code"],
        "direction": "band",
        "effect_type": "score",
        "status": status,
        "selected_level_code": None,
        "evidence_refs": evidence_refs,
        "occurrences": [],
        "calculated_effect": effect,
    }


def _score(value, *, maximum):
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        score = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not score.is_finite() or score < 0 or score > maximum:
        return None
    return score


def _evidence_refs(response, evidence_units, *, requirement):
    by_id = {item["evidence_unit_id"]: item for item in evidence_units}
    raw_items = response.get("evidence") if isinstance(response, Mapping) else None
    if not isinstance(raw_items, (list, tuple)) or not raw_items:
        return ([], requirement == "optional")
    refs = []
    for raw in raw_items:
        if not isinstance(raw, Mapping) or raw.get("type") != "source_quote":
            return [], False
        unit = by_id.get(raw.get("evidence_unit_id"))
        quote = raw.get("quote")
        if (
            unit is None
            or not isinstance(quote, str)
            or not quote.strip()
            or quote.strip() not in unit["normalized_text"]
        ):
            return [], False
        refs.append(
            {
                "evidence_unit_id": unit["evidence_unit_id"],
                "evidence_type": "source_quote",
                "locator": _plain(unit["locator"]),
                "payload_hash": canonical_sha256(_plain(raw)),
            }
        )
    return refs, True


def _run_provider(request, llm_runtime, criterion, *, requirement):
    port = getattr(llm_runtime, "score_legacy_criterion", None)
    if not callable(port):
        return None, [], False, False
    raw = port(request=_plain(request), criterion=_plain(criterion))
    response = raw if isinstance(raw, Mapping) else {}
    maximum = Decimal(criterion["max_score"])
    score = _score(response.get("direct_score"), maximum=maximum)
    refs, evidence_valid = _evidence_refs(
        response,
        request["document"]["evidence_units"],
        requirement=requirement,
    )
    manual_review = response.get("need_manual_review") is True
    return score, refs, evidence_valid, manual_review


def _invalid_criterion(node):
    criterion = node["criterion_snapshot"]
    return {
        "criterion_code": node["criterion_code"],
        "status": "invalid",
        "auto_score": None,
        "final_score": None,
        "max_score": criterion["max_score"],
    }


def _calculated_criterion(node, score):
    return {
        "criterion_code": node["criterion_code"],
        "status": "calculated",
        "auto_score": _decimal_text(score),
        "final_score": _decimal_text(score),
        "max_score": node["criterion_snapshot"]["max_score"],
    }


def _execute_direct(request, node, llm_runtime):
    score, refs, evidence_valid, manual_review = _run_provider(
        request,
        llm_runtime,
        node["legacy_criterion"],
        requirement=node["evidence_requirement"],
    )
    if score is None or not evidence_valid:
        code = (
            "REQUIRED_EVIDENCE_INVALID"
            if not evidence_valid
            else "DIRECT_SCORE_OUT_OF_RANGE"
        )
        message = (
            "legacy direct response lacks valid required evidence"
            if not evidence_valid
            else "legacy direct score must be within 0..max_score"
        )
        return {
            "criterion": _invalid_criterion(node),
            "decision": _decision(
                request, node, status="invalid", evidence_refs=[], effect=None
            ),
            "contributions": [],
            "issues": [
                _issue(code, "block", node["criterion_code"], node["rule_code"], message)
            ],
        }
    issues = []
    if manual_review:
        issues.append(
            _issue(
                "MANUAL_REVIEW_REQUESTED",
                "review",
                node["criterion_code"],
                node["rule_code"],
                "legacy scorer requested human review",
            )
        )
    amount = _decimal_text(score)
    return {
        "criterion": _calculated_criterion(node, score),
        "decision": _decision(
            request, node, status="triggered", evidence_refs=refs, effect=amount
        ),
        "contributions": [
            {
                "criterion_code": node["criterion_code"],
                "rule_code": node["rule_code"],
                "occurrence_id": None,
                "kind": "band",
                "amount": amount,
            }
        ],
        "issues": issues,
    }


def _execute_composite(request, node, llm_runtime):
    total = Decimal("0")
    refs = []
    contributions = []
    issues = []
    for child in node["children"]:
        score, child_refs, evidence_valid, manual_review = _run_provider(
            request,
            llm_runtime,
            child,
            requirement=node["evidence_requirement"],
        )
        if score is None or not evidence_valid:
            code = (
                "REQUIRED_EVIDENCE_INVALID"
                if not evidence_valid
                else "DIRECT_SCORE_OUT_OF_RANGE"
            )
            return {
                "criterion": _invalid_criterion(node),
                "decision": _decision(
                    request, node, status="invalid", evidence_refs=[], effect=None
                ),
                "contributions": [],
                "issues": [
                    _issue(
                        code,
                        "block",
                        node["criterion_code"],
                        node["rule_code"],
                        "composite child score or evidence is invalid",
                    )
                ],
            }
        total += score
        refs.extend(child_refs)
        contributions.append(
            {
                "criterion_code": node["criterion_code"],
                "rule_code": node["rule_code"],
                "occurrence_id": None,
                "kind": "band",
                "amount": _decimal_text(score),
            }
        )
        if manual_review:
            issues.append(
                _issue(
                    "MANUAL_REVIEW_REQUESTED",
                    "review",
                    node["criterion_code"],
                    node["rule_code"],
                    "legacy composite child requested human review",
                )
            )
    if total > Decimal(node["criterion_snapshot"]["max_score"]):
        return {
            "criterion": _invalid_criterion(node),
            "decision": _decision(
                request, node, status="invalid", evidence_refs=[], effect=None
            ),
            "contributions": [],
            "issues": [
                _issue(
                    "COMPOSITE_SCORE_OVERFLOW",
                    "block",
                    node["criterion_code"],
                    node["rule_code"],
                    "ordered child sum exceeds parent max_score",
                )
            ],
        }
    return {
        "criterion": _calculated_criterion(node, total),
        "decision": _decision(
            request,
            node,
            status="triggered",
            evidence_refs=refs,
            effect=_decimal_text(total),
        ),
        "contributions": contributions,
        "issues": issues,
    }


def execute_legacy_compatibility_plan(
    *, request, checker_registry, llm_runtime, profile
):
    """Execute a legacy_unversioned plan@3 and aggregate every parent once."""

    dto = request if isinstance(request, ScoringRequest) else ScoringRequest.from_mapping(
        _plain(request)
    )
    value = dto.to_mapping()
    plan = value["plan"]
    if (
        plan["schema_version"] != "rule-execution-plan@3"
        or plan["rubric_source_kind"] != "legacy_unversioned"
    ):
        raise ValueError("legacy compatibility executor requires legacy_unversioned plan@3")

    atomic = execute_rule_plan(
        request=dto,
        checker_registry=checker_registry,
        llm_runtime=llm_runtime,
        profile=profile,
    ).to_mapping()
    criteria = list(atomic["criterion_outcomes"])
    decisions = list(atomic["rule_decisions"])
    contributions = list(atomic["score_contributions"])
    issues = list(atomic["review_issues"])

    nodes = {node["rule_code"]: node for node in plan["nodes"]}
    for rule_code in plan["dependency_order"]:
        node = nodes[rule_code]
        if node["node_kind"] == "atomic_rule":
            continue
        if node["node_kind"] == "legacy_direct_criterion":
            result = _execute_direct(value, node, llm_runtime)
        elif node["node_kind"] == "composite_criterion":
            result = _execute_composite(value, node, llm_runtime)
        else:
            raise ValueError("unsupported compatibility node kind")
        criteria.append(result["criterion"])
        decisions.append(result["decision"])
        contributions.extend(result["contributions"])
        issues.extend(result["issues"])

    blocked = atomic["status"] == "blocked" or any(
        item["status"] in {"invalid", "blocked"} for item in criteria
    )
    review_required = atomic["status"] == "review_required"
    unrounded_total = None
    final_total = None
    grade = None
    if not blocked and not review_required:
        criterion_snapshots = {
            node["criterion_code"]: node["criterion_snapshot"]
            for node in plan["nodes"]
        }
        aggregate_items = []
        for outcome in criteria:
            snapshot = criterion_snapshots[outcome["criterion_code"]]
            aggregate_items.append(
                {
                    "criterion_code": outcome["criterion_code"],
                    "max_score": snapshot["max_score"],
                    "weight": snapshot["weight"],
                    "raw_score": outcome["auto_score"],
                    "final_score": outcome["final_score"],
                    "auto_score_status": "calculated",
                }
            )
        policy = compile_scoring_policy(
            plan["policy_snapshot"],
            total_score=plan["policy_snapshot"]["aggregation"]["total_score"],
        )
        aggregated = aggregate_scores(policy, aggregate_items)
        unrounded_total = _decimal_text(aggregated.unrounded_total)
        final_total = _decimal_text(aggregated.rounded_total)
        grade = aggregated.grade

    status = "blocked" if blocked else (
        "review_required" if review_required else "completed"
    )
    return ScoringOutcome.from_mapping(
        {
            "schema_version": "scoring-outcome@2",
            "request_identity": _request_identity(value),
            "criterion_outcomes": criteria,
            "rule_decisions": decisions,
            "score_contributions": contributions,
            "review_issues": issues,
            "unrounded_total": unrounded_total,
            "final_total": final_total,
            "grade": grade,
            "status": status,
            "audit_identity": _plain(value["runtime_identity"]),
        }
    )


__all__ = ["execute_legacy_compatibility_plan"]
