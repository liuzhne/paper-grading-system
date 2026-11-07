"""ASAP 2.0 外部基准评测（docs/ASAP2.0外部基准评测方案.md）。

这里只做数据准备、评分标准载荷与指标；导入、Core 评分、比对复用
``labeled_dataset.build_labeled_eval``。作文正文和档位原文都不进仓库，报告也不写正文。
"""

from __future__ import annotations

from collections import Counter
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import random
import re
import sys

from backend.app.eval.metrics import mae
from backend.app.eval.metrics import quadratic_weighted_kappa


MANIFEST_SCHEMA = "asap2-eval-manifest@1"
REPORT_SCHEMA = "asap2-eval-report@1"
CRITERION_CODE = "HOLISTIC"
SCORES = (1, 2, 3, 4, 5, 6)
RUBRIC_NAME = "ASAP 2.0 整体评分"
REQUIRED_COLUMNS = ("essay_id", "score", "full_text", "assignment", "prompt_name")
SOURCE_COLUMNS = ("source_text_1", "source_text_2", "source_text_3", "source_text_4")
GROUP_FIELDS = (
    "prompt_name",
    "economically_disadvantaged",
    "ell_status",
    "race_ethnicity",
    "gender",
)
_MISSING = {"", "NA", "N/A", "nan"}
# essay_id 直接当文件名（papers/<essay_id>.docx），只收字母、数字、下划线与连字符。
_ESSAY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_rows(path) -> list[dict]:
    """读 Kaggle 的 ASAP 2.0 CSV；分数转成整数。

    缺列、essay_id 不能当文件名或重复、分数不是 1–6 的整数都报错，并指出是哪一篇。
    重复 ID 在这里拦下：生成文档时同名文件会互相覆盖，成绩表却仍有两行。
    """

    csv.field_size_limit(sys.maxsize)
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in REQUIRED_COLUMNS if name not in (reader.fieldnames or ())]
        if missing:
            raise ValueError("ASAP 2.0 CSV 缺少列：%s" % ", ".join(missing))
        rows = []
        seen = set()
        for row in reader:
            essay_id = row["essay_id"]
            if not isinstance(essay_id, str) or not _ESSAY_ID.fullmatch(essay_id):
                raise ValueError("essay_id 为空或含字母、数字、_、- 以外的字符：%r" % essay_id)
            if essay_id in seen:
                raise ValueError("essay_id 重复：%s" % essay_id)
            seen.add(essay_id)
            try:
                score = int(row["score"])
            except (TypeError, ValueError):
                raise ValueError("分数不是整数：%s" % essay_id) from None
            if score not in SCORES:
                raise ValueError("分数超出 1–6：%s" % essay_id)
            rows.append({**row, "score": score})
    return rows


def stratified_sample(rows, size: int, seed: int, exclude=()) -> list[dict]:
    """按（分数, 题目）分层、按比例抽样；名额用最大余数法分配，结果与种子一一对应。"""

    excluded = set(exclude)
    strata = defaultdict(list)
    for row in rows:
        if row["essay_id"] not in excluded:
            strata[(row["score"], row["prompt_name"])].append(row)
    total = sum(len(items) for items in strata.values())
    if size <= 0 or size > total:
        raise ValueError("抽样数要在 1 到 %d 之间" % total)
    keys = sorted(strata)
    quotas = {key: size * len(strata[key]) / total for key in keys}
    counts = {key: int(quotas[key]) for key in keys}
    remainder = size - sum(counts.values())
    for key in sorted(keys, key=lambda item: (counts[item] - quotas[item], item))[:remainder]:
        counts[key] += 1
    generator = random.Random(seed)
    sample = []
    for key in keys:
        items = sorted(strata[key], key=lambda row: row["essay_id"])
        sample.extend(generator.sample(items, counts[key]))
    return sorted(sample, key=lambda row: row["essay_id"])


def _paragraphs(text: str) -> list[str]:
    parts = [part.strip() for part in str(text or "").replace("\r\n", "\n").split("\n")]
    return [part for part in parts if part]


def build_document(row: dict, path, *, with_sources: bool = False) -> None:
    """一篇作文一份 .docx：题目要求在前，作文在后；可选把材料原文附在最后。"""

    from docx import Document

    document = Document()
    document.add_heading(str(row["prompt_name"]), level=1)
    document.add_heading("Assignment", level=2)
    for paragraph in _paragraphs(row["assignment"]):
        document.add_paragraph(paragraph)
    document.add_heading("Essay", level=2)
    for paragraph in _paragraphs(row["full_text"]):
        document.add_paragraph(paragraph)
    if with_sources:
        sources = [row.get(name) for name in SOURCE_COLUMNS if row.get(name) not in _MISSING and row.get(name)]
        if sources:
            document.add_heading("Sources", level=2)
            for source in sources:
                for paragraph in _paragraphs(source):
                    document.add_paragraph(paragraph)
    document.save(str(path))


def filename_for(essay_id: str) -> str:
    return "%s.docx" % essay_id


