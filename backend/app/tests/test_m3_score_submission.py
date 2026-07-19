"""M3 pure scoring vertical-slice executable specification.

The target implementation is intentionally capability-gated while M3 is being
built.  A gate recognizes only an absent target module/symbol.  Once a public
symbol exists, signature errors, internal import errors and behavioral defects
remain ordinary failures.
"""

from __future__ import annotations

import builtins
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
import importlib
import socket
import sqlite3

import pytest

from backend.app.services.cache import llm_cache
from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.results import ScoringOutcome
from backend.app.tests.m2_contract_fixtures import (
    PROFILE_KEY,
    PROFILE_VERSION,
    document_snapshot_projection,
)
from backend.app.tests.m3_contract_fixtures import (
    CHECKER_KEY,
    CHECKER_VERSION,
    DETERMINISTIC_RULE_CODE,
    SEMANTIC_RULE_CODE,
    deterministic_missing_owner_observation,
    idempotency_projection,
    plan_hash_projection,
    scoring_request_payload,
    semantic_high_band_response,
)


ENGINE_MODULE = "backend.app.services.scoring.core.engine"
CONTRACTS_MODULE = "backend.app.services.scoring.core.contracts"
M2_PROMPT_VERSION = "2026-07-19-5"


class M3CapabilityUnavailable(RuntimeError):
    """The only exception an M3 capability gate may turn into XFAIL."""


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
    # Deliberately avoid getattr/module __getattr__ magic.
    return vars(module).get(symbol)


_SCORE_SUBMISSION = _probe_symbol(ENGINE_MODULE, "score_submission")
_PROMPT_ENVELOPE_V3 = _probe_symbol(CONTRACTS_MODULE, "PromptEnvelopeV3")


def _requires_core(*, envelope: bool = True):
    missing = []
    if _SCORE_SUBMISSION is None:
        missing.append(f"{ENGINE_MODULE}.score_submission")
    if envelope and _PROMPT_ENVELOPE_V3 is None:
        missing.append(f"{CONTRACTS_MODULE}.PromptEnvelopeV3")
    return pytest.mark.xfail(
        condition=bool(missing),
        reason="M3 capability is not implemented: " + ", ".join(missing),
        raises=M3CapabilityUnavailable,
        strict=True,
    )


requires_score_submission = _requires_core()


def _require_score_submission():
    if _SCORE_SUBMISSION is None:
        raise M3CapabilityUnavailable(
            f"{ENGINE_MODULE}.score_submission is not implemented"
        )
    if _PROMPT_ENVELOPE_V3 is None:
        raise M3CapabilityUnavailable(
            f"{CONTRACTS_MODULE}.PromptEnvelopeV3 is not implemented"
        )
    return _SCORE_SUBMISSION


def _mapping(value) -> dict:
    method = getattr(value, "to_mapping", None)
    assert callable(method), "Core contracts must expose to_mapping()"
    mapped = method()
    assert isinstance(mapped, Mapping)
    return deepcopy(dict(mapped))


@dataclass(frozen=True, slots=True)
class _TechnicalProposalProfile:
    profile_key: str = PROFILE_KEY
    profile_version: str = PROFILE_VERSION
    prompt_version: str = "technical-proposal-prompt@1"

    def select_prompt_metadata(self, *, metadata):
        # M3 proves opt-in metadata selection.  Sensitive/legacy thesis fields
        # must never be copied by a generic Core serializer.
        return {
            "project_name": deepcopy(metadata["project_name"])
        } if "project_name" in metadata else {}

    def build_prompt_extensions(self, *, submission_snapshot, document_snapshot):
        if submission_snapshot["profile_key"] != self.profile_key:
            raise ValueError("submission profile does not match TestProfile")
        if document_snapshot["profile_key"] != self.profile_key:
            raise ValueError("document profile does not match TestProfile")
        if document_snapshot["profile_version"] != self.profile_version:
            raise ValueError("document profile version does not match TestProfile")
        return {
            "metadata": self.select_prompt_metadata(
                metadata=submission_snapshot.get("metadata", {})
            ),
            "profile_extensions": deepcopy(
                document_snapshot.get("profile_extensions", {}).get(
                    self.profile_key, {}
                )
            ),
        }


