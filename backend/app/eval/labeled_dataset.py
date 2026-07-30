"""QWK 留出集：从"论文文件夹 + 教师成绩表"构建评估，比对系统分与真实人工分（设计§15）。

成绩表（.xlsx 或 .csv）列约定：
- 文件名列（表头含 文件名/文件/论文/filename/file/paper）
- 总分列（表头含 总分/总成绩/total/score）
- 其余表头 = 评分项 code 的列 → 视为该项的人工分（分项分，用于逐维度 bias）
"""

import csv
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy.orm import Session

from backend.app.db.models import GradingBatch
from backend.app.db.models import Rubric
from backend.app.eval.run_eval import _items_by_code
from backend.app.eval.runner import EvalPrediction
from backend.app.eval.runner import EvalSample
from backend.app.eval.runner import evaluate
from backend.app.eval.gating import evaluation_sha256
from backend.app.services.calibration.library import get_anchors
from backend.app.services.papers.ingestion import ingest_file
from backend.app.services.scoring.engine import score_paper

FILENAME_HEADERS = ("文件名", "文件", "论文", "filename", "file", "paper")
TOTAL_HEADERS = ("总分", "总成绩", "总评", "total", "score")


def build_labeled_eval(
    db: Session,
    rubric_id: str,
    papers_dir,
    scores_path,
    scorer=None,
    sample_ids_by_filename=None,
):
    """从论文文件夹 + 教师成绩表构建评估：逐篇导入+评分，与人工真值比对（设计§15）。"""
    rubric = db.get(Rubric, rubric_id)
    if rubric is None:
        raise ValueError("rubric not found: %s" % rubric_id)
    rubric_codes = {criterion.code for criterion in rubric.criteria}
    table = load_scores_table(scores_path)
    if not table:
        raise ValueError("成绩表为空或未识别到有效行")

    batch = GradingBatch(name="QWK评估-%s-%s" % (rubric.name, rubric.version), rubric_id=rubric_id, status="draft")
    db.add(batch)
    db.flush()

    base_dir = Path(papers_dir)
    samples = []
    predictions = []
    errors = []
    completed_runs = 0
    review_required_runs = 0
    blocked_runs = 0
    per_sample = []
    sample_ids_by_filename = sample_ids_by_filename or {}
    for entry in table:
        source = base_dir / entry["filename"]
        sample_id = sample_ids_by_filename.get(entry["filename"])
        error_identity = (
            {"sample_id": sample_id}
            if sample_id
            else {"filename": entry["filename"]}
        )
        if not source.exists():
            errors.append({**error_identity, "error": "未找到论文文件"})
            continue
        try:
            paper = ingest_file(db, batch.id, str(source), entry["filename"])
            if paper.status != "parsed":
                errors.append(
                    {
                        **error_identity,
                        "error": paper.error_message or "解析失败",
                    }
                )
                continue
            run = score_paper(db, paper.id, scorer=scorer)
        except Exception as exc:  # 单篇失败不中断整批
            errors.append({**error_identity, "error": str(exc)})
            continue
        completed_runs += 1
        review_required_runs += int(bool(run.need_manual_review))
        system_items = _items_by_code(run)
        human_items = {
            code: value
            for code, value in entry["items"].items()
            if code in rubric_codes
        }
        private_sample = {
            "sample_id": sample_id or paper.id,
            "human_total": entry["total"],
            "system_total": (
                float(run.final_total_score)
                if run.final_total_score is not None
                else None
            ),
            "human_items": human_items,
            "system_items": system_items,
            "need_manual_review": bool(run.need_manual_review),
            "blocked": run.final_total_score is None,
            "run_identity": _run_identity_projection(run),
        }
        per_sample.append(private_sample)
        if run.final_total_score is None:
            blocked_runs += 1
            errors.append(
                {
                    **error_identity,
                    "error": "自动总分因 invalid/blocked 评分项为空；该样本未进入指标",
                }
            )
            continue
        sample_key = sample_id or paper.id
        samples.append(
            EvalSample(
                key=sample_key,
                human_total=entry["total"],
                human_items=human_items,
            )
        )
        predictions.append(
            EvalPrediction(
                key=sample_key,
                system_total=float(run.final_total_score),
                system_items=system_items,
            )
        )
    db.commit()

    report = evaluate(predictions, samples)
    report["errors"] = errors
    report["rubric_id"] = rubric_id
    report["dataset_size"] = len(table)
    report["completed_runs"] = completed_runs
    report["review_rate"] = (
        review_required_runs / completed_runs if completed_runs else None
    )
    report["blocked_rate"] = blocked_runs / completed_runs if completed_runs else None
    report["per_sample"] = per_sample
    anchors = []
    for code in sorted(rubric_codes):
        selected = get_anchors(db, rubric.id, code)
        anchors.append({"criterion_code": code, "anchors": selected})
    report["anchors_identity"] = {
        "schema": "paper-grading/anchor-manifest@1",
        "count": sum(len(item["anchors"]) for item in anchors),
        "manifest_sha256": evaluation_sha256(anchors),
        # CalibrationAnchor currently lacks a source-sample identity, so this
        # cannot be machine-proven and must be supplied by the approval review.
        "holdout_exclusion_proven": False,
    }
    return report


