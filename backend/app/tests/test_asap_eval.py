"""ASAP 2.0 外部基准评测脚本（docs/ASAP2.0外部基准评测方案.md）。

合成 CSV + Mock LLM，不读真实语料、不调用真实模型。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from backend.app.eval import asap
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
    rows[0]["score"] = "7"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match="1–6"):
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
        ]
    }
    per_sample = [
        {"sample_id": "a", "system_total": 2.0, "need_manual_review": False},
        {"sample_id": "b", "system_total": 5.0, "need_manual_review": True},
        {"sample_id": "c", "system_total": None},
    ]
    summary = asap.summarize(per_sample, manifest)

    assert summary["n"] == 2 and summary["missing_essay_ids"] == ["c"]
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

    assert again["rubric_id"] == report["rubric_id"]  # 同一档位文件不重复建评分标准
    assert report["gating_eligible"] is False and report["levels"]["official"] is False
    assert report["runs"]["dataset_size"] == 12
    assert report["runs"]["completed_runs"] == 12, report["runs"]["errors"]
    summary = report["summary"]
    assert summary["n"] + summary["missing_total"] == 12
    assert set(report["settings"]) >= set(run_asap_eval.STAGE2_SETTINGS)


def test_run_refuses_output_inside_the_repository(capsys):
    inside = run_asap_eval.PROJECT_ROOT / "asap-eval-output"
    code = run_asap_eval.main(["prepare", "--csv", "missing.csv", "--out", str(inside), "--size", "3"])
    assert code == 2
    assert "outside the repository" in capsys.readouterr().err
    assert not inside.exists()
