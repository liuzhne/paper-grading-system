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
from backend.app.eval.gating import assert_public_record_safe
from backend.app.eval.gating import build_gate_candidate
from backend.app.eval.gating import build_holdout_manifest
from backend.app.eval.gating import build_model_artifact_identity
from backend.app.eval.gating import build_regression_record
from backend.app.eval.gating import build_runtime_source_identity
from backend.app.eval.gating import collect_repository_identity
from backend.app.eval.gating import ensure_external_artifact_dir
from backend.app.eval.gating import evaluation_sha256
from backend.app.eval.gating import finalize_gate
from backend.app.eval.gating import normalize_difference_explanations
from backend.app.eval.gating import sha256_file
from backend.app.eval.gating import summarize_run_identities
from backend.app.eval.gating import write_json
from backend.app.eval.labeled_dataset import build_labeled_eval
from backend.app.eval.release_preflight import validate_release_rubric_and_scores
from backend.app.eval.run_eval import _write_report
from backend.app.eval.runner import assert_no_regression
from backend.app.eval.runner import baseline_from_report
from backend.app.services.cache.llm_cache import PROMPT_VERSION
from backend.app.services import release_gates


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
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="仅验证发布数据和 RubricVersion；不运行模型、不生成门禁候选",
    )
    parser.add_argument(
        "--gate-profile-id",
        help="从数据库加载用户冻结的 Rubric/dataset/model/anchors/门禁策略关联",
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
    parser.add_argument(
        "--difference-explanations",
        help="GATE-02 Core 候选逐评分项差异说明 JSON（公开安全字段）",
    )
    parser.add_argument(
        "--gate03-evidence",
        help=(
            "GATE-03 观测、获批基线/M5 比较及逐样本报告 hash 的"
            "公开安全 JSON"
        ),
    )
    args = parser.parse_args(argv)

    if args.preflight_only and not args.release_gate:
        parser.error("--preflight-only is only valid with --release-gate")
    if args.gate_profile_id and not args.release_gate:
        parser.error("--gate-profile-id is only valid with --release-gate")
    if args.preflight_only and args.finalize_candidate:
        parser.error("--preflight-only cannot be used with --finalize-candidate")
    if args.gate_profile_id and args.finalize_candidate:
        parser.error("--gate-profile-id cannot be used with --finalize-candidate")
    if args.finalize_candidate:
        try:
            return _finalize_candidate(args)
        except GateValidationError as exc:
            print("\n❌ 发布门禁批准不合法：%s" % exc)
            return 2
    if args.release_gate and args.gate_profile_id:
        try:
            _apply_release_gate_profile(args)
        except GateValidationError as exc:
            print("\n❌ 发布门禁 profile 不合法：%s" % exc)
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
            if args.preflight_only:
                return _run_release_preflight(args)
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


def _run_release_preflight(args):
    if args.approval:
        raise GateValidationError(
            "--approval is only valid with --finalize-candidate"
        )
    required = {
        "--dataset-id": args.dataset_id,
        "--dataset-version": args.dataset_version,
    }
    missing = [flag for flag, value in required.items() if not value]
    if missing:
        raise GateValidationError(
            "release preflight requires %s" % ", ".join(missing)
        )
    _validate_release_input_paths(args)

    with SessionLocal() as db:
        rubric_version = validate_release_rubric_and_scores(
            db,
            args.rubric_id,
            args.scores,
        )

    private_manifest, _sample_ids = build_holdout_manifest(
        papers_dir=args.papers_dir,
        scores_path=args.scores,
        dataset_id=args.dataset_id,
        dataset_version=args.dataset_version,
    )
    gate_profile = _validate_profile_preflight(
        args,
        rubric_version,
        private_manifest["public_identity"],
    )
    record = {
        "schema": "paper-grading/release-preflight@1",
        "status": "ready_for_real_model_run",
        "gate_passed": False,
        "gating_eligible": False,
        "dataset": private_manifest["public_identity"],
        "rubric": {
            "rubric_id": args.rubric_id,
            "rubric_version_id": rubric_version.id,
            "rubric_version_hash": rubric_version.version_hash,
            "rubric_hash_scheme": rubric_version.hash_scheme,
        },
        "validated": [
            "external_release_paths",
            "complete_supported_paper_coverage",
            "teacher_truth_totals_and_t01_t06",
            "published_immutable_rubric_version",
        ],
        "remaining_requirements": [
            "real_immutable_model_candidate",
            "anchor_holdout_exclusion_confirmation",
            "human_approval_and_thresholds",
        ],
    }
    if gate_profile is not None:
        record["gate_profile"] = {
            "id": gate_profile.id,
            "profile_hash": gate_profile.profile_hash,
        }
    assert_public_record_safe(record)
    print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


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

    # Resolve and validate the exact formal rubric before creating artifacts or
    # invoking the model. The evaluation batch is then locked to this identity.
    with SessionLocal() as db:
        rubric_version = validate_release_rubric_and_scores(
            db,
            args.rubric_id,
            args.scores,
        )
        rubric_version_id = rubric_version.id

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
            rubric_version_id=rubric_version_id,
        )
    if args.difference_explanations:
        report["difference_explanations"] = _load_difference_explanations(
            args.difference_explanations
        )
    elif getattr(args, "gate_key", None) == "GATE-02" and args.regression_reference:
        raise GateValidationError(
            "GATE-02 regression candidate requires --difference-explanations"
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
    candidate = _apply_gate03_evidence(args, candidate)
    candidate_path = write_json(
        artifact_dir / "gate_candidate.json",
        candidate,
    )
    database_run = _register_db_gate_candidate(args, candidate)
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
    if database_run is not None:
        print("database_gate_run_id=%s" % database_run.id)
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


def _apply_release_gate_profile(args):
    with SessionLocal() as db:
        try:
            profile = release_gates.resolve_profile(db, args.gate_profile_id)
        except release_gates.ReleaseGateServiceError as exc:
            raise GateValidationError(str(exc)) from exc
        frozen = {
            "rubric_id": profile.rubric_id,
            "dataset_id": profile.dataset_identity.get("dataset_id"),
            "dataset_version": profile.dataset_identity.get("dataset_version"),
        }
        for attribute, expected in frozen.items():
            supplied = getattr(args, attribute, None)
            flag = attribute.replace("_", "-")
            if supplied not in (None, expected):
                raise GateValidationError(
                    "%s does not match database gate profile" % flag
                )
            setattr(args, attribute, expected)
        args.gate_key = profile.gate_key

        model_identity = profile.model_identity or {}
        method = model_identity.get("identity_method")
        if method == "provider_immutable_revision":
            if args.model_artifact:
                raise GateValidationError(
                    "model-artifact conflicts with database gate profile"
                )
            revision = model_identity.get("immutable_revision")
            if args.immutable_model_revision not in (None, revision):
                raise GateValidationError(
                    "immutable-model-revision does not match database gate profile"
                )
            args.immutable_model_revision = revision
        elif method == "artifact_sha256":
            if args.immutable_model_revision:
                raise GateValidationError(
                    "immutable-model-revision conflicts with database gate profile"
                )
        else:
            raise GateValidationError(
                "database gate profile has unsupported model identity"
            )


def _validate_profile_preflight(args, rubric_version, dataset_identity):
    if not args.gate_profile_id:
        return None
    with SessionLocal() as db:
        try:
            profile = release_gates.resolve_profile(db, args.gate_profile_id)
        except release_gates.ReleaseGateServiceError as exc:
            raise GateValidationError(str(exc)) from exc
        if profile.rubric_version_id != rubric_version.id:
            raise GateValidationError(
                "published RubricVersion does not match database gate profile"
            )
        if profile.dataset_identity != dataset_identity:
            raise GateValidationError(
                "holdout identity does not match database gate profile"
            )
        return profile


def _register_db_gate_candidate(args, candidate):
    if not args.gate_profile_id:
        return None
    try:
        with SessionLocal() as db:
            return release_gates.register_run(
                db,
                args.gate_profile_id,
                candidate,
            )
    except release_gates.ReleaseGateServiceError as exc:
        raise GateValidationError(str(exc)) from exc


def _load_difference_explanations(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateValidationError(
            "difference_explanations must be readable JSON"
        ) from exc
    return normalize_difference_explanations(value)


def _apply_gate03_evidence(args, candidate):
    evidence_path = getattr(args, "gate03_evidence", None)
    gate_key = getattr(args, "gate_key", None)
    if gate_key != "GATE-03":
        if evidence_path:
            raise GateValidationError(
                "--gate03-evidence is only valid for a GATE-03 database profile"
            )
        return candidate
    if not evidence_path:
        raise GateValidationError(
            "GATE-03 requires --gate03-evidence before candidate registration"
        )
    try:
        evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateValidationError(
            "--gate03-evidence must be readable JSON"
        ) from exc
    if not isinstance(evidence, dict):
        raise GateValidationError("--gate03-evidence must contain an object")

    result = dict(candidate)
    result["execution"] = {
        "mode": "formal_release",
        "dataset_class": "external_private_holdout",
        "model_class": "immutable_production",
    }
    result["production_default_switch_authorized"] = False
    result["gate03_evidence"] = evidence
    result.pop("candidate_sha256", None)
    assert_public_record_safe(result)
    result["candidate_sha256"] = evaluation_sha256(result)
    return result


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
    _validate_release_input_paths(args)
    ensure_external_artifact_dir(settings.STORAGE_ROOT, PROJECT_ROOT)
    if args.model_artifact:
        _require_external_existing(args.model_artifact, "model_artifact")


def _validate_release_input_paths(args):
    _require_external_existing(args.papers_dir, "papers_dir")
    _require_external_existing(args.scores, "scores")

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
        "复核率=%s  阻断率=%s  无效证据项率=%s"
        % (
            report.get("review_rate"),
            report.get("blocked_rate"),
            report.get("invalid_evidence_rate"),
        )
    )
    per_criterion = report.get("per_criterion") or {}
    if per_criterion:
        print("逐维度 bias（>0 偏宽 / <0 偏严）：")
        for code, stats in per_criterion.items():
            print("  %s: bias=%.3f mae=%.3f n=%s" % (code, stats["bias"], stats["mae"], stats["n"]))
    identity = report.get("evaluation_identity") or {}
    if identity:
        print(
            "评估身份：RubricVersion=%s  Profile=%s/%s"
            % (
                identity.get("rubric_version_id"),
                identity.get("business_profile_key"),
                identity.get("business_profile_version"),
            )
        )
        print(
            "Policy=%s  GradeScale=%s  Rounding=%s"
            % (
                identity.get("policy_hash"),
                identity.get("grade_scale_sha256"),
                identity.get("rounding"),
            )
        )
        print(
            "Plan=%s  Document=%s  Runtime=%s  Models=%s"
            % (
                identity.get("execution_plan_hashes"),
                identity.get("document_snapshot_hashes"),
                identity.get("runtime_identity_hashes"),
                identity.get("models"),
            )
        )
    print("报告：%s" % report_path)


if __name__ == "__main__":
    sys.exit(main())
