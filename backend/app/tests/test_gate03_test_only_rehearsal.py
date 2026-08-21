from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app.core.config import Settings
from backend.app.db.models import ReleaseGateApproval
from backend.app.db.models import ReleaseGateRun
from backend.app.eval.gating import evaluation_sha256
from backend.app.eval.gating import GateValidationError
from backend.app.services.batch_scoring.jobs import evaluate_observation_policy
from backend.app.services.batch_scoring.jobs import validate_observation_policy
from backend.app.tests.test_release_gate_profiles import _candidate
from backend.app.tests.test_release_gate_profiles import _profile_payload
from backend.app.tests.test_release_gate_profiles import _published_test_rubric


PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEST_ONLY_ARCHIVE = (
    PROJECT_ROOT / "docs" / "baselines" / "gate-03-test-only-rehearsal.json"
)


def _observation_policy():
    return {
        "schema_version": "core-cutover-observation-policy@1",
        "minimum_sample_size": 2,
        "observation_window": {"minimum_completed_items": 2},
        "thresholds": {
            "max_abs_legacy_core_delta": 0.5,
            "max_invalid_evidence_rate": 0.01,
            "max_unauthorized_rule_rate": 0.0,
            "max_manual_review_rate": 0.25,
            "min_cache_hit_rate": 0.5,
            "max_checker_failure_rate": 0.0,
            "max_llm_failure_rate": 0.0,
            "max_retry_rate": 0.1,
            "max_p95_latency_ms": 5000,
        },
        "fallback_tolerance": {"max_abs_score_delta": 0.5},
    }


def _operational_metrics():
    return {
        "sample_size": 2,
        "completed_items": 2,
        "invalid_evidence_rate": "0",
        "unauthorized_rule_rate": "0",
        "manual_review_rate": "0",
        "cache_hit_rate": "0.75",
        "cache_by_profile_and_rubric_version": {
            "thesis:test-version": {
                "hits": 3,
                "misses": 1,
                "hit_rate": "0.75",
            }
        },
        "checker_failure_rate": "0",
        "llm_failure_rate": "0",
        "retry_rate": "0",
        "p50_latency_ms": "120",
        "p95_latency_ms": "180",
        "legacy_core_delta": {"available": True, "values": ["0", "0"]},
        "error_counts": {},
        "attempt_error_counts": {},
    }


def _comparison(reference_id, marker):
    return {
        "reference_id": reference_id,
        "reference_sha256": marker * 64,
        "status": "passed",
        "metric_deltas": {
            "qwk": 0.0,
            "mae": 0.0,
            "rmse": 0.0,
            "exact_grade_agreement": 0.0,
            "adjacent_grade_agreement": 0.0,
            "review_rate": 0.0,
            "invalid_evidence_rate": 0.0,
        },
        "significant_differences": [],
        "difference_explanations": [],
    }


def _rehearsal_candidate(profile, version_hash, hash_scheme):
    candidate = _candidate(profile, version_hash, hash_scheme)
    candidate["evaluation_id"] = "pgs-35-gate03-test-only-rehearsal"
    candidate["status"] = "ineligible"
    candidate["reproducible"] = True
    candidate["gating_eligible"] = False
    candidate["gate_passed"] = False
    candidate["missing_or_invalid"] = [
        "test-only rehearsal cannot authorize a production release"
    ]
    candidate["execution"] = {
        "mode": "test_only",
        "dataset_class": "synthetic",
        "model_class": "immutable_test_fixture",
    }
    candidate["production_default_switch_authorized"] = False
    candidate["anchors"]["holdout_exclusion_proven"] = True
    candidate["manual_confirmations_pending"] = []
    candidate["private_artifacts"] = {
        "location": "synthetic_test_fixture",
        "private_report_sha256": "c" * 64,
    }
    candidate["evaluation"]["per_criterion"] = {
        f"T{index:02d}": {"n": 2, "mae": 0.0, "bias": 0.0}
        for index in range(1, 7)
    }
    metrics = _operational_metrics()
    policy = validate_observation_policy(_observation_policy())
    candidate["gate03_evidence"] = {
        "schema": "paper-grading/gate-03-evidence@1",
        "observation_policy": policy,
        "observation_policy_sha256": evaluation_sha256(policy),
        "observation_source_sha256": evaluation_sha256(
            {"policy": policy, "metrics": metrics}
        ),
        "operational_metrics": metrics,
        "observation_report": evaluate_observation_policy(policy, metrics),
        "approved_baseline_comparison": _comparison(
            "approved-test-baseline", "a"
        ),
        "m5_parity_comparison": _comparison("m5-test-only-parity", "b"),
        "private_per_sample_report_sha256": "c" * 64,
    }
    candidate.pop("candidate_sha256")
    candidate["candidate_sha256"] = evaluation_sha256(candidate)
    return candidate


