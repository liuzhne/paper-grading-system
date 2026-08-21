"""M5 explicit Thesis Core parity, compatibility and replay golden."""

import json
from pathlib import Path

from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.db import models
from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.tests.conftest import create_legacy_unversioned_rubric_fixture
from backend.app.tests.conftest import make_sample_docx
from backend.app.tests.test_m0_characterization import M0_RUBRIC


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GOLDEN = Path(__file__).with_name("golden") / "m5" / "thesis-core-parity.json"


def _upload(client, batch_id, name, content):
    response = client.post(
        "/api/papers/upload",
        data={"batch_id": batch_id},
        files={"file": (name, content, DOCX_MIME)},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _items(client, run_id):
    response = client.get("/api/scoring-runs/%s/items" % run_id)
    assert response.status_code == 200, response.text
    return response.json()


def _scores(items):
    return {
        item["criterion_code"]: {
            "max_score": item["max_score"],
            "ai_score": item["ai_score"],
            "final_score": item["final_score"],
            "auto_score_status": item.get("auto_score_status"),
        }
        for item in items
    }


def _sha256(value):
    return isinstance(value, str) and len(value) == 64 and set(value) <= set(
        "0123456789abcdef"
    )


def test_explicit_thesis_core_parity_and_replay_match_frozen_golden(
    client,
    monkeypatch,
):
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")
    content = make_sample_docx().getvalue()
    rubric_id = create_legacy_unversioned_rubric_fixture(client, M0_RUBRIC)
    batch = client.post(
        "/api/batches",
        json={"name": "M5 parity golden", "rubric_id": rubric_id},
    )
    assert batch.status_code == 200, batch.text
    batch_id = batch.json()["id"]
    legacy_paper_id = _upload(client, batch_id, "legacy.docx", content)
    core_paper_id = _upload(client, batch_id, "core.docx", content)

    monkeypatch.setattr(settings, "SCORING_ENGINE_MODE", "legacy")
    legacy_response = client.post("/api/papers/%s/score" % legacy_paper_id)
    assert legacy_response.status_code == 200, legacy_response.text
    legacy_run = legacy_response.json()
    legacy_items = _items(client, legacy_run["id"])

    monkeypatch.setattr(settings, "SCORING_ENGINE_MODE", "core")
    core_response = client.post("/api/papers/%s/score" % core_paper_id)
    assert core_response.status_code == 200, core_response.text
    core_run = core_response.json()
    core_items = _items(client, core_run["id"])

    export = client.get("/api/scoring-runs/%s/export.json" % core_run["id"])
    report = client.get("/api/scoring-runs/%s/report" % core_run["id"])
    assert export.status_code == report.status_code == 200
    assert export.json()["schema"] == "paper-grading/run-export@1"
    assert "毕业论文智能评分报告" in report.text

    first_item = core_items[0]
    override = client.patch(
        "/api/score-items/%s" % first_item["id"],
        json={
            "final_score": first_item["final_score"],
            "reason": "M5 parity golden review",
        },
    )
    assert override.status_code == 200, override.text
    reviewed = client.post(
        "/api/scoring-runs/%s/review" % core_run["id"],
        json={"reason": "M5 parity golden accepted"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "reviewed"

    retry = client.post("/api/scoring-runs/%s/retry" % core_run["id"])
    assert retry.status_code == 200, retry.text
    retry_run = retry.json()
    assert retry_run["id"] != core_run["id"]

    with client.session_factory() as db:
        first = db.get(models.ScoringRun, core_run["id"])
        second = db.get(models.ScoringRun, retry_run["id"])
        official = db.scalars(
            select(models.ScoringRun).where(
                models.ScoringRun.paper_id == core_paper_id
            )
        ).all()
        assert len(official) == 2
        frozen_fields = (
            "rubric_source_kind",
            "rubric_snapshot_hash",
            "rubric_version_id",
            "rubric_version_hash",
            "rubric_hash_scheme",
            "business_profile_key",
            "business_profile_version",
            "prompt_version",
            "runtime_identity",
            "workflow_profile",
            "execution_plan_hash",
            "plan_schema_version",
            "checker_manifest",
            "policy_hash",
            "source_artifact_hash",
            "normalized_content_hash",
            "document_snapshot_hash",
            "document_schema_version",
            "engine_version",
        )
        assert {
            field: getattr(first, field) for field in frozen_fields
        } == {
            field: getattr(second, field) for field in frozen_fields
        }
        assert (first.rescore_generation, second.rescore_generation) == (0, 1)
        assert first.idempotency_key != second.idempotency_key
        plan = first.execution_plan_snapshot
        node_kind_by_criterion = {
            node["criterion_code"]: node["node_kind"]
            for node in plan["nodes"]
        }
        assert node_kind_by_criterion["DET-REF"] == "legacy_direct_criterion"
        assert node_kind_by_criterion["DED-METHOD"] == "legacy_direct_criterion"
        assert node_kind_by_criterion["BAND-INNOVATION"] == "atomic_rule"
        assert node_kind_by_criterion["HYB-COMPLIANCE"] == "composite_criterion"
        hashes = {
            "rubric_snapshot_hash": first.rubric_snapshot_hash,
            "policy_hash": first.policy_hash,
            "execution_plan_hash": first.execution_plan_hash,
            "runtime_identity_sha256": canonical_sha256(first.runtime_identity),
        }

    legacy_scores = _scores(legacy_items)
    core_scores = _scores(core_items)
    assert set(legacy_scores) == set(core_scores)
    assert {
        code: value["max_score"] for code, value in legacy_scores.items()
    } == {
        code: value["max_score"] for code, value in core_scores.items()
    }
    assert core_scores["DET-REF"]["final_score"] == legacy_scores["DET-REF"][
        "final_score"
    ]
    assert core_scores["DED-METHOD"]["final_score"] == legacy_scores[
        "DED-METHOD"
    ]["final_score"]
    differences = [
        {
            "criterion_code": code,
            "legacy_final": legacy_scores[code]["final_score"],
            "core_final": core_scores[code]["final_score"],
        }
        for code in sorted(core_scores)
        if legacy_scores[code]["final_score"] != core_scores[code]["final_score"]
    ]
    projection = {
        "schema_version": "thesis-core-parity-golden@1",
        "compatibility": {
            "public_export_schema": export.json()["schema"],
            "report_heading_present": True,
            "criterion_codes": sorted(core_scores),
            "core_persisted_item_order": [
                item["criterion_code"] for item in core_items
            ],
            "review_and_retry_supported": True,
            "history_generations": [0, 1],
        },
        "candidate_version": {
            "profile_key": first.business_profile_key,
            "profile_version": first.business_profile_version,
            "prompt_version": first.prompt_version,
            "engine_version": first.engine_version,
            "plan_schema_version": first.plan_schema_version,
            "node_kinds": [node["node_kind"] for node in plan["nodes"]],
            "node_signatures": [
                {
                    "criterion_code": node["criterion_code"],
                    "rule_code": node["rule_code"],
                    "node_kind": node["node_kind"],
                }
                for node in plan["nodes"]
            ],
            "hashes": hashes,
            "all_hashes_are_sha256": all(_sha256(value) for value in hashes.values()),
        },
        "parity": {
            "legacy_total": legacy_run["final_total_score"],
            "core_total": core_run["final_total_score"],
            "legacy_grade": legacy_run["grade"],
            "core_grade": core_run["grade"],
            "criterion_differences": differences,
            "difference_explanations": [
                "CORE_CORRECTED_POLICY_AND_GRADE_SCALE",
                "CORE_AUTHORIZED_EVIDENCE_AND_EFFECTS",
                "LEGACY_DIRECT_AND_COMPOSITE_COMPATIBILITY_NODES",
            ],
        },
        "replay": {
            "frozen_identity_equal_across_generations": True,
            "generation_changes_idempotency_only": True,
        },
    }

    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert projection == expected
