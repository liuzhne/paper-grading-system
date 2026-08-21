"""Self-contained GATE-02 contract using approved test-only identities."""

from copy import deepcopy
import json
from pathlib import Path

import pytest
from sqlalchemy import func
from sqlalchemy import select

from backend.app.db.models import ReleaseGateApproval
from backend.app.db.models import ReleaseGateProfile
from backend.app.db.models import ReleaseGateRun
from backend.app.db.models import RubricVersion
from backend.app.eval.gating import build_gate_candidate
from backend.app.eval.gating import build_regression_record
from backend.app.eval.gating import evaluation_sha256
from backend.app.eval.runner import EvalPrediction
from backend.app.eval.runner import EvalSample
from backend.app.eval.runner import evaluate
from backend.app.tests.conftest import publish_rubric_via_api


MAXIMA = {"T01": 20, "T02": 20, "T03": 20, "T04": 10, "T05": 10, "T06": 20}
TEST_ONLY_ARCHIVE = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "baselines"
    / "gate-02-test-only-parity.json"
)


def _sha(character):
    return character * 64


def _levels(maximum):
    return [
        {
            "level_code": code,
            "points": maximum * ratio,
            "descriptor": descriptor,
        }
        for code, ratio, descriptor in (
            ("EXCELLENT", 1.0, "证据完整、论证严谨，可直接复核。"),
            ("GOOD", 0.8, "主要要求满足，存在轻微且可解释的缺口。"),
            ("PASS", 0.6, "基本满足要求，但关键论证仍需补充。"),
            ("WEAK", 0.4, "仅部分满足要求，证据明显不足。"),
            ("NO_EVIDENCE", 0.0, "没有可授权的评分证据。"),
        )
    ]


def _rubric_payload():
    descriptions = {
        "T01": "选题价值、计划、问题分析与建模。",
        "T02": "需求分析、方案论证与复杂工程问题求解。",
        "T03": "信息检索、工程技术与工具使用。",
        "T04": "建模、开发、测试与优化改进。",
        "T05": "维护、可用性、安全与可持续性。",
        "T06": "工作量、难度、履责与严谨态度。",
    }
    return {
        "name": "GATE-02 T01-T06 test-only rubric",
        "version": "gate-02-test-v1",
        "total_score": 100,
        "criteria": [
            {
                "code": code,
                "name": "测试评分项 " + code,
                "max_score": maximum,
                "description": descriptions[code],
                "criterion_type": "llm_judgment",
                "scoring_mode": "banded",
                "rubric_levels": _levels(maximum),
                "display_order": index,
            }
            for index, (code, maximum) in enumerate(MAXIMA.items())
        ],
    }


def _profile_payload(rubric_id, version_id):
    return {
        "gate_key": "GATE-02",
        "name": "M5 Thesis Core parity test-only gate",
        "rubric_id": rubric_id,
        "rubric_version_id": version_id,
        "dataset_identity": {
            "dataset_id": "gate-02-test-only-t01-t06",
            "dataset_version": "2026-08-02-v1",
            "sample_count": 6,
            "dataset_manifest_sha256": _sha("1"),
            "sample_ids_sha256": _sha("2"),
            "paper_manifest_sha256": _sha("3"),
            "truth_sha256": _sha("4"),
        },
        "model_identity": {
            "provider": "test_immutable",
            "model_name": "thesis-core-parity-fixture",
            "model_version": "2026-08-02",
            "identity_method": "provider_immutable_revision",
            "immutable_revision": "thesis-core-parity-fixture@2026-08-02",
            "artifact_identity_sha256": _sha("5"),
        },
        "anchors_identity": {
            "schema": "paper-grading/anchor-manifest@1",
            "count": 0,
            "manifest_sha256": evaluation_sha256([]),
            "holdout_exclusion_proven": True,
        },
        "acceptance_thresholds": {
            "minimum_qwk": 0.9,
            "maximum_mae": 3.0,
            "maximum_rmse": 4.0,
            "minimum_exact_grade_agreement": 0.9,
            "minimum_adjacent_grade_agreement": 1.0,
            "maximum_review_rate": 0.2,
            "maximum_blocked_rate": 0.0,
            "maximum_invalid_evidence_rate": 0.0,
        },
        "regression_tolerances": {
            "qwk_drop": 0.02,
            "mae_rise": 1.0,
            "rmse_rise": 1.0,
            "exact_grade_agreement_drop": 0.05,
            "adjacent_grade_agreement_drop": 0.05,
            "review_rate_rise": 0.1,
            "invalid_evidence_rate_rise": 0.02,
        },
    }


