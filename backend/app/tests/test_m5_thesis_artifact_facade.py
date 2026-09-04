from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.policy import build_corrected_thesis_policy
from backend.app.services.scoring.profiles.thesis import THESIS_PROMPT_VERSION
from backend.app.services.scoring.profiles.thesis import ThesisProfile
from backend.app.eval.labeled_dataset import _run_identity_projection


def _item():
    criterion = SimpleNamespace(code="T01", name="研究方法")
    return SimpleNamespace(
        id="item-1",
        criterion=criterion,
        criterion_code="T01",
        max_score=Decimal("10"),
        ai_score=Decimal("8"),
        final_score=Decimal("9"),
        evidence_sufficient=True,
        reason="证据支持",
        deductions=["方法说明略短"],
        deduction_items=[],
        evidence=[{"quote": "研究方法包括访谈。", "location": "研究方法"}],
        band_selection=None,
        sub_results=None,
        suggestion="补充样本说明",
        confidence=Decimal("0.8"),
        need_manual_review=False,
        aggregation={
            "schema_version": "criterion-aggregation@2",
            "criterion_code": "T01",
            "status": "calculated",
            "contributions": [],
        },
        aggregation_schema_version="criterion-aggregation@2",
        auto_score_status="calculated",
        rule_results=[{"rule_code": "t01.method.v1", "status": "triggered"}],
        rule_results_schema_version="rule-results@1",
    )


def _runtime_identity():
    return {
        "engine_contract_version": "scoring-core@1",
        "engine_version": "thesis-core-adapter@1",
        "profile_key": "thesis",
        "profile_version": "thesis-legacy-profile@1",
        "prompt_version": THESIS_PROMPT_VERSION,
        "provider": {
            "name": "mock",
            "model": "mock-criterion-scorer",
            "model_version": "v1",
            "sampling": {
                "temperature": "0",
                "top_p": "1",
                "seed": 0,
                "max_tokens": 512,
            },
            "thinking": {"enabled": False, "type": None},
            "response_format": "json_schema",
            "response_schema": "atomic-rule-decisions@1",
            "artifact_hash": "a" * 64,
        },
        "calibration_anchors_hash": "b" * 64,
    }


def _run():
    policy = build_corrected_thesis_policy(10, "points").to_mapping()
    paper = SimpleNamespace(
        id="paper-1",
        title="论文标题",
        student_id="S001",
        student_name="学生",
        department="计算机学院",
        major="软件工程",
    )
    rubric = SimpleNamespace(id="rubric-1", name="论文评分", version="v1")
    return SimpleNamespace(
        id="run-1",
        paper=paper,
        rubric=rubric,
        items=[_item()],
        ai_total_score=Decimal("8"),
        final_total_score=Decimal("9"),
        grade="良好",
        need_manual_review=False,
        status="reviewed",
        finished_at=datetime(2026, 8, 2, 12, 0, 0),
        coherence_findings=[{"kind": "coherence", "message": "一致"}],
        format_findings=[{"field": "font", "message": "符合"}],
        policy_snapshot=policy,
        policy_hash=policy["policy_hash"],
        rubric_version_id="version-1",
        rubric_version_hash="c" * 64,
        rubric_hash_scheme="rubric-content-v2",
        rubric_snapshot_hash="d" * 64,
        execution_plan_hash="e" * 64,
        plan_schema_version="rule-execution-plan@2",
        checker_manifest={"thesis.structure.required_sections.v1": {"v": "1"}},
        business_profile_key="thesis",
        business_profile_version="thesis-legacy-profile@1",
        workflow_profile="template_driven",
        model_provider="mock",
        model_name="mock-criterion-scorer",
        model_version="v1",
        engine_version="thesis-core-adapter@1",
        prompt_version=THESIS_PROMPT_VERSION,
        runtime_identity=_runtime_identity(),
        source_artifact_hash="f" * 64,
        normalized_content_hash="1" * 64,
        document_snapshot_hash="2" * 64,
    )


