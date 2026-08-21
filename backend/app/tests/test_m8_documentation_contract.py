from pathlib import Path

from backend.app.core.config import settings


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_operator_guides_share_current_runtime_migration_and_profile_facts():
    assert settings.SCORING_ENGINE_MODE == "legacy"
    for path in ("README.md", "AGENTS.md", "CLAUDE.md"):
        text = _read(path)
        assert "Python 3.10+" in text
        assert "0017_batch_scoring_jobs" in text
        assert "technical_proposal / technical-proposal-profile@1" in text
        assert "SCORING_ENGINE_MODE" in text
        assert "默认" in text and "legacy" in text
        assert "GATE-03" in text


def test_current_plan_and_historical_progress_have_an_explicit_truth_order():
    plan = _read("代码改造计划.md")
    progress = _read("执行进展.md")

    assert "Accepted ADR > 当前代码/迁移 head > `代码改造计划.md` §7" in plan
    assert "M7 状态：已完成" in plan
    assert "M8 状态：进行中" in plan
    assert "当前评分入口 | v1 `score_paper()` + v2 `score_generic_submission()`" in plan
    assert "历史冻结记录" in progress[:600]
    assert "2026-06-16" in progress[:600]
    assert "当前待办以 `代码改造计划.md` §7 和 Linear 为准" in progress[:600]


def test_web_cli_matrix_and_deployment_docs_describe_m8_boundaries():
    matrix = _read("docs/web-cli-功能对等.md")
    deployment = _read("docs/部署.md")

    assert "core-cutover-audit" in matrix
    assert "Rubric 严格审核/发布" in matrix
    assert "静态 Web 生命周期闭环" in matrix
    assert "0017_batch_scoring_jobs" in deployment
    assert "batch_scoring_jobs" in deployment
    assert "经批准的观察策略" in deployment
    assert "GATE-03" in deployment
    assert "production_default_switch_authorized=false" in deployment
    assert "legacy compatibility" in deployment


def test_eval_docs_require_complete_real_gate_identity_and_approval():
    evaluation = _read("backend/app/eval/README.md")
    baselines = _read("docs/baselines/README.md")
    combined = evaluation + "\n" + baselines

    for marker in (
        "dataset",
        "truth",
        "RubricVersion",
        "DocumentSnapshot",
        "model/provider artifact",
        "policy",
        "plan",
        "prompt",
        "anchors",
        "checker",
        "revision",
        "gating_eligible=true",
        "GATE-03",
        "维护者批准",
        "回退容差",
    ):
        assert marker in combined
    assert "test-only" in combined
    assert "production_default_switch_authorized=false" in combined


def test_obsolete_fixed_cap_and_sqlite_serialization_copy_is_gone():
    frontend = _read("frontend/web/assets/app.js")
    integrations = _read("docs/integrations.md")
    cli = _read("backend/app/cli/main.py")

    assert "普通情况最高按80%控制" not in frontend
    assert "普通情况会按 `SCORING_STANDARD_CAP_RATIO=0.8` 封顶" not in integrations
    assert "多 worker 实际趋于串行" not in cli
    assert "冻结 ScoringPolicy" in frontend
    assert "legacy_unversioned compatibility" in integrations
    assert "WAL" in cli and "busy_timeout" in cli