def _evaluation_report(
    system_totals,
    *,
    difference_explanations,
    item_shifts=None,
):
    human_totals = [55, 62, 72, 82, 92, 88]
    item_shifts = item_shifts or {}
    truths = []
    predictions = []
    for index, (human_total, system_total) in enumerate(
        zip(human_totals, system_totals, strict=True), start=1
    ):
        human_items = {
            code: human_total * maximum / 100
            for code, maximum in MAXIMA.items()
        }
        # Small deterministic per-dimension offsets exercise bias reporting.
        system_items = {
            code: max(
                0,
                min(
                    maximum,
                    value
                    + ((index + offset) % 3 - 1) * 0.25
                    + item_shifts.get(code, 0),
                ),
            )
            for offset, ((code, maximum), value) in enumerate(
                zip(MAXIMA.items(), human_items.values(), strict=True)
            )
        }
        key = "gate-02-sample-%02d" % index
        truths.append(EvalSample(key, human_total, human_items))
        predictions.append(EvalPrediction(key, system_total, system_items))
    report = evaluate(predictions, truths)
    report.update(
        dataset_size=6,
        completed_runs=6,
        errors=[],
        review_rate=0.0,
        blocked_rate=0.0,
        invalid_evidence_rate=0.0,
        difference_explanations=deepcopy(difference_explanations),
    )
    return report


def _candidate(profile, version, *, evaluation_id, report, plan_hash, generated_at):
    run_identity = {
        "rubric_version_id": version.id,
        "rubric_version_hash": version.version_hash,
        "rubric_hash_scheme": version.hash_scheme,
        "rubric_snapshot_hash": _sha("7"),
        "policy_hash": _sha("8"),
        "execution_plan_hash": plan_hash,
        "plan_schema_version": "rule-execution-plan@2",
        "checker_manifest_sha256": _sha("a"),
        "business_profile_key": "thesis",
        "workflow_profile": "template_driven",
        "model_provider": profile["model_identity"]["provider"],
        "model_name": profile["model_identity"]["model_name"],
        "model_version": profile["model_identity"]["model_version"],
        "engine_version": "thesis-core-adapter@1",
        "document_snapshot_manifest_sha256": _sha("b"),
        "scored_source_manifest_sha256": _sha("c"),
        "evaluated_sample_ids_sha256": profile["dataset_identity"][
            "sample_ids_sha256"
        ],
    }
    return build_gate_candidate(
        evaluation_id=evaluation_id,
        report=report,
        dataset_identity=profile["dataset_identity"],
        run_identity=run_identity,
        run_identity_issues=[],
        anchors_identity={
            **profile["anchors_identity"],
            "holdout_exclusion_proven": False,
        },
        model_identity=profile["model_identity"],
        prompt_identity={
            "prompt_version": "2026-08-02-9",
            "prompt_source_manifest_sha256": _sha("d"),
        },
        repository_identity={
            "revision": "e" * 40,
            "branch": "codex/gate-02-test-only",
            "dirty": False,
            "lockfile": "uv.lock",
            "lockfile_sha256": _sha("f"),
        },
        private_artifacts={
            "location": "external_private_not_in_repository",
            "private_manifest_sha256": _sha("0"),
            "private_report_sha256": _sha("1"),
        },
        artifact_directory_external=True,
        generated_at=generated_at,
    )


