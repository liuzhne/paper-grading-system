"""QWK 验收脚本（设计§15）：用"论文文件夹 + 教师成绩表"对系统评分做一致性评估。

用法（需真实数据 + 真实 LLM；学生数据放仓库外）：
    LLM_PROVIDER=openai_compatible OPENAI_COMPATIBLE_API_KEY=... \\
    .venv/bin/python -m backend.app.scripts.run_qwk_eval \\
        --rubric-id <已发布的rubric_id> --papers-dir /path/to/theses --scores /path/to/scores.xlsx

普通模式：出报告(storage/eval/) → 首跑写入非发布基线 baseline.json → 已有基线则做回归检查。

PGS-8 发布门禁模式额外要求：
    --release-gate --dataset-id ... --dataset-version ...
    --artifact-dir /path/outside/repository
    (--model-artifact /path/to/model OR --immutable-model-revision ...)

发布门禁先生成 candidate；维护者用精确 candidate hash 填写 approval JSON 后，对原
candidate 执行离线 finalize，才可能得到 gate_passed=true。任何 identity、隐私确认
或阈值缺失都会 fail closed。
"""

import argparse
import json
import sys
from pathlib import Path

from backend.app.core.config import settings
from backend.app.db.session import SessionLocal
from backend.app.eval.gating import GateValidationError
from backend.app.eval.gating import build_gate_candidate
from backend.app.eval.gating import build_holdout_manifest
from backend.app.eval.gating import build_model_artifact_identity
from backend.app.eval.gating import build_regression_record
from backend.app.eval.gating import build_runtime_source_identity
from backend.app.eval.gating import collect_repository_identity
from backend.app.eval.gating import ensure_external_artifact_dir
from backend.app.eval.gating import finalize_gate
from backend.app.eval.gating import sha256_file
from backend.app.eval.gating import summarize_run_identities
from backend.app.eval.gating import write_json
from backend.app.eval.labeled_dataset import build_labeled_eval
from backend.app.eval.run_eval import _write_report
from backend.app.eval.runner import assert_no_regression
from backend.app.eval.runner import baseline_from_report
from backend.app.services.cache.llm_cache import PROMPT_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def main(argv=None):
    parser = argparse.ArgumentParser(description="QWK 留出集评估（系统分 vs 教师分）")
    parser.add_argument("--rubric-id")
    parser.add_argument("--papers-dir")
    parser.add_argument("--scores", help="教师成绩表 .xlsx 或 .csv（文件名+总分+各评分项code列）")
    parser.add_argument("--baseline", default=None, help="基线 JSON 路径（默认 storage/eval/baseline.json）")
    parser.add_argument(
        "--release-gate",
        action="store_true",
        help="启用 PGS-8 fail-closed 发布门禁；私有产物必须写到仓库外",
    )
    parser.add_argument("--evaluation-id", help="发布评估唯一 ID")
    parser.add_argument("--dataset-id", help="冻结留出集 ID")
    parser.add_argument("--dataset-version", help="冻结留出集版本")
    parser.add_argument("--artifact-dir", help="仓库外私有归档目录")
    model_group = parser.add_mutually_exclusive_group()
    model_group.add_argument("--model-artifact", help="本地模型制品路径（计算 SHA-256）")
    model_group.add_argument(
        "--immutable-model-revision",
        help="云 provider 的不可变模型 revision（不可使用滚动 alias）",
    )
    parser.add_argument(
        "--approval",
        help="维护者批准文件；与 --finalize-candidate 一起使用",
    )
    parser.add_argument(
        "--finalize-candidate",
        help="离线批准已有 candidate；不会重新运行模型或读取真实数据",
    )
    parser.add_argument(
        "--public-record-output",
        help="可安全提交仓库的候选/最终门禁记录输出路径",
    )
    parser.add_argument(
        "--regression-reference",
        help="已批准且通过的发布门禁记录；按其中容差检查本次 candidate",
    )
    args = parser.parse_args(argv)

    if args.finalize_candidate:
        try:
            return _finalize_candidate(args)
        except GateValidationError as exc:
            print("\n❌ 发布门禁批准不合法：%s" % exc)
            return 2
    missing_inputs = [
        flag
        for flag, value in (
            ("--rubric-id", args.rubric_id),
            ("--papers-dir", args.papers_dir),
            ("--scores", args.scores),
        )
        if not value
    ]
    if missing_inputs:
        parser.error("the following arguments are required: %s" % ", ".join(missing_inputs))
    if args.release_gate:
        try:
            return _run_release_gate(args)
        except GateValidationError as exc:
            print("\n❌ 发布门禁输入不合法：%s" % exc)
            return 2

    baseline_path = Path(args.baseline) if args.baseline else (settings.STORAGE_ROOT / "eval" / "baseline.json")

    with SessionLocal() as db:
        report = build_labeled_eval(db, args.rubric_id, args.papers_dir, args.scores)

    report_path = _write_report(report)
    _print_summary(report, report_path)

    issues = []
    if baseline_path.exists():
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print("\n❌ 基线文件损坏，无法解析：%s\n  %s" % (baseline_path, exc))
            return 1
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


