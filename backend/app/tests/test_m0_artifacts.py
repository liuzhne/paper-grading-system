"""M0 architecture, evaluation-baseline, and terminology freeze tests."""

import hashlib
import json
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
ADR_PATH = PROJECT_ROOT / "docs" / "adr" / "0001-general-scoring-core-foundations.md"
BASELINE_PATH = PROJECT_ROOT / "docs" / "baselines" / "m0-thesis-evaluation.json"
GOLDEN_DIR = Path(__file__).with_name("golden") / "m0"
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
HEX_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def test_m0_adr_accepts_every_planned_decision_without_placeholders():
    text = ADR_PATH.read_text(encoding="utf-8")

    assert "状态：Accepted" in text
    assert "本 ADR 不包含影响 M1/M2 实施路径的开放语义" in text
    assert re.findall(r"^### D(\d{2})\b", text, flags=re.MULTILINE) == [
        "%02d" % number for number in range(1, 14)
    ]
    assert not re.search(r"\b(?:TBD|TODO|FIXME)\b|待定|待补|尚未决定|\?\?\?", text, flags=re.I)

    frozen_contract_markers = {
        "dependency direction": "Core 只接收不可变合同和 ports",
        "weight modes": "weighted_normalized",
        "quote evidence": "source-quote-normalization-v1",
        "deterministic evidence": "DeterministicObservation",
        "absence coverage": "expected/checked 集合",
        "score authorization": "模型和 Checker 不能授权分值",
        "decision lifecycle": "RuleDecision.status",
        "rule lifecycle": "AtomicRule.status",
        "band model": "恰好包含一个",
        "review-only boundary": "其 auto score 始终为空",
        "occurrence identity": "occurrence-id-v1",
        "evidence unit identity": "evidence-unit-id-v1",
        "duplicate section identity": "section_ordinal",
        "canonical text span": "半开区间 `[start,end)`",
        "finding authorization": "allowed_finding_codes",
        "capped repeat": "最后一条可只分配剩余 cap",
        "mutex boundary": "多个非 band AtomicRule",
        "human resolution": "score.review.resolve_block",
        "document identity": "document-snapshot-v1",
        "rubric hash": "rubric-content-v1",
        "checker identity": "checker-package-sha256-v1",
        "profile separation": "business_profile_key",
        "second profile": "technical_proposal",
        "legacy migration": "legacy_unversioned",
        "engine boundary": "不得继续给 `backend/app/services/scoring/engine.py` 增加论文专用分支",
        "ui boundary": "Streamlit 泛化在本轮冻结",
    }
    missing = [label for label, marker in frozen_contract_markers.items() if marker not in text]
    assert not missing, "M0 ADR missing frozen contracts: %s" % ", ".join(missing)

    # The current implementation hashes columns dynamically.  M0 freezes the
    # complete pre-0013 allowlist so a future ORM column cannot alter v1.
    for graph_node in (
        "Rubric",
        "RubricCriterion",
        "RubricCompilation",
        "SourceArtifact",
        "SourceRule",
        "RubricVersion",
        "TemplateItem",
        "AtomicRule",
        "RuleLevel",
        "RuleTemplateLink",
    ):
        assert "| %s |" % graph_node in text


def test_m0_historical_evaluation_baseline_is_exact_and_non_gating():
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    assert baseline["schema"] == "paper-grading/evaluation-baseline@1"
    assert baseline["archive_record_version"] == "m0-a4-full98-metadata-v1"
    assert baseline["profile_key"] == "thesis"
    assert baseline["provenance"] == "historical_transcription"
    assert baseline["status"] == "historical_summary_only"
    assert baseline["reproducible"] is False
    assert baseline["gating_eligible"] is False

    evaluation = baseline["evaluation"]
    assert evaluation["reported_sample_count"] == 98
    assert evaluation["reported_errors"] == 0
    assert evaluation["reported_unmatched"] == 0
    metrics = evaluation["metrics"]
    assert metrics["qwk"] == {"value": -0.002, "reported_decimals": 3, "approximate": True}
    assert metrics["mae"] == {"value": 6.83, "reported_decimals": 2}
    assert metrics["rmse"] == {"value": 8.78, "reported_decimals": 2}
    assert metrics["exact_grade_agreement"]["value"] == 0.449
    assert metrics["adjacent_grade_agreement"]["value"] == 0.898
    assert metrics["criterion_bias"] == {
        "R01": 0.45,
        "R02": 1.5,
        "R03": 1.51,
        "R04": 1.3,
        "R05": 0.84,
    }

    dataset = baseline["dataset"]
    assert dataset["recorded_version"] is None
    assert dataset["identity_status"] == "not_recorded"
    assert dataset["raw_artifacts_retained"] is False
    assert dataset["dataset_manifest_sha256"] is None
    assert dataset["paper_manifest_sha256"] is None
    assert dataset["truth_sha256"] is None
    assert dataset["unavailable_reason"]
    assert baseline["rubric"]["rubric_version_hash"] is None
    assert baseline["runtime"]["model_artifact_sha256"] is None
    assert baseline["runtime"]["model_identity_status"] == "not_recorded"

    empty_anchor_hash = hashlib.sha256(b"[]").hexdigest()
    assert HEX_SHA256.fullmatch(baseline["anchors"]["canonical_empty_array_sha256"])
    assert baseline["anchors"]["canonical_empty_array_sha256"] == empty_anchor_hash
    assert baseline["anchors"]["run_snapshot_retained"] is False
    assert HEX_COMMIT.fullmatch(baseline["code"]["best_known_scoring_revision"])
    assert HEX_COMMIT.fullmatch(baseline["code"]["historical_record_revision"])
    assert baseline["source_evidence"] == [
        {
            "path": "执行进展.md",
            "lines": "91-105",
            "commit": "3fc3f6dbed8ca4616262e138847e470cae075996",
        }
    ]


def test_m0_terminology_and_reproducible_goldens_are_declared():
    adr = ADR_PATH.read_text(encoding="utf-8")
    glossary = adr.split("## 规范术语", 1)[1].split("## 已接受决策", 1)[0]
    for term in (
        "Submission",
        "SubmissionSnapshot",
        "DocumentSnapshot",
        "RubricVersion",
        "Criterion",
        "AtomicRule",
        "Profile",
        "business_profile_key",
        "workflow_profile",
        "ParsedPaper",
    ):
        assert "| %s |" % term in glossary

    legacy_docs = (
        PROJECT_ROOT / "论文打分系统设计方案.md",
        PROJECT_ROOT / "requirements-analysis.md",
        PROJECT_ROOT / "technical-development.md",
    )
    for path in legacy_docs:
        text = path.read_text(encoding="utf-8")
        assert "docs/adr/0001-general-scoring-core-foundations.md" in text
        assert "M0" in text[:1500]

    assert {path.name for path in GOLDEN_DIR.iterdir() if path.is_file()} == {
        "manual-review.json",
        "mock-scoring.json",
        "report.html",
        "run-export-v1.json",
    }