def _approve(client, run_id):
    response = client.post(
        "/api/release-gates/runs/%s/approve" % run_id,
        json={
            "privacy_review": {
                "dataset_deidentified": True,
                "repository_scan_passed": True,
                "anchors_exclude_holdout": True,
                "teacher_truth_external_only": True,
            }
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "passed"
    return response.json()


def test_gate02_approved_baseline_and_core_candidate_share_frozen_db_profile(client):
    created = client.post("/api/rubrics", json=_rubric_payload())
    assert created.status_code == 200, created.text
    rubric_id = created.json()["id"]
    _published, identity = publish_rubric_via_api(client, rubric_id)
    with client.session_factory() as db:
        versions = db.scalars(
            select(RubricVersion).where(RubricVersion.rubric_id == rubric_id)
        ).all()
        assert len(versions) == 1
        version = versions[0]

    profile_payload = _profile_payload(rubric_id, identity["rubric_version_id"])
    profile_response = client.post(
        "/api/release-gates/profiles", json=profile_payload
    )
    assert profile_response.status_code == 200, profile_response.text
    profile = profile_response.json()
    assert profile["gate_key"] == "GATE-02"

    baseline = _candidate(
        profile_payload,
        version,
        evaluation_id="gate-02-test-only-approved-baseline",
        report=_evaluation_report([54, 64, 74, 80, 91, 86], difference_explanations=[]),
        plan_hash=_sha("6"),
        generated_at="2026-08-02T00:00:00+08:00",
    )
    baseline_run = client.post(
        "/api/release-gates/profiles/%s/runs" % profile["id"],
        json={"candidate_record": baseline},
    )
    assert baseline_run.status_code == 200, baseline_run.text
    approved_baseline = _approve(client, baseline_run.json()["id"])[
        "final_record"
    ]

    explanations = [
        {
            "criterion_code": code,
            "reason_code": (
                "EVIDENCE_CORRECTNESS_FIX"
                if index % 3 == 0
                else "POLICY_CORRECTNESS_FIX"
                if index % 3 == 1
                else "DEDUCTIVE_CORRECTNESS_FIX"
            ),
            "summary": "%s 使用冻结证据、策略和授权分值重新核算。" % code,
        }
        for index, code in enumerate(MAXIMA)
    ]
    core_candidate = _candidate(
        profile_payload,
        version,
        evaluation_id="gate-02-test-only-thesis-core-candidate",
        report=_evaluation_report(
            [56, 63, 71, 84, 93, 87],
            difference_explanations=explanations,
            item_shifts={
                "T01": 0.25,
                "T02": -0.25,
                "T03": 0.5,
                "T04": -0.5,
                "T05": 0.25,
                "T06": -0.25,
            },
        ),
        plan_hash=_sha("9"),
        generated_at="2026-08-02T00:10:00+08:00",
    )
    parity = build_regression_record(
        candidate=core_candidate,
        approved_record=approved_baseline,
    )
    assert parity["regression_passed"] is True
    assert set(parity["per_criterion"]) == set(MAXIMA)
    assert any(
        comparison["bias_delta"] != 0
        for comparison in parity["per_criterion"].values()
    )
    assert {item["criterion_code"] for item in parity["difference_explanations"]} == set(MAXIMA)
    assert parity["metrics"]["invalid_evidence_rate"] == 0.0
    assert parity["metrics"]["qwk"] >= profile_payload[
        "acceptance_thresholds"
    ]["minimum_qwk"]

    candidate_run = client.post(
        "/api/release-gates/profiles/%s/runs" % profile["id"],
        json={"candidate_record": core_candidate},
    )
    assert candidate_run.status_code == 200, candidate_run.text
    approved_candidate = _approve(client, candidate_run.json()["id"])[
        "final_record"
    ]
    assert approved_candidate["gate_passed"] is True
    assert approved_candidate["difference_explanations"] == explanations

    for relationship in ("dataset", "model"):
        assert approved_candidate[relationship] == approved_baseline[relationship]
    assert approved_candidate["anchors"]["manifest_sha256"] == approved_baseline[
        "anchors"
    ]["manifest_sha256"]
    assert approved_candidate["rubric"] == approved_baseline["rubric"]
    assert approved_candidate["plan"]["sha256"] != approved_baseline["plan"][
        "sha256"
    ]

    with client.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ReleaseGateProfile)) == 1
        assert db.scalar(select(func.count()).select_from(ReleaseGateRun)) == 2
        assert db.scalar(select(func.count()).select_from(ReleaseGateApproval)) == 2

    # The fixture is evidence for test behavior only; it must never authorize
    # the production default switch.
    assert profile["model_identity"]["provider"] == "test_immutable"
    assert profile["dataset_identity"]["dataset_id"].startswith("gate-02-test-only")

    archive = json.loads(TEST_ONLY_ARCHIVE.read_text(encoding="utf-8"))
    assert archive["test_contract_passed"] is True
    assert archive["gating_eligible"] is False
    assert archive["gate_passed"] is False
    assert archive["production_default_switch_authorized"] is False
    assert archive["profile"]["rubric_criteria"] == MAXIMA
    assert archive["core_test_candidate"]["qwk"] == pytest.approx(
        parity["metrics"]["qwk"]
    )
    assert archive["core_test_candidate"]["invalid_evidence_rate"] == 0.0
