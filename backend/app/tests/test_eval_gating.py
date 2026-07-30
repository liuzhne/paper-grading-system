import json
from copy import deepcopy

import pytest

from backend.app.eval.gating import APPROVAL_SCHEMA
from backend.app.eval.gating import GateValidationError
from backend.app.eval.gating import approved_regression_issues
from backend.app.eval.gating import build_gate_candidate
from backend.app.eval.gating import build_holdout_manifest_from_rows
from backend.app.eval.gating import build_regression_record
from backend.app.eval.gating import ensure_external_artifact_dir
from backend.app.eval.gating import evaluation_sha256
from backend.app.eval.gating import finalize_gate
from backend.app.eval.gating import summarize_run_identities


def _sha(character):
    return character * 64


def _report(provider="openai"):
    run_identity = {
        "rubric_version_id": "rubric-version-1",
        "rubric_version_hash": _sha("1"),
        "rubric_hash_scheme": "rubric-content-v1",
        "rubric_snapshot_hash": _sha("2"),
        "policy_hash": _sha("3"),
        "execution_plan_hash": _sha("4"),
        "plan_schema_version": "rule-execution-plan@2",
        "checker_manifest_sha256": _sha("5"),
        "business_profile_key": "thesis",
        "workflow_profile": "thesis",
        "model_provider": provider,
        "model_name": "fixed-model",
        "model_version": "2026-07-01",
        "engine_version": "scoring-core@1",
        "source_artifact_hash": _sha("6"),
        "normalized_content_hash": _sha("7"),
        "document_snapshot_hash": _sha("8"),
    }
    return {
        "n": 1,
        "dataset_size": 1,
        "completed_runs": 1,
        "errors": [],
        "unmatched_predictions": [],
        "qwk": 1.0,
        "mae": 1.0,
        "rmse": 1.0,
        "exact_grade_agreement": 1.0,
        "adjacent_grade_agreement": 1.0,
        "review_rate": 0.0,
        "blocked_rate": 0.0,
        "per_criterion": {"C01": {"n": 1, "mae": 1.0, "bias": 1.0}},
        "grade_confusion": [[1]],
        "grade_labels": ["优秀"],
        "per_sample": [
            {
                "sample_id": _sha("9"),
                "human_total": 90.0,
                "system_total": 91.0,
                "human_items": {"C01": 9.0},
                "system_items": {"C01": 10.0},
                "run_identity": run_identity,
            }
        ],
    }


def _candidate(provider="openai"):
    report = _report(provider)
    run_identity, issues = summarize_run_identities(report)
    return build_gate_candidate(
        evaluation_id="pgs-8-test",
        report=report,
        dataset_identity={
            "dataset_id": "teacher-holdout",
            "dataset_version": "2026-07-30-v1",
            "sample_count": 1,
            "dataset_manifest_sha256": _sha("a"),
            "sample_ids_sha256": evaluation_sha256([_sha("9")]),
            "paper_manifest_sha256": _sha("b"),
            "truth_sha256": _sha("c"),
        },
        run_identity=run_identity,
        run_identity_issues=issues,
        anchors_identity={
            "schema": "paper-grading/anchor-manifest@1",
            "count": 0,
            "manifest_sha256": _sha("d"),
            "holdout_exclusion_proven": False,
        },
        model_identity={
            "provider": provider,
            "model_name": "fixed-model",
            "model_version": "2026-07-01",
            "identity_method": "provider_immutable_revision",
            "immutable_revision": "fixed-model-2026-07-01",
            "artifact_identity_sha256": _sha("e"),
        },
        prompt_identity={
            "prompt_version": "2026-07-20-7",
            "prompt_source_manifest_sha256": _sha("f"),
        },
        repository_identity={
            "revision": "a" * 40,
            "branch": "codex/pgs-8",
            "dirty": False,
            "lockfile": "uv.lock",
            "lockfile_sha256": _sha("0"),
        },
        private_artifacts={
            "location": "external_private_not_in_repository",
            "private_manifest_sha256": _sha("1"),
            "private_report_sha256": _sha("2"),
        },
        artifact_directory_external=True,
        generated_at="2026-07-30T00:00:00+00:00",
    )


