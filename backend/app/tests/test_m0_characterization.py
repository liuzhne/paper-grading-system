"""M0 legacy scoring characterization and golden compatibility tests.

These tests intentionally freeze the observable legacy thesis workflow before the
general scoring core is introduced.  Dynamic UUIDs and timestamps are redacted,
while scores, evidence, deductions, review state, report content, and the
``run-export@1`` schema remain part of the golden contract.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from backend.app.core.config import settings
from backend.app.tests.conftest import create_legacy_unversioned_rubric_fixture
from backend.app.tests.conftest import make_sample_docx


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GOLDEN_DIR = Path(__file__).with_name("golden") / "m0"
M0_DEV_USER_ID = "00000000-0000-0000-0000-000000000001"
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I)
TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\b"
)


M0_RUBRIC = {
    "name": "M0 论文评分兼容基线",
    "version": "legacy-v1",
    "description": "冻结 deterministic、deductive、banded、hybrid 四条旧引擎路径。",
    "total_score": 75,
    "criteria": [
        {
            "code": "DET-REF",
            "name": "参考文献规范",
            "max_score": 15,
            "criterion_type": "deterministic",
            "evidence_hints": ["参考文献", "引用"],
            "display_order": 1,
        },
        {
            "code": "DED-METHOD",
            "name": "研究方法",
            "max_score": 20,
            "scoring_mode": "deductive",
            "evidence_hints": ["研究方法", "实验设计", "数据来源"],
            "deduction_rules": ["方法说明不足时扣分"],
            "display_order": 2,
        },
        {
            "code": "BAND-INNOVATION",
            "name": "创新性",
            "max_score": 20,
            "scoring_mode": "banded",
            "evidence_hints": ["创新", "贡献", "改进"],
            "rubric_levels": [
                {"label": "优秀", "points": 20},
                {"label": "良好", "points": 15},
                {"label": "中等", "points": 10},
                {"label": "不及格", "points": 5},
            ],
            "display_order": 3,
        },
        {
            "code": "HYB-COMPLIANCE",
            "name": "研究规范综合检查",
            "max_score": 20,
            "criterion_type": "hybrid",
            "evidence_hints": ["研究方法", "实验设计"],
            "sub_checks": [
                {"kind": "deterministic", "name": "正文字数", "max_points": 8},
                {"kind": "llm_judgment", "name": "研究方法论证", "max_points": 12},
            ],
            "display_order": 4,
        },
    ],
}


@pytest.fixture()
def m0_scored_run(client, monkeypatch):
    """Create one representative paper run with every legacy scoring route."""

    # Pin every scoring switch that can change the Mock output.  A developer's
    # local .env must not silently rewrite the M0 compatibility baseline.
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "DEFAULT_DEV_USER_ID", M0_DEV_USER_ID)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")
    monkeypatch.setattr(settings, "COHERENCE_SEMANTIC_ENABLED", False)
    monkeypatch.setattr(settings, "SCORING_CHUNK_EVAL_TOP_K", 3)
    monkeypatch.setattr(settings, "SCORING_LLM_DIRECT_SINGLE_CALL", False)
    monkeypatch.setattr(settings, "SCORING_INSUFFICIENT_EVIDENCE_CAP_RATIO", 0.6)
    monkeypatch.setattr(settings, "SCORING_CONFIDENCE_REVIEW_THRESHOLD", 0.72)
    monkeypatch.setattr(settings, "SCORING_FULL_SCORE_REVIEW_RATIO", 0.95)
    monkeypatch.setattr(settings, "SCORING_HIGH_CONFIDENCE", 0.85)

    rubric_id = create_legacy_unversioned_rubric_fixture(client, M0_RUBRIC)

    batch_response = client.post(
        "/api/batches",
        json={
            "name": "M0 兼容性批次",
            "rubric_id": rubric_id,
            "department": "计算机学院",
            "major": "软件工程",
        },
    )
    assert batch_response.status_code == 200, batch_response.text
    batch_id = batch_response.json()["id"]

    document = make_sample_docx()
    upload_response = client.post(
        "/api/papers/upload",
        data={"batch_id": batch_id},
        files={"file": ("m0-representative.docx", document.getvalue(), DOCX_MIME)},
    )
    assert upload_response.status_code == 200, upload_response.text
    paper_id = upload_response.json()["id"]

    metadata_response = client.patch(
        "/api/papers/%s" % paper_id,
        json={
            "student_id": "M0001",
            "student_name": "M0 测试学生",
            "title": "M0 代表性毕业论文",
            "department": "计算机学院",
            "major": "软件工程",
            "advisor": "测试导师",
        },
    )
    assert metadata_response.status_code == 200, metadata_response.text

    run_response = client.post("/api/papers/%s/score" % paper_id)
    assert run_response.status_code == 200, run_response.text
    run = run_response.json()

    items_response = client.get("/api/scoring-runs/%s/items" % run["id"])
    assert items_response.status_code == 200, items_response.text
    items = items_response.json()
    assert [item["criterion_code"] for item in items] == [
        "DET-REF",
        "DED-METHOD",
        "BAND-INNOVATION",
        "HYB-COMPLIANCE",
    ]
    return {
        "batch_id": batch_id,
        "paper_id": paper_id,
        "rubric_id": rubric_id,
        "run": run,
        "items": items,
    }


def test_m0_scoring_chain_characterizes_all_legacy_routes(client, m0_scored_run):
    """Freeze the four routes as one persisted, API-visible scoring run."""

    run = m0_scored_run["run"]
    items = {item["criterion_code"]: item for item in m0_scored_run["items"]}

    deterministic = items["DET-REF"]
    assert deterministic["confidence"] == 1.0
    assert deterministic["evidence_sufficient"] is True
    assert deterministic["band_selection"] is None
    assert deterministic["sub_results"] is None
    assert "编号制引文-参考文献双向核对" in deterministic["reason"]

    deductive = items["DED-METHOD"]
    assert deductive["deduction_items"]
    assert all(item["points"] is not None for item in deductive["deduction_items"])
    assert deductive["final_score"] == pytest.approx(
        deductive["max_score"] - sum(item["points"] for item in deductive["deduction_items"])
    )
    assert "按扣分制核算" in deductive["reason"]

    banded = items["BAND-INNOVATION"]
    assert banded["band_selection"]["level"] in {"优秀", "良好", "中等", "不及格"}
    assert banded["final_score"] == banded["band_selection"]["awarded"]
    assert banded["final_score"] in {20.0, 15.0, 10.0, 5.0}
    assert "按分档制核算" in banded["reason"]

    hybrid = items["HYB-COMPLIANCE"]
    assert len(hybrid["sub_results"]) == 2
    assert hybrid["sub_results"][0]["checker_kind"] == "word_count"
    assert hybrid["sub_results"][0]["scoring_mode"] == "deterministic"
    assert hybrid["sub_results"][1]["evidence_gate_applied"] is True
    assert hybrid["final_score"] == pytest.approx(sum(item["score"] for item in hybrid["sub_results"]))
    assert "按混合制核算" in hybrid["reason"]

    assert run["model_provider"] == "mock"
    assert run["model_name"] == "mock-criterion-scorer"
    assert run["model_version"] == "v1"
    assert run["owner_id"] == M0_DEV_USER_ID
    assert run["paper_id"] == m0_scored_run["paper_id"]
    assert run["rubric_id"] == m0_scored_run["rubric_id"]
    assert all(item["scoring_run_id"] == run["id"] for item in items.values())
    assert len({item["id"] for item in items.values()}) == len(items)
    assert len({item["criterion_id"] for item in items.values()}) == len(items)
    assert run["total_tokens"] == 0
    assert run["ai_total_score"] == pytest.approx(sum(item["ai_score"] for item in items.values()))
    # Characterization only: the legacy grader applies absolute 100-point
    # bands even though this rubric totals 75. M1 intentionally corrects it.
    assert run["grade"] == "不及格"

    _assert_json_golden(
        "mock-scoring.json",
        {"run": run, "items": m0_scored_run["items"]},
    )


def test_m0_mock_scoring_is_reproducible(client, m0_scored_run):
    """The same paper and frozen rubric must reproduce the Mock business output."""

    retry_response = client.post("/api/scoring-runs/%s/retry" % m0_scored_run["run"]["id"])
    assert retry_response.status_code == 200, retry_response.text
    retry_run = retry_response.json()
    assert retry_run["id"] != m0_scored_run["run"]["id"]
    retry_items_response = client.get("/api/scoring-runs/%s/items" % retry_run["id"])
    assert retry_items_response.status_code == 200, retry_items_response.text

    first = _scoring_business_projection(m0_scored_run["run"], m0_scored_run["items"])
    second = _scoring_business_projection(retry_run, retry_items_response.json())
    assert second == first


def test_m0_review_report_and_run_export_match_goldens(client, m0_scored_run):
    """Freeze manual review, HTML report, and the public run-export@1 artifact."""

    hybrid = next(item for item in m0_scored_run["items"] if item["criterion_code"] == "HYB-COMPLIANCE")
    update_response = client.patch(
        "/api/score-items/%s" % hybrid["id"],
        json={"final_score": 16.0, "reason": "M0 golden：教师核对原文后调整混合项。"},
    )
    assert update_response.status_code == 200, update_response.text

    review_response = client.post(
        "/api/scoring-runs/%s/review" % m0_scored_run["run"]["id"],
        json={"reason": "M0 golden：人工复核完成。"},
    )
    assert review_response.status_code == 200, review_response.text
    reviewed_run = review_response.json()
    assert reviewed_run["status"] == "reviewed"
    assert reviewed_run["need_manual_review"] is False

    logs_response = client.get("/api/scoring-runs/%s/review-logs" % reviewed_run["id"])
    assert logs_response.status_code == 200, logs_response.text
    review_logs = logs_response.json()
    assert len(review_logs) == 2
    assert review_logs[0]["score_item_id"] == hybrid["id"]
    assert review_logs[1]["score_item_id"] is None
    assert all(log["scoring_run_id"] == reviewed_run["id"] for log in review_logs)
    assert all(log["reviewer_id"] == M0_DEV_USER_ID for log in review_logs)

    reviewed_items_response = client.get("/api/scoring-runs/%s/items" % reviewed_run["id"])
    assert reviewed_items_response.status_code == 200, reviewed_items_response.text
    reviewed_items = reviewed_items_response.json()
    adjusted_item = next(item for item in reviewed_items if item["criterion_code"] == "HYB-COMPLIANCE")
    _assert_json_golden(
        "manual-review.json",
        {
            "run": reviewed_run,
            "adjusted_item": {
                key: adjusted_item[key]
                for key in [
                    "id",
                    "scoring_run_id",
                    "criterion_id",
                    "criterion_code",
                    "criterion_name",
                    "max_score",
                    "ai_score",
                    "final_score",
                    "need_manual_review",
                    "created_at",
                ]
            },
            "review_logs": review_logs,
        },
    )

    export_response = client.get("/api/scoring-runs/%s/export.json" % reviewed_run["id"])
    assert export_response.status_code == 200, export_response.text
    run_export = export_response.json()
    assert run_export["schema"] == "paper-grading/run-export@1"
    assert run_export["run"] == reviewed_run
    assert run_export["paper"] == {
        "id": m0_scored_run["paper_id"],
        "title": "M0 代表性毕业论文",
        "student_id": "M0001",
        "student_name": "M0 测试学生",
    }
    assert run_export["rubric"] == {
        "id": m0_scored_run["rubric_id"],
        "name": M0_RUBRIC["name"],
        "version": M0_RUBRIC["version"],
    }
    assert run_export["items"] == reviewed_items
    assert run_export["review_logs"] == review_logs
    assert run_export["run"]["paper_id"] == run_export["paper"]["id"]
    assert run_export["run"]["rubric_id"] == run_export["rubric"]["id"]
    assert all(item["scoring_run_id"] == reviewed_run["id"] for item in run_export["items"])
    _assert_json_golden("run-export-v1.json", _run_export_golden_projection(run_export))

    report_response = client.get("/api/scoring-runs/%s/report" % reviewed_run["id"])
    assert report_response.status_code == 200, report_response.text
    report = _normalize_dynamic_text(report_response.text)
    assert "M0 代表性毕业论文" in report
    assert "M0 golden：教师核对原文后调整混合项。" in report
    assert "M0 golden：人工复核完成。" in report
    _assert_text_golden("report.html", report)


def _scoring_business_projection(run, items):
    """Remove only run-instance identity; keep every scoring decision comparable."""

    return {
        "run": {
            key: value
            for key, value in run.items()
            if key not in {"id", "started_at", "finished_at", "created_at"}
        },
        "items": [
            {
                key: value
                for key, value in item.items()
                if key not in {"id", "scoring_run_id", "created_at"}
            }
            for item in items
        ],
    }


def _run_export_golden_projection(run_export):
    """Keep the export golden readable while hashing the complete normalized payload."""

    normalized = _normalize_dynamic_values(run_export)
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    item_keys = sorted(normalized["items"][0]) if normalized["items"] else []
    review_log_keys = sorted(normalized["review_logs"][0]) if normalized["review_logs"] else []
    return {
        "normalized_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "schema": normalized["schema"],
        "schema_keys": {
            "top_level": sorted(normalized),
            "run": sorted(normalized["run"]),
            "item": item_keys,
            "review_log": review_log_keys,
        },
        "paper": normalized["paper"],
        "rubric": normalized["rubric"],
        "run": normalized["run"],
        "items": [
            {
                "criterion_code": item["criterion_code"],
                "criterion_name": item["criterion_name"],
                "max_score": item["max_score"],
                "ai_score": item["ai_score"],
                "final_score": item["final_score"],
                "evidence_sufficient": item["evidence_sufficient"],
                "evidence_count": len(item["evidence"]),
                "deduction_items": item["deduction_items"],
                "band_selection": item["band_selection"],
                "need_manual_review": item["need_manual_review"],
                "sub_results": [
                    {
                        key: sub.get(key)
                        for key in [
                            "criterion_name",
                            "max_score",
                            "score",
                            "evidence_sufficient",
                            "need_manual_review",
                            "checker_kind",
                            "scoring_mode",
                        ]
                        if key in sub
                    }
                    for sub in (item["sub_results"] or [])
                ],
            }
            for item in normalized["items"]
        ],
        "review_logs": normalized["review_logs"],
    }


def _normalize_dynamic_text(value):
    value = UUID_RE.sub("<UUID>", value)
    value = TIMESTAMP_RE.sub("<TIMESTAMP>", value)
    return "\n".join(line.rstrip() for line in value.splitlines())


def _normalize_dynamic_values(value):
    if isinstance(value, dict):
        return {key: _normalize_dynamic_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_dynamic_values(item) for item in value]
    if isinstance(value, str):
        return _normalize_dynamic_text(value)
    return value


def _assert_json_golden(name, payload):
    actual = json.dumps(
        _normalize_dynamic_values(payload),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    _assert_text_golden(name, actual)


def _assert_text_golden(name, actual):
    path = GOLDEN_DIR / name
    actual = actual.strip() + "\n"
    assert path.is_file(), "missing M0 golden %s; generated content:\n%s" % (path, actual)
    expected = path.read_text(encoding="utf-8").strip() + "\n"
    assert actual == expected