def prepare(csv_path, out_dir, *, size: int, seed: int, with_sources: bool = False, exclude=()) -> dict:
    """抽样并写出 papers/*.docx、scores.csv 与 manifest.json；返回 manifest。"""

    rows = load_rows(csv_path)
    sample = stratified_sample(rows, size, seed, exclude=exclude)
    out = Path(out_dir)
    papers = out / "papers"
    papers.mkdir(parents=True, exist_ok=True)
    for row in sample:
        build_document(row, papers / filename_for(row["essay_id"]), with_sources=with_sources)
    with open(out / "scores.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["文件名", "总分", CRITERION_CODE])
        for row in sample:
            writer.writerow([filename_for(row["essay_id"]), row["score"], row["score"]])
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "source_file": Path(csv_path).name,
        "source_sha256": sha256_file(csv_path),
        "size": len(sample),
        "seed": seed,
        "with_sources": with_sources,
        "excluded_count": len(set(exclude)),
        "score_counts": {str(score): count for score, count in sorted(Counter(row["score"] for row in sample).items())},
        "essays": [
            {
                "essay_id": row["essay_id"],
                "score": row["score"],
                **{field: row.get(field) or "NA" for field in GROUP_FIELDS},
            }
            for row in sample
        ],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def load_levels(path) -> dict:
    """档位描述：``{"official": bool, "source": str, "levels": {"1": "...", ..., "6": "..."}}``。"""

    raw = Path(path).read_bytes()
    value = json.loads(raw.decode("utf-8"))
    levels = value.get("levels") if isinstance(value, dict) else None
    if not isinstance(levels, dict) or sorted(levels) != [str(score) for score in SCORES]:
        raise ValueError("档位文件要有 levels，键正好是 1 到 6")
    if any(not isinstance(text, str) or not text.strip() for text in levels.values()):
        raise ValueError("每个档位都要有描述")
    return {
        "official": bool(value.get("official")),
        "source": str(value.get("source") or ""),
        "levels": {key: levels[key].strip() for key in sorted(levels)},
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def rubric_payload(levels: dict, *, version: str) -> dict:
    """一个评分项、6 个档位（S1–S6，分值 1–6），与工作台手工创建同一个接口载荷。"""

    return {
        "name": RUBRIC_NAME,
        "version": version,
        "total_score": 6,
        "criteria": [
            {
                "code": CRITERION_CODE,
                "name": "Holistic writing quality",
                "max_score": 6,
                "scoring_mode": "banded",
                "description": (
                    "Holistic score of a source-based argumentative essay written by a U.S. "
                    "secondary student, judged against the assignment shown in the document."
                ),
                "rubric_levels": [
                    {
                        "level_code": "S%d" % score,
                        "points": score,
                        "descriptor": levels["levels"][str(score)],
                        "display_order": score - 1,
                    }
                    for score in SCORES
                ],
            }
        ],
    }


def _rounded(value) -> int:
    return min(max(int(round(float(value))), SCORES[0]), SCORES[-1])


def summarize(per_sample, manifest: dict) -> dict:
    """在 1–6 原始分上算指标；没有自动总分的样本单列，不进指标。

    没有总分的样本包括判失败的，以及有评分项等人工定分的（“证据不足”转人工就在这里）。
    没跑完的样本（文件缺失、解析或评分出错，原因在报告的 ``runs.errors``）不在
    ``per_sample`` 里，另列 ``not_run``，保证 n + 无总分 + 没跑完 = 清单篇数。
    ``policy_review`` 只是评分政策的复核标记（总分过低、接近等级线），总分照常进指标。
    """

    essays = {item["essay_id"]: item for item in manifest["essays"]}
    scored = []
    missing = []
    seen = set()
    for sample in per_sample:
        essay = essays.get(sample["sample_id"])
        if essay is None:
            continue
        seen.add(essay["essay_id"])
        if sample.get("system_total") is None:
            missing.append(essay["essay_id"])
            continue
        scored.append((essay, essay["score"], _rounded(sample["system_total"]), bool(sample.get("need_manual_review"))))
    human = [item[1] for item in scored]
    system = [item[2] for item in scored]
    confusion = [[0] * len(SCORES) for _ in SCORES]
    for truth, predicted in zip(human, system):
        confusion[truth - 1][predicted - 1] += 1
    per_score = {}
    for score in SCORES:
        pairs = [(truth, predicted) for truth, predicted in zip(human, system) if truth == score]
        per_score[str(score)] = {
            "n": len(pairs),
            "exact": round(sum(truth == predicted for truth, predicted in pairs) / len(pairs), 3) if pairs else None,
            "mean_system": round(sum(predicted for _truth, predicted in pairs) / len(pairs), 3) if pairs else None,
        }
    groups = {}
    for field in GROUP_FIELDS:
        buckets = defaultdict(list)
        for essay, truth, predicted, _review in scored:
            buckets[essay.get(field) or "NA"].append(predicted - truth)
        groups[field] = {
            value: {"n": len(deltas), "bias": round(sum(deltas) / len(deltas), 3)}
            for value, deltas in sorted(buckets.items())
        }
    n = len(scored)
    not_run = sorted(essay_id for essay_id in essays if essay_id not in seen)
    return {
        "n": n,
        "missing_total": len(missing),
        "missing_essay_ids": sorted(missing),
        "not_run_total": len(not_run),
        "not_run_essay_ids": not_run,
        "policy_review": sum(item[3] for item in scored),
        "qwk": quadratic_weighted_kappa(human, system, SCORES[0], SCORES[-1]) if n else None,
        "mae": mae(human, system) if n else None,
        "exact": round(sum(a == b for a, b in zip(human, system)) / n, 3) if n else None,
        "adjacent": round(sum(abs(a - b) <= 1 for a, b in zip(human, system)) / n, 3) if n else None,
        "mean_bias": round(sum(b - a for a, b in zip(human, system)) / n, 3) if n else None,
        "confusion": {"rows_human_cols_system": [list(row) for row in confusion], "labels": list(SCORES)},
        "per_score": per_score,
        "groups": groups,
    }


__all__ = [
    "CRITERION_CODE",
    "MANIFEST_SCHEMA",
    "REPORT_SCHEMA",
    "RUBRIC_NAME",
    "build_document",
    "filename_for",
    "load_levels",
    "load_rows",
    "prepare",
    "rubric_payload",
    "sha256_file",
    "stratified_sample",
    "summarize",
]
