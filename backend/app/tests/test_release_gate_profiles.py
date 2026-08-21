from copy import deepcopy
from uuid import uuid4

from sqlalchemy import select

from backend.app.db.models import Rubric
from backend.app.db.models import RubricCompilation
from backend.app.db.models import ReleaseGateApproval
from backend.app.db.models import ReleaseGateProfile
from backend.app.db.models import ReleaseGateRun
from backend.app.db.models import RubricVersion
from backend.app.eval.gating import GATE_RECORD_SCHEMA
from backend.app.eval.gating import evaluation_sha256
from backend.app.tests.conftest import publish_rubric_via_api


def _sha(character):
    return character * 64


def _levels(max_score):
    return [
        {
            "level_code": "EXCELLENT",
            "points": max_score,
            "descriptor": "完整满足评分依据，证据充分且结论可复核。",
        },
        {
            "level_code": "GOOD",
            "points": max_score * 0.8,
            "descriptor": "主要满足评分依据，存在少量不影响结论的不足。",
        },
        {
            "level_code": "PASS",
            "points": max_score * 0.6,
            "descriptor": "基本满足评分依据，但证据或论证存在明显缺口。",
        },
        {
            "level_code": "WEAK",
            "points": max_score * 0.4,
            "descriptor": "仅部分满足评分依据，关键证据不足。",
        },
        {
            "level_code": "NO_EVIDENCE",
            "points": 0,
            "descriptor": "未找到可支持该评分项的有效证据。",
        },
    ]


def _test_rubric_payload(name="PGS-8 数据库门禁测试标准"):
    maxima = (20, 20, 20, 10, 10, 20)
    descriptions = (
        "选题具有软件工程价值，计划合理，并完成问题分析、定义与建模。",
        "能够完成需求分析、方案论证与复杂软件工程问题求解。",
        "能够检索信息并使用合适的软件工程技术和工具实现方案。",
        "能够综合运用建模、开发与测试工具并提出优化改进。",
        "能够论证系统维护、可用性、安全性与可持续性。",
        "工作量和难度适当，按期履责并保持严谨的学习工作态度。",
    )
    return {
        "name": name,
        "version": "test-v1",
        "total_score": 100,
        "criteria": [
            {
                "code": f"T{index:02d}",
                "name": f"测试评分项 T{index:02d}",
                "max_score": maximum,
                "description": description,
                "criterion_type": "llm_judgment",
                "scoring_mode": "banded",
                "rubric_levels": _levels(maximum),
                "display_order": index - 1,
            }
            for index, (maximum, description) in enumerate(
                zip(maxima, descriptions), start=1
            )
        ],
    }


def _published_test_rubric(client):
    created = client.post("/api/rubrics", json=_test_rubric_payload())
    assert created.status_code == 200, created.text
    rubric_id = created.json()["id"]
    _published, identity = publish_rubric_via_api(client, rubric_id)
    with client.session_factory() as db:
        version = db.get(RubricVersion, identity["rubric_version_id"])
        assert version is not None
        return rubric_id, version.id, version.version_hash, version.hash_scheme