def _approval(candidate):
    metrics = deepcopy(candidate["evaluation"]["metrics"])
    return {
        "schema": APPROVAL_SCHEMA,
        "candidate_sha256": candidate["candidate_sha256"],
        "approved_by": "authorized-maintainer",
        "approved_at": "2026-07-30T12:00:00+08:00",
        "privacy_review": {
            "dataset_deidentified": True,
            "repository_scan_passed": True,
            "anchors_exclude_holdout": True,
            "teacher_truth_external_only": True,
        },
        "accepted_baseline": {
            "metrics": metrics,
            "acceptance_thresholds": {
                "minimum_qwk": 0.5,
                "maximum_mae": 2.0,
                "maximum_rmse": 2.0,
                "minimum_exact_grade_agreement": 0.8,
                "minimum_adjacent_grade_agreement": 0.9,
                "maximum_review_rate": 0.2,
                "maximum_blocked_rate": 0.0,
            },
            "regression_tolerances": {
                "qwk_drop": 0.02,
                "mae_rise": 1.0,
                "rmse_rise": 1.0,
                "exact_grade_agreement_drop": 0.05,
                "adjacent_grade_agreement_drop": 0.05,
                "review_rate_rise": 0.1,
            },
        },
    }


def test_private_holdout_manifest_is_stable_and_omits_original_filenames(tmp_path):
    papers = tmp_path / "private-papers"
    papers.mkdir()
    (papers / "张三.docx").write_bytes(b"paper-a")
    (papers / "李四.pdf").write_bytes(b"paper-b")
    rows = [
        {"filename": "张三.docx", "total": 90, "items": {"C01": 9}},
        {"filename": "李四.pdf", "total": 70, "items": {"C01": 7}},
    ]

    first, mapping = build_holdout_manifest_from_rows(
        papers_dir=papers,
        rows=rows,
        dataset_id="teacher-holdout",
        dataset_version="v1",
    )
    second, _ = build_holdout_manifest_from_rows(
        papers_dir=papers,
        rows=list(reversed(rows)),
        dataset_id="teacher-holdout",
        dataset_version="v1",
    )

    serialized = json.dumps(first, ensure_ascii=False)
    assert "张三" not in serialized
    assert "李四" not in serialized
    assert set(mapping) == {"张三.docx", "李四.pdf"}
    assert (
        first["public_identity"]["dataset_manifest_sha256"]
        == second["public_identity"]["dataset_manifest_sha256"]
    )
    assert first["truth"] == second["truth"]


def test_holdout_manifest_rejects_path_escape_and_duplicate_content(tmp_path):
    papers = tmp_path / "papers"
    papers.mkdir()
    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"outside")
    with pytest.raises(GateValidationError, match="escapes"):
        build_holdout_manifest_from_rows(
            papers_dir=papers,
            rows=[{"filename": "../outside.docx", "total": 80, "items": {}}],
            dataset_id="d",
            dataset_version="v1",
        )

    (papers / "a.docx").write_bytes(b"same")
    (papers / "b.docx").write_bytes(b"same")
    with pytest.raises(GateValidationError, match="duplicate paper content"):
        build_holdout_manifest_from_rows(
            papers_dir=papers,
            rows=[
                {"filename": "a.docx", "total": 80, "items": {}},
                {"filename": "b.docx", "total": 81, "items": {}},
            ],
            dataset_id="d",
            dataset_version="v1",
        )


def test_complete_candidate_is_eligible_but_waits_for_human_approval():
    candidate = _candidate()

    assert candidate["reproducible"] is True
    assert candidate["gating_eligible"] is True
    assert candidate["gate_passed"] is False
    assert candidate["status"] == "candidate_awaiting_approval"
    serialized = json.dumps(candidate)
    assert "human_total" not in serialized
    assert "per_sample" not in serialized


def test_mock_or_dirty_revision_can_never_be_gating_eligible():
    mock = _candidate("mock")
    assert mock["gating_eligible"] is False
    assert any("mock provider" in issue for issue in mock["missing_or_invalid"])

    report = _report()
    run_identity, issues = summarize_run_identities(report)
    dirty = build_gate_candidate(
        evaluation_id="dirty",
        report=report,
        dataset_identity=_candidate()["dataset"],
        run_identity=run_identity,
        run_identity_issues=issues,
        anchors_identity=_candidate()["anchors"],
        model_identity=_candidate()["model"],
        prompt_identity={
            "prompt_version": "v",
            "prompt_source_manifest_sha256": _sha("f"),
        },
        repository_identity={
            "revision": "a" * 40,
            "branch": "main",
            "dirty": True,
            "lockfile": "uv.lock",
            "lockfile_sha256": _sha("0"),
        },
        private_artifacts=_candidate()["private_artifacts"],
        artifact_directory_external=True,
    )
    assert dirty["gating_eligible"] is False
    assert "code worktree must be clean" in dirty["missing_or_invalid"]


