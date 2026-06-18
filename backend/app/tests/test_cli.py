"""CLI 端（pgs）冒烟测试：CliRunner + 临时 sqlite + 强制 Mock，自包含不联网。

注意：conftest 的 mock 隔离是普通 fixture（仅 client 测试生效），CLI 测试不走它，
故本文件 autouse 强制 settings.LLM_PROVIDER=mock 并在结束后还原被 configure 改写的全局设置。
"""

import json

import pytest
from typer.testing import CliRunner

from backend.app.cli.main import app
from backend.app.core.config import settings
from backend.app.tests.conftest import make_rules_xlsx
from backend.app.tests.conftest import make_sample_docx

runner = CliRunner()

SEED_RUBRIC = "本科毕业论文通用评分标准"


@pytest.fixture(autouse=True)
def _cli_isolation():
    saved = (settings.DATABASE_URL, settings.STORAGE_ROOT, settings.LLM_PROVIDER)
    settings.LLM_PROVIDER = "mock"  # 任何 get_llm_scorer() → mock，绝不联网
    yield
    settings.DATABASE_URL, settings.STORAGE_ROOT, settings.LLM_PROVIDER = saved


@pytest.fixture
def local(tmp_path):
    """每个测试独立的本地 sqlite + storage。"""
    return ["--db", str(tmp_path / "cli.db"), "--storage", str(tmp_path / "storage")]


def test_init_seed_then_rubrics_lists_default(local):
    assert runner.invoke(app, ["init", "--seed", *local]).exit_code == 0
    listing = runner.invoke(app, ["rubrics", "--json", *local])
    assert listing.exit_code == 0
    assert SEED_RUBRIC in listing.output


def test_check_mock_ok():
    result = runner.invoke(app, ["check", "--json"])
    assert result.exit_code == 0
    assert '"stage": "mock"' in result.output


def test_score_mock_writes_report(tmp_path, local):
    runner.invoke(app, ["init", "--seed", *local])
    docx = tmp_path / "thesis.docx"
    docx.write_bytes(make_sample_docx().getvalue())
    report_dir = tmp_path / "reports"
    result = runner.invoke(
        app,
        ["score", str(docx), "--rubric", SEED_RUBRIC, "--mock", "--report-dir", str(report_dir), *local],
    )
    assert result.exit_code == 0, result.output
    assert "thesis.docx" in result.output
    assert (report_dir / "thesis.html").exists()


def test_score_bad_file_nonzero_exit(tmp_path, local):
    runner.invoke(app, ["init", "--seed", *local])
    bad = tmp_path / "broken.docx"
    bad.write_text("this is not a docx")
    result = runner.invoke(app, ["score", str(bad), "--rubric", SEED_RUBRIC, "--mock", "--json", *local])
    assert result.exit_code == 1  # 解析失败 → 非零退出（供脚本/CI 判定）
    assert "broken.docx" in result.output


def test_score_unknown_rubric_errors(tmp_path, local):
    runner.invoke(app, ["init", *local])
    docx = tmp_path / "t.docx"
    docx.write_bytes(make_sample_docx().getvalue())
    result = runner.invoke(app, ["score", str(docx), "--rubric", "不存在的标准", "--mock", *local])
    assert result.exit_code != 0


def test_import_rubric_from_excel(tmp_path, local):
    rules = tmp_path / "rules.xlsx"
    rules.write_bytes(make_rules_xlsx().getvalue())
    result = runner.invoke(app, ["import", str(rules), "--name", "CLI导入标准", "--version", "v1.0", *local])
    assert result.exit_code == 0, result.output
    listing = runner.invoke(app, ["rubrics", "--json", *local])
    assert "CLI导入标准" in listing.output


def test_runs_and_show_after_score(tmp_path, local):
    runner.invoke(app, ["init", "--seed", *local])
    docx = tmp_path / "t.docx"
    docx.write_bytes(make_sample_docx().getvalue())
    scored = runner.invoke(app, ["score", str(docx), "--rubric", SEED_RUBRIC, "--mock", "--json", *local])
    assert scored.exit_code == 0, scored.output
    run_id = json.loads(scored.output)["results"][0]["run_id"]

    listed = runner.invoke(app, ["runs", "--json", *local])
    assert listed.exit_code == 0
    assert run_id in listed.output

    shown = runner.invoke(app, ["show", run_id, "--json", *local])
    assert shown.exit_code == 0, shown.output
    detail = json.loads(shown.output)
    assert detail["run"]["run_id"] == run_id
    assert len(detail["items"]) == 7  # 种子 7 个评分项


def test_batches_lists_after_score(tmp_path, local):
    runner.invoke(app, ["init", "--seed", *local])
    docx = tmp_path / "t.docx"
    docx.write_bytes(make_sample_docx().getvalue())
    runner.invoke(app, ["score", str(docx), "--rubric", SEED_RUBRIC, "--mock", *local])
    result = runner.invoke(app, ["batches", "--json", *local])
    assert result.exit_code == 0
    assert json.loads(result.output)  # 非空


def test_publish_draft_rubric(tmp_path, local):
    rules = tmp_path / "rules.xlsx"
    rules.write_bytes(make_rules_xlsx().getvalue())
    assert runner.invoke(app, ["import", str(rules), "--name", "待发布标准", *local]).exit_code == 0
    rid = next(r["id"] for r in json.loads(runner.invoke(app, ["rubrics", "--json", *local]).output) if r["name"] == "待发布标准")
    assert runner.invoke(app, ["publish", rid, *local]).exit_code == 0
    after = json.loads(runner.invoke(app, ["rubrics", "--json", *local]).output)
    assert next(r["status"] for r in after if r["id"] == rid) == "published"


