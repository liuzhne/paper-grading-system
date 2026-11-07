"""ASAP 2.0 外部基准评测脚本（docs/ASAP2.0外部基准评测方案.md）。

合成 CSV + Mock LLM，不读真实语料、不调用真实模型。
"""

from __future__ import annotations

import csv
from datetime import datetime
import json
from pathlib import Path

import pytest

from backend.app.eval import asap
from backend.app.eval.gating import GateValidationError
from backend.app.scripts import run_asap_eval


PROMPTS = ("Exploring Venus", "Driverless cars")
LEVELS_FILE = Path(asap.__file__).with_name("asap2_levels.unofficial.json")


def _write_csv(path: Path, count_per_cell: int = 2) -> list[dict]:
    rows = []
    for score in asap.SCORES:
        for prompt in PROMPTS:
            for index in range(count_per_cell):
                rows.append(
                    {
                        "essay_id": "E%d%s%d" % (score, prompt[:2].upper(), index),
                        "score": str(score),
                        "full_text": "Paragraph one about %s.\n\nParagraph two with evidence %d." % (prompt, index),
                        "assignment": "Write an argumentative essay about %s." % prompt,
                        "prompt_name": prompt,
                        "economically_disadvantaged": "Economically disadvantaged" if index else "NA",
                        "student_disability_status": "NA",
                        "ell_status": "No",
                        "race_ethnicity": "White",
                        "gender": "F" if index else "M",
                        "source_text_1": "Source text about %s." % prompt,
                        "source_text_2": "NA",
                        "source_text_3": "NA",
                        "source_text_4": "NA",
                    }
                )
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return rows


def test_stratified_sample_is_proportional_deterministic_and_respects_exclusions(tmp_path):
    _write_csv(tmp_path / "asap.csv", count_per_cell=4)
    rows = asap.load_rows(tmp_path / "asap.csv")
    first = asap.stratified_sample(rows, 24, seed=7)
    again = asap.stratified_sample(rows, 24, seed=7)
    other = asap.stratified_sample(rows, 24, seed=8)

    assert [row["essay_id"] for row in first] == [row["essay_id"] for row in again]
    assert [row["essay_id"] for row in first] != [row["essay_id"] for row in other]
    # 12 个分层（6 档 × 2 题目）各 4 篇，抽 24 篇即每层 2 篇。
    cells = {}
    for row in first:
        cells[(row["score"], row["prompt_name"])] = cells.get((row["score"], row["prompt_name"]), 0) + 1
    assert set(cells.values()) == {2}

    excluded = {row["essay_id"] for row in first}
    rest = asap.stratified_sample(rows, 24, seed=7, exclude=excluded)
    assert not excluded & {row["essay_id"] for row in rest}
    with pytest.raises(ValueError, match="抽样数"):
        asap.stratified_sample(rows, 49, seed=7)