def _create_profile_and_candidate(client):
    rubric_id, version_id, version_hash, hash_scheme = _published_test_rubric(
        client
    )
    payload = _profile_payload(rubric_id, version_id)
    payload["gate_key"] = "GATE-03"
    payload["name"] = "PGS-35 test-only rehearsal profile"
    created = client.post("/api/release-gates/profiles", json=payload)
    assert created.status_code == 200, created.text
    profile = created.json()
    return profile, _rehearsal_candidate(
        profile, version_hash, hash_scheme
    )


def test_gate03_test_only_rehearsal_is_persisted_but_never_approvable(client):
    profile, candidate = _create_profile_and_candidate(client)

    response = client.post(
        f"/api/release-gates/profiles/{profile['id']}/rehearsals",
        json={"candidate_record": candidate},
    )

    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "ineligible"
    assert run["candidate_record"]["gating_eligible"] is False
    assert run["final_record"]["gate_passed"] is False
    assert run["final_record"]["production_default_switch_authorized"] is False
    assert len(run["final_record_sha256"]) == 64
    assert run["finalized_at"] is not None

    approval = client.post(
        f"/api/release-gates/runs/{run['id']}/approve",
        json={
            "privacy_review": {
                "dataset_deidentified": True,
                "repository_scan_passed": True,
                "anchors_exclude_holdout": True,
                "teacher_truth_external_only": True,
            }
        },
    )
    assert approval.status_code == 409
    with client.session_factory() as db:
        stored = db.scalar(select(ReleaseGateRun))
        assert stored.profile_id == profile["id"]
        assert stored.final_record_sha256 == run["final_record_sha256"]
        assert db.scalar(select(ReleaseGateApproval)) is None


def test_gate03_rehearsal_rejects_incomplete_metrics_and_comparisons(client):
    profile, candidate = _create_profile_and_candidate(client)
    incomplete = deepcopy(candidate)
    incomplete["gate03_evidence"]["operational_metrics"].pop(
        "unauthorized_rule_rate"
    )
    incomplete["gate03_evidence"]["m5_parity_comparison"].pop(
        "significant_differences"
    )
    incomplete.pop("candidate_sha256")
    incomplete["candidate_sha256"] = evaluation_sha256(incomplete)

    response = client.post(
        f"/api/release-gates/profiles/{profile['id']}/rehearsals",
        json={"candidate_record": incomplete},
    )

    assert response.status_code == 400
    assert "unauthorized_rule_rate" in response.text
    assert "significant_differences" in response.text


def test_gate03_rehearsal_rejects_observation_sample_identity_mismatch(client):
    profile, candidate = _create_profile_and_candidate(client)
    evidence = candidate["gate03_evidence"]
    evidence["operational_metrics"]["sample_size"] = 3
    evidence["observation_report"] = evaluate_observation_policy(
        evidence["observation_policy"],
        evidence["operational_metrics"],
    )
    candidate.pop("candidate_sha256")
    candidate["candidate_sha256"] = evaluation_sha256(candidate)

    response = client.post(
        f"/api/release-gates/profiles/{profile['id']}/rehearsals",
        json={"candidate_record": candidate},
    )

    assert response.status_code == 400
    assert "observation_source_sha256" in response.text


def test_gate03_rehearsal_is_bound_to_database_profile_identity(client):
    profile, candidate = _create_profile_and_candidate(client)
    tampered = deepcopy(candidate)
    tampered["dataset"]["dataset_version"] = "different-test-dataset"
    tampered.pop("candidate_sha256")
    tampered["candidate_sha256"] = evaluation_sha256(tampered)

    response = client.post(
        f"/api/release-gates/profiles/{profile['id']}/rehearsals",
        json={"candidate_record": tampered},
    )

    assert response.status_code == 400
    assert "dataset.dataset_version" in response.text


def test_formal_gate03_candidate_cannot_skip_operational_evidence(client):
    profile, rehearsal = _create_profile_and_candidate(client)
    formal = deepcopy(rehearsal)
    formal["evaluation_id"] = "pgs-35-formal-candidate-without-evidence"
    formal["status"] = "candidate_awaiting_approval"
    formal["gating_eligible"] = True
    formal["missing_or_invalid"] = []
    formal["execution"] = {
        "mode": "formal_release",
        "dataset_class": "external_private_holdout",
        "model_class": "immutable_production",
    }
    formal["manual_confirmations_pending"] = [
        "privacy_review",
        "anchor_holdout_exclusion",
        "baseline_and_regression_threshold_approval",
    ]
    formal.pop("gate03_evidence")
    formal.pop("candidate_sha256")
    formal["candidate_sha256"] = evaluation_sha256(formal)

    response = client.post(
        f"/api/release-gates/profiles/{profile['id']}/runs",
        json={"candidate_record": formal},
    )

    assert response.status_code == 400
    assert "gate03_evidence" in response.text