def test_score_multiple_with_workers(tmp_path, local):
    runner.invoke(app, ["init", "--seed", *local])
    docs = []
    for i in range(3):
        d = tmp_path / ("p%d.docx" % i)
        d.write_bytes(make_sample_docx().getvalue())
        docs.append(str(d))
    result = runner.invoke(
        app, ["score", *docs, "--rubric", SEED_RUBRIC, "--mock", "--workers", "3", "--json", *local]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data["results"]) == 3
    assert all(r["status"] == "ok" for r in data["results"])


def test_score_no_db_is_stateless(tmp_path):
    rules = tmp_path / "rules.xlsx"
    rules.write_bytes(make_rules_xlsx().getvalue())
    docx = tmp_path / "t.docx"
    docx.write_bytes(make_sample_docx().getvalue())
    ghost_db = tmp_path / "should_not_exist.db"  # --no-db 即便给了 --db 也不应建库
    result = runner.invoke(
        app,
        ["score", str(docx), "--no-db", "--rubric-file", str(rules), "--mock", "--json", "--db", str(ghost_db)],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["stateless"] is True
    assert len(data["results"]) == 1
    assert data["results"][0]["status"] == "ok"
    assert data["results"][0]["items"]  # 含逐项明细
    assert not ghost_db.exists()  # 关键：无状态不建任何 sqlite


def test_score_no_db_requires_rubric_file(tmp_path):
    docx = tmp_path / "t.docx"
    docx.write_bytes(make_sample_docx().getvalue())
    result = runner.invoke(app, ["score", str(docx), "--no-db", "--mock"])
    assert result.exit_code != 0  # 缺 --rubric-file → 报错退出


def test_review_overrides_item_and_submits(tmp_path, local):
    runner.invoke(app, ["init", "--seed", *local])
    docx = tmp_path / "t.docx"
    docx.write_bytes(make_sample_docx().getvalue())
    run_id = json.loads(
        runner.invoke(app, ["score", str(docx), "--rubric", SEED_RUBRIC, "--mock", "--json", *local]).output
    )["results"][0]["run_id"]

    reviewed = runner.invoke(app, ["review", run_id, "--set", "C01=9", "--note", "人工调整", "--submit", *local])
    assert reviewed.exit_code == 0, reviewed.output

    shown = json.loads(runner.invoke(app, ["show", run_id, "--json", *local]).output)
    assert shown["run"]["status"] == "reviewed"
    assert next(i["score"] for i in shown["items"] if i["code"] == "C01") == 9.0


def test_review_rejects_unknown_code(tmp_path, local):
    runner.invoke(app, ["init", "--seed", *local])
    docx = tmp_path / "t.docx"
    docx.write_bytes(make_sample_docx().getvalue())
    run_id = json.loads(
        runner.invoke(app, ["score", str(docx), "--rubric", SEED_RUBRIC, "--mock", "--json", *local]).output
    )["results"][0]["run_id"]
    assert runner.invoke(app, ["review", run_id, "--set", "ZZ=5", *local]).exit_code != 0


def test_scores_template_columns_match_rubric(tmp_path, local):
    runner.invoke(app, ["init", "--seed", *local])
    rid = next(
        r["id"]
        for r in json.loads(runner.invoke(app, ["rubrics", "--json", *local]).output)
        if r["name"] == SEED_RUBRIC
    )
    out = tmp_path / "tmpl.xlsx"
    result = runner.invoke(app, ["scores-template", rid, "-o", str(out), *local])
    assert result.exit_code == 0, result.output

    from openpyxl import load_workbook

    headers = [cell.value for cell in load_workbook(out)["教师评分"][1]]
    assert headers[:2] == ["文件名", "总分"]
    assert "C01" in headers and "C07" in headers


def test_check_mock_overrides_real_provider(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai_compatible")  # 即使配了真实 provider
    result = runner.invoke(app, ["check", "--mock", "--json"])
    assert result.exit_code == 0
    assert '"stage": "mock"' in result.output


def test_apply_llm_overrides_local(monkeypatch):
    from backend.app.cli.main import _apply_llm_overrides

    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")
    monkeypatch.setattr(settings, "LOCAL_LLM_BASE_URL", "placeholder")
    monkeypatch.setattr(settings, "LOCAL_LLM_MODEL", "placeholder")
    _apply_llm_overrides("local", "qwen3-30b", "http://localhost:1234/v1")
    assert settings.LLM_PROVIDER == "local"
    assert settings.LOCAL_LLM_BASE_URL == "http://localhost:1234/v1"
    assert settings.LOCAL_LLM_MODEL == "qwen3-30b"


def test_doctor_reports_offline_ready(monkeypatch):
    # mock LLM + mock 表格写入 → 纯本地、零外呼。
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")
    monkeypatch.setattr(settings, "SHEET_WRITER_PROVIDER", "mock")
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    assert '"offline_ready": true' in result.output


def test_doctor_flags_cloud_touchpoint(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")
    monkeypatch.setattr(settings, "SHEET_WRITER_PROVIDER", "google_sheets")  # 外呼环节
    result = runner.invoke(app, ["doctor", "--json"])
    assert '"offline_ready": false' in result.output