class _CheckerRegistry:
    def __init__(self, *, available: bool = True):
        self.available = available
        self.calls: list[dict] = []
        self.executions: list[dict] = []

    def resolve(
        self,
        *,
        checker_key,
        checker_version,
        checker_params,
        profile_key,
        document_schema_version,
    ):
        call = {
            "checker_key": checker_key,
            "checker_version": checker_version,
            "checker_params": deepcopy(checker_params),
            "profile_key": profile_key,
            "document_schema_version": document_schema_version,
        }
        self.calls.append(call)
        if (
            not self.available
            or checker_key != CHECKER_KEY
            or checker_version != CHECKER_VERSION
        ):
            raise KeyError(f"unknown checker {checker_key}@{checker_version}")

        def checker(*, document, params):
            self.executions.append(
                {"document": deepcopy(document), "params": deepcopy(params)}
            )
            return deterministic_missing_owner_observation()

        return checker


class _CapturingRuntime:
    def __init__(self, response=None):
        self.response = deepcopy(response or semantic_high_band_response())
        self.envelopes = []
        self.provider_mappings: list[dict] = []
        self.cache_hashes: list[str] = []

    def score(self, *, envelope):
        assert isinstance(envelope, _PROMPT_ENVELOPE_V3), (
            "score_submission must send the public PromptEnvelopeV3 DTO to "
            "the runtime"
        )
        mapping = _mapping(envelope)
        self.envelopes.append(envelope)
        self.provider_mappings.append(mapping)
        # The provider input and the cache identity must be the same canonical
        # object; no lossy parallel payload is allowed.
        self.cache_hashes.append(canonical_sha256(mapping))
        assert envelope.canonical_hash() == self.cache_hashes[-1]
        return deepcopy(self.response)


def _score(*, request=None, registry=None, runtime=None, profile=None):
    score_submission = _require_score_submission()
    return score_submission(
        request=deepcopy(request or scoring_request_payload()),
        checker_registry=registry or _CheckerRegistry(),
        llm_runtime=runtime or _CapturingRuntime(),
        profile=profile or _TechnicalProposalProfile(),
    )


def _refresh_request_identity(request: dict) -> None:
    policy = request["plan"]["policy_snapshot"]
    policy_without_hash = deepcopy(policy)
    policy_without_hash.pop("policy_hash", None)
    policy["policy_hash"] = canonical_sha256(policy_without_hash)
    request["plan"]["policy_hash"] = policy["policy_hash"]
    request["document"]["document_snapshot_hash"] = canonical_sha256(
        document_snapshot_projection(request["document"])
    )
    request["plan"]["plan_hash"] = canonical_sha256(
        plan_hash_projection(request["plan"])
    )
    request["idempotency_key"] = canonical_sha256(
        idempotency_projection(request)
    )


@requires_score_submission
def test_score_submission_runs_one_deterministic_deduction_and_one_semantic_band():
    registry = _CheckerRegistry()
    runtime = _CapturingRuntime()

    outcome = _score(registry=registry, runtime=runtime)

    assert isinstance(outcome, ScoringOutcome)
    result = outcome.to_mapping()
    assert result["status"] == "completed"
    assert result["unrounded_total"] == "90"
    assert result["final_total"] == "90"
    assert result["grade"] == "A"
    assert result["review_issues"] == []
    assert {
        item["criterion_code"]: (
            item["status"], item["auto_score"], item["final_score"]
        )
        for item in result["criterion_outcomes"]
    } == {
        "RISK_CONTROL": ("calculated", "10", "10"),
        "SOLUTION_FIT": ("calculated", "80", "80"),
    }
    assert {
        item["rule_code"]: item["status"] for item in result["rule_decisions"]
    } == {
        DETERMINISTIC_RULE_CODE: "triggered",
        SEMANTIC_RULE_CODE: "triggered",
    }
    contribution_totals = {}
    for item in result["score_contributions"]:
        contribution_totals.setdefault(item["criterion_code"], Decimal("0"))
        contribution_totals[item["criterion_code"]] += Decimal(item["amount"])
    assert contribution_totals == {
        "RISK_CONTROL": Decimal("10"),
        "SOLUTION_FIT": Decimal("80"),
    }
    request = scoring_request_payload()
    assert result["request_identity"] == {
        "idempotency_key": request["idempotency_key"],
        "document_snapshot_hash": request["document"]["document_snapshot_hash"],
        "rubric_snapshot_hash": request["plan"]["rubric_snapshot_hash"],
        "plan_hash": request["plan"]["plan_hash"],
        "policy_hash": request["plan"]["policy_hash"],
        "profile_key": PROFILE_KEY,
    }
    assert result["audit_identity"] == request["runtime_identity"]
    assert len(registry.calls) == len(registry.executions) == 1
    assert len(runtime.envelopes) == 1


