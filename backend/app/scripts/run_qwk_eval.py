"""QWK 验收脚本（设计§15）：用"论文文件夹 + 教师成绩表"对系统评分做一致性评估。

用法（需真实数据 + 真实 LLM；学生数据放仓库外）：
    LLM_PROVIDER=openai_compatible OPENAI_COMPATIBLE_API_KEY=... \\
    .venv/bin/python -m backend.app.scripts.run_qwk_eval \\
        --rubric-id <已发布的rubric_id> --papers-dir /path/to/theses --scores /path/to/scores.xlsx

行为：出报告(storage/eval/) → 首跑写入基线 baseline.json（仅聚合指标）→ 已有基线则做回归门禁，
**QWK 跌幅/MAE 涨幅超容差即非零退出**（可接 CI/cron）。
"""

import argparse
import json
import sys
from pathlib import Path

from backend.app.core.config import settings
from backend.app.db.session import SessionLocal
from backend.app.eval.labeled_dataset import build_labeled_eval
from backend.app.eval.run_eval import _write_report
from backend.app.eval.runner import assert_no_regression
from backend.app.eval.runner import baseline_from_report


def main(argv=None):
    parser = argparse.ArgumentParser(description="QWK 留出集评估（系统分 vs 教师分）")
    parser.add_argument("--rubric-id", required=True)
    parser.add_argument("--papers-dir", required=True)
    parser.add_argument("--scores", required=True, help="教师成绩表 .xlsx 或 .csv（文件名+总分+各评分项code列）")
    parser.add_argument("--baseline", default=None, help="基线 JSON 路径（默认 storage/eval/baseline.json）")
    args = parser.parse_args(argv)

    baseline_path = Path(args.baseline) if args.baseline else (settings.STORAGE_ROOT / "eval" / "baseline.json")

    with SessionLocal() as db:
        report = build_labeled_eval(db, args.rubric_id, args.papers_dir, args.scores)

    report_path = _write_report(report)
    _print_summary(report, report_path)

    issues = []
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        issues = assert_no_regression(report, baseline)
        if issues:
            print("\n❌ 回归门禁未通过：")
            for issue in issues:
                print("  -", issue)
        else:
            print("\n✓ 回归门禁通过（未低于基线）。")
    else:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(baseline_from_report(report), ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n✓ 首次运行：已将当前指标固化为基线 → %s" % baseline_path)

    return 1 if issues else 0


def _print_summary(report, report_path):
    print("样本 n=%s（数据集 %s，错误 %s）" % (report.get("n"), report.get("dataset_size"), len(report.get("errors") or [])))
    print("QWK=%s  MAE=%s  RMSE=%s" % (report.get("qwk"), report.get("mae"), report.get("rmse")))
    print("同档一致率=%s  相邻档一致率=%s" % (report.get("exact_grade_agreement"), report.get("adjacent_grade_agreement")))
    per_criterion = report.get("per_criterion") or {}
    if per_criterion:
        print("逐维度 bias（>0 偏宽 / <0 偏严）：")
        for code, stats in per_criterion.items():
            print("  %s: bias=%.3f mae=%.3f n=%s" % (code, stats["bias"], stats["mae"], stats["n"]))
    print("报告：%s" % report_path)


if __name__ == "__main__":
    sys.exit(main())