def _run_identity_projection(run):
    checker_manifest = run.checker_manifest
    return {
        "rubric_version_id": run.rubric_version_id,
        "rubric_version_hash": run.rubric_version_hash,
        "rubric_hash_scheme": run.rubric_hash_scheme,
        "rubric_snapshot_hash": run.rubric_snapshot_hash,
        "policy_hash": run.policy_hash,
        "execution_plan_hash": run.execution_plan_hash,
        "plan_schema_version": run.plan_schema_version,
        "checker_manifest_sha256": (
            evaluation_sha256(checker_manifest)
            if checker_manifest is not None
            else None
        ),
        "business_profile_key": run.business_profile_key,
        "workflow_profile": run.workflow_profile,
        "model_provider": run.model_provider,
        "model_name": run.model_name,
        "model_version": run.model_version,
        "engine_version": run.engine_version,
        "source_artifact_hash": run.source_artifact_hash,
        "normalized_content_hash": run.normalized_content_hash,
        "document_snapshot_hash": run.document_snapshot_hash,
    }


def load_scores_table(path):
    """返回 [{filename, total, items:{code:score}}]。"""
    suffix = Path(path).suffix.lower()
    rows = _read_xlsx(path) if suffix in (".xlsx", ".xlsm") else _read_csv(path)
    return _rows_to_samples(rows)


def _read_xlsx(path):
    workbook = load_workbook(str(path), data_only=True)
    sheet = workbook.active
    return [list(row) for row in sheet.iter_rows(values_only=True)]


def _read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return [row for row in csv.reader(handle)]


def _rows_to_samples(rows):
    rows = [row for row in rows if row and any(_norm(cell) for cell in row)]
    if not rows:
        return []
    raw_header = ["" if cell is None else str(cell).strip() for cell in rows[0]]
    norm_header = [cell.lower() for cell in raw_header]  # 仅用于别名匹配
    filename_idx = _find_column(norm_header, FILENAME_HEADERS)
    total_idx = _find_column(norm_header, TOTAL_HEADERS)
    if filename_idx is None or total_idx is None:
        raise ValueError("成绩表需包含『文件名』列与『总分』列")

    # 其余列视为评分项 code → 保留原始大小写（与 rubric 的 criterion code 严格匹配）。
    code_columns = {
        index: raw_header[index]
        for index in range(len(raw_header))
        if index not in (filename_idx, total_idx) and raw_header[index]
    }

    samples = []
    for row in rows[1:]:
        filename = _cell(row, filename_idx)
        total = _num(_cell(row, total_idx))
        if not filename or total is None:
            continue
        items = {}
        for index, code in code_columns.items():
            value = _num(_cell(row, index))
            if value is not None:
                items[code] = value
        samples.append({"filename": str(filename).strip(), "total": total, "items": items})
    return samples


def _find_column(header, aliases):
    for index, cell in enumerate(header):
        if cell and any(alias.lower() in cell for alias in aliases):
            return index
    return None


def _cell(row, index):
    if index is None or index >= len(row):
        return None
    return row[index]


def _num(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _norm(value):
    return "" if value is None else str(value).strip().lower()