def test_gate03_cli_attaches_operator_evidence_before_registration(
    client, tmp_path
):
    from backend.app.scripts import run_qwk_eval

    _profile, candidate = _create_profile_and_candidate(client)
    evidence = candidate.pop("gate03_evidence")
    candidate.pop("execution")
    candidate.pop("production_default_switch_authorized")
    candidate["status"] = "candidate_awaiting_approval"
    candidate["gating_eligible"] = True
    candidate["missing_or_invalid"] = []
    candidate["manual_confirmations_pending"] = [
        "privacy_review",
        "anchor_holdout_exclusion",
        "baseline_and_regression_threshold_approval",
    ]
    candidate.pop("candidate_sha256")
    candidate["candidate_sha256"] = evaluation_sha256(candidate)
    evidence_path = tmp_path / "gate03-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    result = run_qwk_eval._apply_gate03_evidence(
        SimpleNamespace(
            gate_key="GATE-03",
            gate03_evidence=str(evidence_path),
        ),
        candidate,
    )

    assert result["execution"]["mode"] == "formal_release"
    assert result["gate03_evidence"] == evidence
    assert result["production_default_switch_authorized"] is False
    assert result["candidate_sha256"] == evaluation_sha256(
        {
            key: value
            for key, value in result.items()
            if key != "candidate_sha256"
        }
    )

    with pytest.raises(GateValidationError, match="--gate03-evidence"):
        run_qwk_eval._apply_gate03_evidence(
            SimpleNamespace(gate_key="GATE-03", gate03_evidence=None),
            candidate,
        )


def test_formal_gate03_approval_alone_can_authorize_later_cutover(client):
    profile, candidate = _create_profile_and_candidate(client)
    candidate["evaluation_id"] = "pgs-35-formal-candidate-complete-evidence"
    candidate["status"] = "candidate_awaiting_approval"
    candidate["gating_eligible"] = True
    candidate["missing_or_invalid"] = []
    candidate["execution"] = {
        "mode": "formal_release",
        "dataset_class": "external_private_holdout",
        "model_class": "immutable_production",
    }
    candidate["manual_confirmations_pending"] = [
        "privacy_review",
        "anchor_holdout_exclusion",
        "baseline_and_regression_threshold_approval",
    ]
    candidate.pop("candidate_sha256")
    candidate["candidate_sha256"] = evaluation_sha256(candidate)
    registered = client.post(
        f"/api/release-gates/profiles/{profile['id']}/runs",
        json={"candidate_record": candidate},
    )
    assert registered.status_code == 200, registered.text

    approved = client.post(
        f"/api/release-gates/runs/{registered.json()['id']}/approve",
        json={
            "privacy_review": {
                "dataset_deidentified": True,
                "repository_scan_passed": True,
                "anchors_exclude_holdout": True,
                "teacher_truth_external_only": True,
            }
        },
    )

    assert approved.status_code == 200, approved.text
    final = approved.json()["final_record"]
    assert final["gate_passed"] is True
    assert final["production_default_switch_authorized"] is True


def test_gate03_test_only_archive_is_explicitly_non_production():
    archive = json.loads(TEST_ONLY_ARCHIVE.read_text(encoding="utf-8"))

    assert archive["schema"] == "paper-grading/gate-03-test-only-rehearsal@1"
    assert archive["test_contract_passed"] is True
    assert archive["database_relationship_exercised"] is True
    assert archive["gating_eligible"] is False
    assert archive["gate_passed"] is False
    assert archive["production_default_switch_authorized"] is False
    assert set(archive["evaluation"]["per_criterion_bias"]) == {
        f"T{index:02d}" for index in range(1, 7)
    }
    assert archive["operational_metrics"]["unauthorized_rule_rate"] == "0"
    assert archive["observation_policy_sha256"] == evaluation_sha256(
        archive["observation_policy"]
    )
    assert archive["observation_source_sha256"] == evaluation_sha256(
        {
            "policy": archive["observation_policy"],
            "metrics": archive["operational_metrics"],
        }
    )
    assert archive["approved_baseline_comparison"]["status"] == "passed"
    assert archive["m5_parity_comparison"]["status"] == "passed"
    assert len(archive["private_per_sample_report_sha256"]) == 64
    assert "pgs-6-real-postgres-evidence-pending" in archive[
        "why_not_release_evidence"
    ]
    assert "pgs-8-real-holdout-evidence-pending" in archive[
        "why_not_release_evidence"
    ]


def test_test_only_rehearsal_does_not_change_default_engine_mode():
    assert Settings().SCORING_ENGINE_MODE == "legacy"