def test_load_rows_rejects_missing_columns_and_bad_scores(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("essay_id,score\nA,3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少列"):
        asap.load_rows(path)

    rows = _write_csv(tmp_path / "asap.csv", count_per_cell=1)

    def rewrite(**changes):
        changed = [dict(rows[0], **changes)] + rows[1:]
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(changed)

    rewrite(score="7")
    with pytest.raises(ValueError, match="1–6"):
        asap.load_rows(path)
    for bad in ("3.0", ""):
        rewrite(score=bad)
        with pytest.raises(ValueError, match="分数不是整数：%s" % rows[0]["essay_id"]):
            asap.load_rows(path)
    # 同一 ID 两篇会生成同名文档、互相覆盖，成绩表却有两行。
    rewrite(essay_id=rows[1]["essay_id"])
    with pytest.raises(ValueError, match="essay_id 重复：%s" % rows[1]["essay_id"]):
        asap.load_rows(path)
    for bad in ("../escape", "", "a b"):
        rewrite(essay_id=bad)
        with pytest.raises(ValueError, match="essay_id 为空或含"):
            asap.load_rows(path)


def test_prepare_writes_documents_scores_and_a_text_free_manifest(tmp_path):
    from docx import Document

    source = tmp_path / "asap.csv"
    _write_csv(source)
    manifest = asap.prepare(source, tmp_path / "out", size=12, seed=1, with_sources=True)

    assert manifest["size"] == 12
    assert manifest["score_counts"] == {str(score): 2 for score in asap.SCORES}
    assert "full_text" not in json.dumps(manifest)
    essay_id = manifest["essays"][0]["essay_id"]
    text = "\n".join(p.text for p in Document(str(tmp_path / "out" / "papers" / ("%s.docx" % essay_id))).paragraphs)
    assert "Assignment" in text and "Essay" in text and "Sources" in text and "Paragraph two" in text
    with open(tmp_path / "out" / "scores.csv", encoding="utf-8") as handle:
        table = list(csv.reader(handle))
    assert table[0] == ["文件名", "总分", asap.CRITERION_CODE]
    assert len(table) == 13


def test_load_levels_requires_six_described_levels(tmp_path):
    levels = asap.load_levels(LEVELS_FILE)
    assert levels["official"] is False and sorted(levels["levels"]) == ["1", "2", "3", "4", "5", "6"]

    broken = tmp_path / "levels.json"
    broken.write_text(json.dumps({"levels": {"1": "a", "2": "b"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="1 到 6"):
        asap.load_levels(broken)
    broken.write_text(json.dumps({"levels": {str(n): ("" if n == 3 else "x") for n in asap.SCORES}}), encoding="utf-8")
    with pytest.raises(ValueError, match="描述"):
        asap.load_levels(broken)


def test_summarize_scores_on_the_raw_scale_and_reports_groups():
    manifest = {
        "essays": [
            {"essay_id": "a", "score": 2, "prompt_name": "P", "economically_disadvantaged": "NA", "ell_status": "No", "race_ethnicity": "W", "gender": "F"},
            {"essay_id": "b", "score": 4, "prompt_name": "P", "economically_disadvantaged": "NA", "ell_status": "Yes", "race_ethnicity": "W", "gender": "M"},
            {"essay_id": "c", "score": 6, "prompt_name": "Q", "economically_disadvantaged": "NA", "ell_status": "No", "race_ethnicity": "W", "gender": "F"},
            {"essay_id": "d", "score": 3, "prompt_name": "Q", "economically_disadvantaged": "NA", "ell_status": "No", "race_ethnicity": "W", "gender": "M"},
        ]
    }
    per_sample = [
        {"sample_id": "a", "system_total": 2.0, "need_manual_review": False},
        {"sample_id": "b", "system_total": 5.0, "need_manual_review": True},
        {"sample_id": "c", "system_total": None},
    ]
    summary = asap.summarize(per_sample, manifest)

    assert summary["n"] == 2 and summary["missing_essay_ids"] == ["c"]
    # d 没跑完（文件缺失、解析或评分出错），不在 per_sample 里，单列出来。
    assert summary["not_run_total"] == 1 and summary["not_run_essay_ids"] == ["d"]
    assert summary["n"] + summary["missing_total"] + summary["not_run_total"] == len(manifest["essays"])
    assert summary["exact"] == 0.5 and summary["adjacent"] == 1.0 and summary["mean_bias"] == 0.5
    assert summary["policy_review"] == 1
    assert summary["confusion"]["rows_human_cols_system"][3][4] == 1
    assert summary["groups"]["ell_status"] == {"No": {"n": 1, "bias": 0.0}, "Yes": {"n": 1, "bias": 1.0}}
    assert summary["per_score"]["6"] == {"n": 0, "exact": None, "mean_system": None}


def test_run_publishes_the_banded_rubric_and_scores_with_the_mock_model(client, tmp_path):
    source = tmp_path / "asap.csv"
    _write_csv(source, count_per_cell=1)
    data_dir = tmp_path / "data"
    asap.prepare(source, data_dir, size=12, seed=3)
    levels = asap.load_levels(LEVELS_FILE)

    with client.session_factory() as db:
        report = run_asap_eval.evaluate(db, client, data_dir, levels, label="mock")
        again = run_asap_eval.evaluate(db, client, data_dir, levels, label="mock-again")

        # 显式 --rubric-id 也要核对：别的档位文件建的评分标准会让报告记错档位哈希。
        assert run_asap_eval.resolve_rubric(db, client, levels, report["rubric_id"]) == report["rubric_id"]
        other = {**levels, "sha256": "0" * 64}
        with pytest.raises(RuntimeError, match="不是用这份档位文件建的"):
            run_asap_eval.evaluate(db, client, data_dir, other, label="x", rubric_id=report["rubric_id"])
        with pytest.raises(RuntimeError, match="不存在"):
            run_asap_eval.resolve_rubric(db, client, levels, "no-such-rubric")
        # 上次运行在发布前中断留下的草稿：显式给 ID 与按版本查到都拒绝。
        draft = client.post(
            run_asap_eval.settings.API_PREFIX.rstrip("/") + "/rubrics",
            json=asap.rubric_payload(other, version=run_asap_eval.levels_version(other)),
        )
        assert draft.status_code == 200, draft.text
        db.expire_all()
        for explicit in (draft.json()["id"], None):
            with pytest.raises(RuntimeError, match="未发布"):
                run_asap_eval.resolve_rubric(db, client, other, explicit)

    assert again["rubric_id"] == report["rubric_id"]  # 同一档位文件不重复建评分标准
    assert report["gating_eligible"] is False and report["levels"]["official"] is False
    assert report["runs"]["dataset_size"] == 12
    assert report["runs"]["completed_runs"] == 12, report["runs"]["errors"]
    summary = report["summary"]
    assert summary["n"] + summary["missing_total"] + summary["not_run_total"] == 12
    assert set(report["settings"]) >= set(run_asap_eval.STAGE2_SETTINGS)


def test_run_refuses_output_inside_the_repository(capsys):
    inside = run_asap_eval.PROJECT_ROOT / "asap-eval-output"
    code = run_asap_eval.main(["prepare", "--csv", "missing.csv", "--out", str(inside), "--size", "3"])
    assert code == 2
    assert "outside the repository" in capsys.readouterr().err
    assert not inside.exists()


def test_run_refuses_reports_database_and_storage_inside_the_repository(tmp_path, monkeypatch):
    settings = run_asap_eval.settings
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite+pysqlite:///%s" % (tmp_path / "eval.db"))
    monkeypatch.setattr(settings, "STORAGE_ROOT", tmp_path / "storage")
    path = run_asap_eval.check_run_environment(tmp_path, "baseline")
    assert path.parent == (tmp_path / "reports").resolve() and path.name.startswith("baseline-")

    # 组名带路径（相对或绝对）会把报告写到 reports/ 以外，包括仓库里。
    for label in ("../escape", str(run_asap_eval.PROJECT_ROOT / "report"), "..", ""):
        with pytest.raises(ValueError, match="--label"):
            run_asap_eval.check_run_environment(tmp_path, label)

    # 报告位置事先放了指向仓库的符号链接（哪怕目标还不存在）：拒绝，不跟随它写。
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 7, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(run_asap_eval, "datetime", _Frozen)
    target = run_asap_eval.PROJECT_ROOT / "asap-report-symlink-target.json"
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "baseline-20261107T120000Z.json").symlink_to(target)
    with pytest.raises(ValueError, match="符号链接"):
        run_asap_eval.check_run_environment(tmp_path, "baseline")
    assert not target.exists()
    monkeypatch.setattr(run_asap_eval, "datetime", datetime)

    # SQLite URI 形式：file: 后面的路径才是数据库文件；在仓库外运行也要拦下。
    monkeypatch.chdir(tmp_path)
    inside_db = run_asap_eval.PROJECT_ROOT / "eval.db"
    for url in ("sqlite+pysqlite:///file:%s?uri=true" % inside_db, "sqlite+pysqlite:///file://%s?mode=ro&uri=true" % inside_db):
        monkeypatch.setattr(settings, "DATABASE_URL", url)
        with pytest.raises(GateValidationError, match="SQLite"):
            run_asap_eval.check_run_environment(tmp_path, "baseline")

    # 相对路径按当前目录解析，与应用读设置时一致；在仓库根目录运行就落在仓库里。
    monkeypatch.chdir(run_asap_eval.PROJECT_ROOT)
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite+pysqlite:///eval.db")
    with pytest.raises(GateValidationError, match="SQLite"):
        run_asap_eval.check_run_environment(tmp_path, "baseline")
    assert not inside_db.exists()

    # 内存库不是文件，在仓库根目录运行也放行（包括 URI 形式的共享内存库）。
    for url in (
        "sqlite+pysqlite:///file:memdb?mode=memory&cache=shared&uri=true",
        "sqlite+pysqlite:///file::memory:?cache=shared&uri=true",
    ):
        monkeypatch.setattr(settings, "DATABASE_URL", url)
        run_asap_eval.check_run_environment(tmp_path, "baseline")

    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setattr(settings, "STORAGE_ROOT", Path("storage"))  # 默认值
    with pytest.raises(GateValidationError, match="STORAGE_ROOT"):
        run_asap_eval.check_run_environment(tmp_path, "baseline")


def test_run_reports_evaluation_errors_with_exit_code_2(tmp_path, monkeypatch, capsys):
    closed = []

    class _Session:
        def close(self):
            closed.append(True)

    class _Client:
        def __init__(self, app):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fail(*args, **kwargs):
        raise RuntimeError("评分标准 r1 已存在但未发布，请先在工作台处理或换档位文件")

    monkeypatch.setattr("fastapi.testclient.TestClient", _Client)
    monkeypatch.setattr("backend.app.db.session.SessionLocal", _Session)
    monkeypatch.setattr(run_asap_eval, "evaluate", _fail)
    monkeypatch.setattr(run_asap_eval.settings, "DATABASE_URL", "sqlite+pysqlite:///%s" % (tmp_path / "eval.db"))
    monkeypatch.setattr(run_asap_eval.settings, "STORAGE_ROOT", tmp_path / "storage")

    argv = ["run", "--data", str(tmp_path / "data"), "--levels", str(LEVELS_FILE), "--label", "mock"]
    code = run_asap_eval.main(argv)

    assert code == 2
    assert "已存在但未发布" in capsys.readouterr().err
    assert closed == [True]
    assert not (tmp_path / "data" / "reports").exists()

    # 评分跑完了但报告目录建不出来（评分期间那个位置出现了同名文件）：
    # 指标照常打印，退出码 2，不抛原始堆栈。
    def _finish_but_block_reports(db, client, data_dir, levels, *, label, rubric_id=None):
        (data_dir / "reports").write_text("not a directory", encoding="utf-8")
        summary = {name: None for name in ("qwk", "mae", "exact", "adjacent", "mean_bias")}
        summary.update(n=0, missing_total=0, not_run_total=0, policy_review=0)
        return {"label": label, "levels": {"official": False}, "settings": {"LLM_PROVIDER": "mock"}, "summary": summary}

    monkeypatch.setattr(run_asap_eval, "evaluate", _finish_but_block_reports)
    code = run_asap_eval.main(argv)

    captured = capsys.readouterr()
    assert code == 2
    assert "有效样本 0 篇" in captured.out
    assert "报告写不进去" in captured.err
    assert closed == [True, True]

    # 报告目录事先就不是目录：开评前就拒绝，不白跑一轮评分。
    with pytest.raises(ValueError, match="不是目录"):
        run_asap_eval.check_run_environment(tmp_path / "data", "mock")


def test_run_checks_paths_the_way_sqlalchemy_and_storage_resolve_them(tmp_path, monkeypatch):
    # SQLAlchemy 与 STORAGE_ROOT 都不展开 ~：在仓库根目录运行时 ~/eval.db 是仓库里名为 ~ 的目录。
    repo = (tmp_path / "repo").resolve()
    (repo / "~").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(run_asap_eval, "PROJECT_ROOT", repo)
    monkeypatch.chdir(repo)
    settings = run_asap_eval.settings
    outside = tmp_path / "outside"

    monkeypatch.setattr(settings, "STORAGE_ROOT", outside / "storage")
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite+pysqlite:///~/eval.db")
    with pytest.raises(GateValidationError, match="SQLite"):
        run_asap_eval.check_run_environment(outside, "baseline")

    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite+pysqlite:///%s" % (outside / "eval.db"))
    monkeypatch.setattr(settings, "STORAGE_ROOT", Path("~/storage"))
    with pytest.raises(GateValidationError, match="STORAGE_ROOT"):
        run_asap_eval.check_run_environment(outside, "baseline")