def _profile_payload(rubric_id, version_id):
    return {
        "gate_key": "GATE-01",
        "name": "PGS-8 test gate profile",
        "rubric_id": rubric_id,
        "rubric_version_id": version_id,
        "dataset_identity": {
            "dataset_id": "synthetic-teacher-holdout",
            "dataset_version": "test-v1",
            "sample_count": 2,
            "dataset_manifest_sha256": _sha("1"),
            "sample_ids_sha256": _sha("2"),
            "paper_manifest_sha256": _sha("3"),
            "truth_sha256": _sha("4"),
        },
        "model_identity": {
            "provider": "test_immutable",
            "model_name": "rubric-gate-fixture",
            "model_version": "2026-08-02",
            "identity_method": "provider_immutable_revision",
            "immutable_revision": "rubric-gate-fixture@2026-08-02",
            "artifact_identity_sha256": _sha("5"),
        },
        "anchors_identity": {
            "schema": "paper-grading/anchor-manifest@1",
            "count": 0,
            "manifest_sha256": _sha("6"),
            "holdout_exclusion_proven": True,
        },
        "acceptance_thresholds": {
            "minimum_qwk": 0.5,
            "maximum_mae": 5.0,
            "maximum_rmse": 6.0,
            "minimum_exact_grade_agreement": 0.5,
            "minimum_adjacent_grade_agreement": 0.9,
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


def _candidate(profile, version_hash, hash_scheme):
    metrics = {
        "qwk": 0.8,
        "mae": 2.0,
        "rmse": 3.0,
        "exact_grade_agreement": 0.6,
        "adjacent_grade_agreement": 1.0,
        "review_rate": 0.1,
        "blocked_rate": 0.0,
        "invalid_evidence_rate": 0.0,
    }
    record = {
        "schema": GATE_RECORD_SCHEMA,
        "evaluation_id": "pgs-8-db-profile-test",
        "status": "candidate_awaiting_approval",
        "reproducible": True,
        "gating_eligible": True,
        "gate_passed": False,
        "missing_or_invalid": [],
        "profile_key": "thesis",
        "evaluation": {
            "sample_count": 2,
            "dataset_size": 2,
            "completed_runs": 2,
            "error_count": 0,
            "metrics": metrics,
            "per_criterion": {},
            "grade_confusion": [[2]],
            "grade_labels": ["合格"],
        },
        "dataset": deepcopy(profile["dataset_identity"]),
        "rubric": {
            "version_id": profile["rubric_version_id"],
            "version_hash": version_hash,
            "hash_scheme": hash_scheme,
            "snapshot_sha256": _sha("7"),
        },
        "anchors": {
            **deepcopy(profile["anchors_identity"]),
            "holdout_exclusion_proven": False,
        },
        "model": deepcopy(profile["model_identity"]),
        "approval": None,
        "manual_confirmations_pending": [
            "privacy_review",
            "anchor_holdout_exclusion",
            "baseline_and_regression_threshold_approval",
        ],
    }
    record["candidate_sha256"] = evaluation_sha256(record)
    return record


def test_users_create_db_backed_profile_run_and_exact_approval(client):
    rubric_id, version_id, version_hash, hash_scheme = _published_test_rubric(
        client
    )
    payload = _profile_payload(rubric_id, version_id)

    created = client.post("/api/release-gates/profiles", json=payload)
    assert created.status_code == 200, created.text
    profile = created.json()
    assert profile["status"] == "active"
    assert len(profile["profile_hash"]) == 64
    assert profile["rubric_version_id"] == version_id

    candidate = _candidate(profile, version_hash, hash_scheme)
    registered = client.post(
        f"/api/release-gates/profiles/{profile['id']}/runs",
        json={"candidate_record": candidate},
    )
    assert registered.status_code == 200, registered.text
    run = registered.json()
    assert run["candidate_sha256"] == candidate["candidate_sha256"]
    assert run["status"] == "candidate_awaiting_approval"

    approved = client.post(
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
    assert approved.status_code == 200, approved.text
    final = approved.json()
    assert final["status"] == "passed"
    assert final["final_record"]["gate_passed"] is True
    assert final["final_record"]["approval"]["approved_by"]

    duplicate = client.post(
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
    assert duplicate.status_code == 409

    with client.session_factory() as db:
        assert db.scalar(select(ReleaseGateProfile)).id == profile["id"]
        assert db.scalar(select(ReleaseGateRun)).id == run["id"]
        approval = db.scalar(select(ReleaseGateApproval))
        assert approval.run_id == run["id"]
        assert approval.candidate_sha256 == candidate["candidate_sha256"]


def test_profile_and_candidate_relationships_fail_closed(client):
    rubric_id, version_id, version_hash, hash_scheme = _published_test_rubric(
        client
    )
    payload = _profile_payload(rubric_id, version_id)

    mock_payload = deepcopy(payload)
    mock_payload["name"] = "mock model must fail"
    mock_payload["model_identity"]["provider"] = "mock"
    rejected_mock = client.post(
        "/api/release-gates/profiles", json=mock_payload
    )
    assert rejected_mock.status_code == 400

    local_payload = deepcopy(payload)
    local_payload["name"] = "local immutable artifact profile"
    local_payload["model_identity"] = {
        "provider": "local",
        "model_name": "local-gate-model",
        "model_version": "2026-08-02",
        "identity_method": "artifact_sha256",
        "immutable_revision": None,
        "artifact_identity_sha256": _sha("8"),
    }
    accepted_local = client.post(
        "/api/release-gates/profiles", json=local_payload
    )
    assert accepted_local.status_code == 200, accepted_local.text

    draft = client.post(
        "/api/rubrics",
        json=_test_rubric_payload("PGS-8 unpublished profile fixture"),
    )
    assert draft.status_code == 200, draft.text
    with client.session_factory() as db:
        draft_version = db.scalar(
            select(RubricVersion).where(
                RubricVersion.rubric_id == draft.json()["id"]
            )
        )
        assert draft_version is not None
        draft_payload = _profile_payload(draft.json()["id"], draft_version.id)
        draft_payload["name"] = "unpublished rubric must fail"
    rejected_draft = client.post(
        "/api/release-gates/profiles", json=draft_payload
    )
    assert rejected_draft.status_code == 400

    created = client.post("/api/release-gates/profiles", json=payload)
    assert created.status_code == 200, created.text
    profile = created.json()
    candidate = _candidate(profile, version_hash, hash_scheme)
    candidate["dataset"]["dataset_version"] = "tampered"
    candidate.pop("candidate_sha256")
    candidate["candidate_sha256"] = evaluation_sha256(candidate)
    rejected_candidate = client.post(
        f"/api/release-gates/profiles/{profile['id']}/runs",
        json={"candidate_record": candidate},
    )
    assert rejected_candidate.status_code == 400
    assert "dataset.dataset_version" in rejected_candidate.text


def test_profile_content_hash_detects_database_tampering(client):
    rubric_id, version_id, version_hash, hash_scheme = _published_test_rubric(
        client
    )
    payload = _profile_payload(rubric_id, version_id)
    payload["name"] = "tamper-evident profile"
    created = client.post("/api/release-gates/profiles", json=payload)
    assert created.status_code == 200, created.text
    profile = created.json()
    candidate = _candidate(profile, version_hash, hash_scheme)

    with client.session_factory() as db:
        stored = db.get(ReleaseGateProfile, profile["id"])
        stored.dataset_identity = {
            **stored.dataset_identity,
            "dataset_version": "database-tampered",
        }
        db.commit()

    rejected = client.post(
        f"/api/release-gates/profiles/{profile['id']}/runs",
        json={"candidate_record": candidate},
    )

    assert rejected.status_code == 400
    assert "profile content does not match profile_hash" in rejected.text


def test_profile_requires_exactly_one_published_rubric_version(client):
    rubric_id, version_id, _version_hash, _hash_scheme = (
        _published_test_rubric(client)
    )
    with client.session_factory() as db:
        rubric = db.get(Rubric, rubric_id)
        original = db.get(RubricVersion, version_id)
        assert rubric is not None
        assert original is not None
        shadow_hash = _sha("9")
        shadow_compilation_id = str(uuid4())
        # Published graphs are ORM-protected, so use Core inserts to emulate a
        # legacy/manual database corruption that the gate must still reject.
        db.connection().execute(
            RubricCompilation.__table__.insert().values(
                id=shadow_compilation_id,
                rubric_id=rubric_id,
                status="validated",
                parser_version="test-parser",
                compiler_version="atomic-v1",
                model_provider=None,
                model_name=None,
                sampling_params={},
                prompt_version="test-prompt",
                raw_parse_output={},
                raw_model_output={},
                validation_result={},
                blockers=[],
                warnings=[],
                human_changes=[],
                created_by=original.created_by,
                reviewed_by=original.created_by,
                reviewed_at=rubric.published_at,
                published_at=rubric.published_at,
                final_version_hash=shadow_hash,
            )
        )
        db.connection().execute(
            RubricVersion.__table__.insert().values(
                id=str(uuid4()),
                rubric_id=rubric_id,
                compilation_id=shadow_compilation_id,
                version="test-shadow-v2",
                workflow_profile=original.workflow_profile,
                global_policy={},
                version_hash=shadow_hash,
                business_profile_key=original.business_profile_key,
                hash_scheme=original.hash_scheme,
                created_by=original.created_by,
            )
        )
        db.commit()

    payload = _profile_payload(rubric_id, version_id)
    payload["name"] = "ambiguous published rubric must fail"
    rejected = client.post("/api/release-gates/profiles", json=payload)

    assert rejected.status_code == 400
    assert "exactly one published immutable RubricVersion" in rejected.text


def test_release_gate_audit_relationships_do_not_cascade_delete():
    assert "delete" not in ReleaseGateProfile.runs.property.cascade
    assert "delete" not in ReleaseGateRun.approval.property.cascade