@requires_score_submission
def test_semantic_provider_receives_complete_v3_audit_envelope_and_exact_cache_hash():
    request = scoring_request_payload()
    runtime = _CapturingRuntime()

    _score(request=request, runtime=runtime)

    provider_input = runtime.provider_mappings[0]
    semantic_node = next(
        node
        for node in request["plan"]["nodes"]
        if node["rule_code"] == SEMANTIC_RULE_CODE
    )
    assert provider_input["schema_version"] == "prompt-envelope@3"
    assert provider_input["prompt_version"] == llm_cache.PROMPT_VERSION
    assert provider_input["runtime_identity"] == request["runtime_identity"]
    assert provider_input["rubric_identity"] == {
        "rubric_source_kind": request["plan"]["rubric_source_kind"],
        "rubric_version_id": request["plan"]["rubric_version_id"],
        "rubric_version_hash": request["plan"]["rubric_version_hash"],
        "rubric_hash_scheme": request["plan"]["rubric_hash_scheme"],
    }
    for key in ("rubric_snapshot_hash", "plan_hash", "policy_hash"):
        expected_key = "plan_hash" if key == "plan_hash" else key
        assert provider_input[key] == request["plan"][expected_key]
    assert provider_input["criterion_snapshot"] == semantic_node[
        "criterion_snapshot"
    ]
    assert provider_input["atomic_rule_snapshot"] == semantic_node[
        "atomic_rule_snapshot"
    ]
    assert provider_input["submission"] == {
        "source_artifact_hash": request["submission"]["source_artifact_hash"],
        "normalized_content_hash": request["document"]["content_hash"],
        "document_snapshot_hash": request["document"]["document_snapshot_hash"],
    }
    provided_evidence = provider_input["evidence_units"]
    document_evidence = {
        item["evidence_unit_id"]: item
        for item in request["document"]["evidence_units"]
    }
    assert provided_evidence, "semantic scoring requires an authorized evidence subset"
    assert len({item["evidence_unit_id"] for item in provided_evidence}) == len(
        provided_evidence
    )
    assert all(
        item == document_evidence[item["evidence_unit_id"]]
        for item in provided_evidence
    )
    response_evidence = semantic_high_band_response()["evidence"]
    provided_ids = {item["evidence_unit_id"] for item in provided_evidence}
    for evidence in response_evidence:
        assert evidence["evidence_unit_id"] in provided_ids
        assert evidence["quote"] in document_evidence[
            evidence["evidence_unit_id"]
        ]["normalized_text"]
    extensions = provider_input["profile_prompt_extensions"]
    assert extensions["metadata"] == {
        "project_name": request["submission"]["metadata"]["project_name"]
    }
    serialized = repr(provider_input)
    for forbidden in (
        "vendor_name",
        "uploader_email",
        "student_id",
        "internal_tracking_tag",
    ):
        assert forbidden not in serialized
    assert runtime.cache_hashes == [canonical_sha256(provider_input)]