def _review_logs():
    return [
        SimpleNamespace(
            score_item_id="item-1",
            reviewer_id="teacher-1",
            before_score=Decimal("8"),
            after_score=Decimal("9"),
            reason="教师调整",
            policy_hash=_run().policy_hash,
            resolution_type="ordinary_override",
            created_at=datetime(2026, 8, 2, 12, 1, 0),
        ),
        SimpleNamespace(
            score_item_id=None,
            reviewer_id="teacher-2",
            before_score=Decimal("8"),
            after_score=Decimal("9"),
            reason="复核完成",
            policy_hash=_run().policy_hash,
            resolution_type="ordinary_override",
            created_at=datetime(2026, 8, 2, 12, 2, 0),
        ),
    ]


def test_thesis_artifact_facade_owns_report_export_and_spreadsheet_projection():
    profile = ThesisProfile()
    run = _run()
    logs = _review_logs()

    artifact = profile.build_artifact_projection(
        run=run,
        review_logs=logs,
        section_summaries=[{"title": "绪论", "paragraphs": 2, "chars": 120}],
    )

    assert artifact["schema_version"] == "thesis-artifact-projection@1"
    assert artifact["profile_key"] == "thesis"
    assert artifact["paper"] == {
        "id": "paper-1",
        "title": "论文标题",
        "student_id": "S001",
        "student_name": "学生",
        "department": "计算机学院",
        "major": "软件工程",
    }
    assert artifact["rubric"] == {
        "id": "rubric-1",
        "name": "论文评分",
        "version": "v1",
    }
    assert artifact["items"][0]["criterion_name"] == "研究方法"
    assert artifact["review_logs"][-1]["reviewer_id"] == "teacher-2"
    assert artifact["coherence_findings"] == run.coherence_findings
    assert artifact["format_findings"] == run.format_findings

    sheet = profile.build_spreadsheet_projection(
        batch=SimpleNamespace(
            name="批次",
            department="默认学院",
            major="默认专业",
        ),
        run=run,
        review_logs=logs,
    )
    assert sheet["summary"]["reviewer"] == "teacher-2"
    assert sheet["summary"]["review_notes"] == "复核完成"
    assert sheet["summary"]["main_deductions"] == "方法说明略短"
    assert sheet["details"][0]["evidence_quotes"] == "研究方法包括访谈。"

    artifact["paper"]["title"] = "tampered"
    assert run.paper.title == "论文标题"


def test_thesis_eval_identity_binds_profile_policy_grade_and_runtime():
    profile = ThesisProfile()
    run = _run()

    identity = profile.build_eval_run_identity(run=run)

    assert identity["schema_version"] == "paper-grading/thesis-eval-run-identity@1"
    assert identity["business_profile_key"] == "thesis"
    assert identity["business_profile_version"] == profile.profile_version
    assert identity["prompt_version"] == profile.prompt_version
    assert identity["policy_hash"] == run.policy_hash
    assert identity["policy_snapshot_sha256"] == canonical_sha256(run.policy_snapshot)
    assert identity["grade_scale_sha256"] == canonical_sha256(
        run.policy_snapshot["grade_scale"]
    )
    assert identity["runtime_identity_sha256"] == canonical_sha256(
        run.runtime_identity
    )

    mismatched = _run()
    mismatched.runtime_identity = deepcopy(mismatched.runtime_identity)
    mismatched.runtime_identity["profile_version"] = "other-profile@1"
    with pytest.raises(ValueError, match="runtime.*profile"):
        profile.build_eval_run_identity(run=mismatched)


def test_labeled_dataset_uses_thesis_profile_eval_identity():
    run = _run()

    identity = _run_identity_projection(run, require_frozen=True)

    assert identity == ThesisProfile().build_eval_run_identity(run=run)


@pytest.mark.parametrize("status", ["invalid", "blocked"])
def test_thesis_review_facade_keeps_unresolved_blockers_fail_closed(status):
    profile = ThesisProfile()
    run = _run()
    item = run.items[0]
    item.auto_score_status = status
    item.ai_score = None
    item.final_score = None
    run.final_total_score = None

    with pytest.raises(ValueError, match="ordinary override"):
        profile.assert_ordinary_item_override_allowed(item=item)
    with pytest.raises(ValueError, match="invalid or blocked"):
        profile.assert_review_submission_allowed(run=run)