def _run_release_gate(args):
    if args.approval:
        raise GateValidationError(
            "--approval is only valid with --finalize-candidate"
        )
    required = {
        "--evaluation-id": args.evaluation_id,
        "--dataset-id": args.dataset_id,
        "--dataset-version": args.dataset_version,
        "--artifact-dir": args.artifact_dir,
    }
    missing = [flag for flag, value in required.items() if not value]
    if missing:
        raise GateValidationError(
            "release gate requires %s" % ", ".join(missing)
        )
    if not args.model_artifact and not args.immutable_model_revision:
        raise GateValidationError(
            "release gate requires --model-artifact or --immutable-model-revision"
        )
    _validate_release_environment(args)

    artifact_dir = ensure_external_artifact_dir(args.artifact_dir, PROJECT_ROOT)
    private_manifest, sample_ids = build_holdout_manifest(
        papers_dir=args.papers_dir,
        scores_path=args.scores,
        dataset_id=args.dataset_id,
        dataset_version=args.dataset_version,
    )
    private_manifest_path = write_json(
        artifact_dir / "private_holdout_manifest.json",
        private_manifest,
    )

    with SessionLocal() as db:
        report = build_labeled_eval(
            db,
            args.rubric_id,
            args.papers_dir,
            args.scores,
            sample_ids_by_filename=sample_ids,
        )
    private_report_path = write_json(
        artifact_dir / "private_eval_report.json",
        report,
    )
    _print_summary(report, private_report_path)

    run_identity, run_identity_issues = summarize_run_identities(report)
    model_identity = build_model_artifact_identity(
        provider=run_identity.get("model_provider") or "",
        model_name=run_identity.get("model_name") or "",
        model_version=run_identity.get("model_version") or "",
        artifact_path=args.model_artifact,
        immutable_revision=args.immutable_model_revision,
    )
    prompt_identity = build_runtime_source_identity(
        repo_root=PROJECT_ROOT,
        provider=model_identity["provider"],
        prompt_version=PROMPT_VERSION,
    )
    repository_identity = collect_repository_identity(PROJECT_ROOT)
    candidate = build_gate_candidate(
        evaluation_id=args.evaluation_id,
        report=report,
        dataset_identity=private_manifest["public_identity"],
        run_identity=run_identity,
        run_identity_issues=run_identity_issues,
        anchors_identity=report.get("anchors_identity") or {},
        model_identity=model_identity,
        prompt_identity=prompt_identity,
        repository_identity=repository_identity,
        private_artifacts={
            "location": "external_private_not_in_repository",
            "private_manifest_sha256": sha256_file(private_manifest_path),
            "private_report_sha256": sha256_file(private_report_path),
        },
        artifact_directory_external=True,
    )
    candidate_path = write_json(
        artifact_dir / "gate_candidate.json",
        candidate,
    )
    regression_record = None
    if args.regression_reference:
        try:
            approved_reference = json.loads(
                Path(args.regression_reference).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise GateValidationError(
                "regression reference cannot be read as JSON"
            ) from exc
        regression_record = build_regression_record(
            candidate=candidate,
            approved_record=approved_reference,
        )
        write_json(artifact_dir / "gate_regression.json", regression_record)
    if args.public_record_output:
        write_json(args.public_record_output, regression_record or candidate)

    print("\n发布门禁候选：%s" % candidate_path)
    print("candidate_sha256=%s" % candidate["candidate_sha256"])
    if regression_record is not None:
        if regression_record["regression_passed"]:
            print("✓ 已批准基线回归通过。")
            return 0
        print("❌ 已批准基线回归未通过：")
        for issue in regression_record["issues"]:
            print("  -", issue)
        return 1
    print("⏸ 等待人工批准：隐私复核、锚点排除、基线与回退阈值。")
    return 3


def _finalize_candidate(args):
    if not args.approval:
        raise GateValidationError(
            "--finalize-candidate requires --approval"
        )
    try:
        candidate = json.loads(
            Path(args.finalize_candidate).read_text(encoding="utf-8")
        )
        approval = json.loads(Path(args.approval).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateValidationError(
            "candidate and approval must be readable JSON files"
        ) from exc
    final = finalize_gate(candidate, approval)
    final_path = Path(args.finalize_candidate).with_name("gate_final.json")
    write_json(final_path, final)
    if args.public_record_output:
        write_json(args.public_record_output, final)
    if final.get("gate_passed"):
        print("✓ M1 真实发布门禁通过；记录仍不会自动切换生产默认 Core。")
        print("最终记录：%s" % final_path)
        return 0
    print("❌ M1 真实发布门禁未通过：")
    for issue in final.get("missing_or_invalid") or []:
        print("  -", issue)
    print("最终记录：%s" % final_path)
    return 1


def _validate_release_environment(args):
    if (settings.LLM_PROVIDER or "mock").lower() == "mock":
        raise GateValidationError("release gate requires a real LLM provider")
    if settings.LLM_FALLBACK_TO_MOCK:
        raise GateValidationError(
            "release gate requires LLM_FALLBACK_TO_MOCK=false"
        )
    if settings.LLM_DEBUG_LOG_ENABLED:
        raise GateValidationError(
            "release gate requires LLM_DEBUG_LOG_ENABLED=false"
        )
    if settings.SCORING_ENGINE_MODE != "core":
        raise GateValidationError(
            "release gate requires command-scoped SCORING_ENGINE_MODE=core"
        )
    _require_external_existing(args.papers_dir, "papers_dir")
    _require_external_existing(args.scores, "scores")
    ensure_external_artifact_dir(settings.STORAGE_ROOT, PROJECT_ROOT)
    if args.model_artifact:
        _require_external_existing(args.model_artifact, "model_artifact")

    database_url = str(settings.DATABASE_URL)
    if database_url.startswith("sqlite"):
        if ":///" not in database_url:
            raise GateValidationError("release-gate SQLite URL is malformed")
        raw_path = database_url.split(":///", 1)[1].split("?", 1)[0]
        if raw_path == ":memory:":
            raise GateValidationError(
                "release gate cannot use an in-memory database"
            )
        database_path = Path(raw_path)
        if not database_path.is_absolute():
            database_path = (Path.cwd() / database_path).resolve()
        _require_external_path(database_path, "database")


def _require_external_existing(path, label):
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise GateValidationError("%s path does not exist" % label)
    _require_external_path(resolved, label)


def _require_external_path(path, label):
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError:
        return
    raise GateValidationError("%s must be outside the repository" % label)


def _print_summary(report, report_path):
    print("样本 n=%s（数据集 %s，错误 %s）" % (report.get("n"), report.get("dataset_size"), len(report.get("errors") or [])))
    print("QWK=%s  MAE=%s  RMSE=%s" % (report.get("qwk"), report.get("mae"), report.get("rmse")))
    print("同档一致率=%s  相邻档一致率=%s" % (report.get("exact_grade_agreement"), report.get("adjacent_grade_agreement")))
    print(
        "复核率=%s  阻断率=%s"
        % (report.get("review_rate"), report.get("blocked_rate"))
    )
    per_criterion = report.get("per_criterion") or {}
    if per_criterion:
        print("逐维度 bias（>0 偏宽 / <0 偏严）：")
        for code, stats in per_criterion.items():
            print("  %s: bias=%.3f mae=%.3f n=%s" % (code, stats["bias"], stats["mae"], stats["n"]))
    print("报告：%s" % report_path)


if __name__ == "__main__":
    sys.exit(main())