@requires_score_submission
@pytest.mark.parametrize(
    "identity_part",
    (
        "runtime-provider",
        "rubric-version",
        "execution-plan",
        "policy",
        "source-artifact",
        "document-parser",
    ),
)
def test_complete_prompt_cache_identity_changes_with_every_m3_audit_input(
    identity_part,
):
    baseline_runtime = _CapturingRuntime()
    _score(runtime=baseline_runtime)

    changed = scoring_request_payload()
    if identity_part == "runtime-provider":
        changed["runtime_identity"]["provider"]["artifact_hash"] = "7" * 64
    elif identity_part == "rubric-version":
        changed["plan"]["rubric_version_hash"] = "e" * 64
    elif identity_part == "execution-plan":
        changed["plan"]["engine_contract_version"] = "scoring-core@1.0.1"
        changed["runtime_identity"]["engine_contract_version"] = "scoring-core@1.0.1"
    elif identity_part == "policy":
        changed["plan"]["policy_snapshot"]["rounding"]["digits"] = 1
    elif identity_part == "source-artifact":
        changed["submission"]["source_artifact_hash"] = "b" * 64
        changed["submission"]["artifact_refs"][0].update(
            {
                "ref": "blob:sha256:" + "b" * 64,
                "content_hash": "b" * 64,
            }
        )
    else:
        changed["document"]["parser_version"] = "technical-proposal-parser@2"
    _refresh_request_identity(changed)
    changed_runtime = _CapturingRuntime()

    _score(request=changed, runtime=changed_runtime)

    assert changed_runtime.cache_hashes[0] != baseline_runtime.cache_hashes[0]


@requires_score_submission
@pytest.mark.parametrize(
    ("mutation", "issue_code"),
    (
        ("fabricated-quote", "REQUIRED_EVIDENCE_INVALID"),
        ("unknown-level", "UNKNOWN_LEVEL"),
        ("unknown-rule", "UNKNOWN_RULE"),
    ),
)
def test_semantic_contract_failures_block_the_affected_criterion(
    mutation, issue_code
):
    response = semantic_high_band_response()
    if mutation == "fabricated-quote":
        response["evidence"][0]["quote"] = "原文中并不存在的方案承诺"
    elif mutation == "unknown-level":
        response["level_code"] = "FIT_IMAGINARY"
    else:
        response["rule_code"] = "proposal.unknown.v1"

    outcome = _score(runtime=_CapturingRuntime(response))
    result = outcome.to_mapping()

    assert result["status"] == "blocked"
    assert result["unrounded_total"] is None
    assert result["final_total"] is None
    assert result["grade"] is None
    affected = next(
        item
        for item in result["criterion_outcomes"]
        if item["criterion_code"] == "SOLUTION_FIT"
    )
    assert affected["status"] in {"invalid", "blocked"}
    assert affected["auto_score"] is None
    assert affected["final_score"] is None
    assert issue_code in {item["code"] for item in result["review_issues"]}


@requires_score_submission
def test_profile_mismatch_and_unknown_checker_fail_closed_before_semantic_runtime():
    runtime = _CapturingRuntime()
    wrong_profile = _TechnicalProposalProfile(profile_key="thesis")
    with pytest.raises((TypeError, ValueError), match=r"(?i)profile"):
        _score(profile=wrong_profile, runtime=runtime)
    assert runtime.envelopes == []

    with pytest.raises((KeyError, TypeError, ValueError), match=r"(?i)checker"):
        _score(registry=_CheckerRegistry(available=False), runtime=runtime)
    assert runtime.envelopes == []


@requires_score_submission
def test_score_submission_is_pure_and_opens_no_database_file_or_socket(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("pure score_submission attempted infrastructure I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)

    outcome = _score()

    assert outcome.to_mapping()["final_total"] == "90"


@requires_score_submission
def test_m3_core_prompt_contract_bumps_global_prompt_version():
    _require_score_submission()
    assert llm_cache.PROMPT_VERSION != M2_PROMPT_VERSION, (
        "prompt-envelope@3 changes the exact provider/cache input and must bump "
        "PROMPT_VERSION"
    )