def test_approval_is_content_bound_and_passes_only_after_privacy_and_thresholds():
    candidate = _candidate()
    approval = _approval(candidate)

    final = finalize_gate(candidate, approval)

    assert final["gate_passed"] is True
    assert final["status"] == "passed"
    assert final["anchors"]["holdout_exclusion_proven"] is True
    assert final["manual_confirmations_pending"] == []

    wrong = deepcopy(approval)
    wrong["candidate_sha256"] = _sha("0")
    with pytest.raises(GateValidationError, match="exact candidate"):
        finalize_gate(candidate, wrong)

    privacy_missing = deepcopy(approval)
    privacy_missing["privacy_review"]["repository_scan_passed"] = False
    with pytest.raises(GateValidationError, match="privacy review"):
        finalize_gate(candidate, privacy_missing)


def test_approved_threshold_failure_is_not_a_pass():
    candidate = _candidate()
    approval = _approval(candidate)
    approval["accepted_baseline"]["acceptance_thresholds"]["minimum_qwk"] = 1.1

    final = finalize_gate(candidate, approval)

    assert final["gate_passed"] is False
    assert final["status"] == "failed_thresholds"
    assert any("qwk" in issue for issue in final["missing_or_invalid"])


def test_approved_regression_uses_frozen_tolerances():
    candidate = _candidate()
    approved = finalize_gate(candidate, _approval(candidate))
    current = deepcopy(candidate["evaluation"]["metrics"])
    current["qwk"] = 0.97
    current["mae"] = 2.1

    issues = approved_regression_issues(current, approved)

    assert any("qwk regressed" in issue for issue in issues)
    assert any("mae regressed" in issue for issue in issues)


def test_candidate_regression_reuses_only_same_dataset_rubric_and_anchors():
    candidate = _candidate()
    approved = finalize_gate(candidate, _approval(candidate))

    passed = build_regression_record(
        candidate=candidate,
        approved_record=approved,
    )
    assert passed["regression_passed"] is True

    changed = deepcopy(candidate)
    changed["anchors"]["manifest_sha256"] = _sha("0")
    changed.pop("candidate_sha256")
    changed["candidate_sha256"] = evaluation_sha256(changed)
    failed = build_regression_record(
        candidate=changed,
        approved_record=approved,
    )
    assert failed["regression_passed"] is False
    assert any("anchors.manifest_sha256 changed" in issue for issue in failed["issues"])


def test_candidate_and_approved_record_hashes_are_revalidated():
    candidate = _candidate()
    approved = finalize_gate(candidate, _approval(candidate))

    tampered_candidate = deepcopy(candidate)
    tampered_candidate["evaluation"]["metrics"]["qwk"] = -1.0
    with pytest.raises(GateValidationError, match="candidate content"):
        build_regression_record(
            candidate=tampered_candidate,
            approved_record=approved,
        )

    tampered_approved = deepcopy(approved)
    tampered_approved["approval"]["approved_by"] = "someone-else"
    with pytest.raises(GateValidationError, match="approved reference content"):
        build_regression_record(
            candidate=candidate,
            approved_record=tampered_approved,
        )


def test_private_artifact_directory_must_be_outside_repository(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    with pytest.raises(GateValidationError, match="outside"):
        ensure_external_artifact_dir(repository / "private", repository)

    external = tmp_path / "external"
    assert ensure_external_artifact_dir(external, repository) == external.resolve()


def test_cli_finalizes_exact_candidate_without_rerunning_evaluation(tmp_path):
    from backend.app.scripts import run_qwk_eval

    candidate = _candidate()
    candidate_path = tmp_path / "gate_candidate.json"
    approval_path = tmp_path / "approval.json"
    public_path = tmp_path / "approved.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    approval_path.write_text(
        json.dumps(_approval(candidate)),
        encoding="utf-8",
    )

    exit_code = run_qwk_eval.main(
        [
            "--finalize-candidate",
            str(candidate_path),
            "--approval",
            str(approval_path),
            "--public-record-output",
            str(public_path),
        ]
    )

    assert exit_code == 0
    assert json.loads(public_path.read_text(encoding="utf-8"))["gate_passed"] is True
    assert (tmp_path / "gate_final.json").is_file()
