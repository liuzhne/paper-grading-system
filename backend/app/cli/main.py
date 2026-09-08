"""`pgs` 命令行端：本地零服务，复用与 Web 同一套评分内核（services/*）。

子命令：init / check / import / rubrics / score / report / export / eval。
所有需库的命令先 `_bootstrap`（注入本地 sqlite+storage 到 settings 并建表），再用自建会话调内核。
"""

import json
import shutil
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import List
from typing import Optional

import typer

from backend.app.cli import db as clidb
from backend.app.cli import render
from backend.app.core.config import settings

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="毕业论文智能评分系统 CLI（本地零服务，复用 Web 同款评分内核）。",
)

_DB_OPT = typer.Option(None, "--db", help="本地 sqlite 路径（默认 ~/.paper-grading/cli.db）")
_STORAGE_OPT = typer.Option(None, "--storage", help="本地存储根目录（默认 ~/.paper-grading/storage）")


# ---- 公共 ----
def _bootstrap(db, storage):
    db_path = db or (clidb.DEFAULT_HOME / "cli.db")
    storage_path = storage or (clidb.DEFAULT_HOME / "storage")
    db_url = clidb.configure(db_path, storage_path)
    clidb.ensure_schema()
    return db_url


def _safe_scorer():
    from backend.app.services.llm.factory import get_llm_scorer

    try:
        return get_llm_scorer()
    except Exception:
        return None


def _cli_file_import_command(*, name, version, description, rules, template, scorer):
    """Build the versioned M4 command consumed by the shared import pipeline."""
    from backend.app.services.rubric_import import pipeline as rubric_pipeline

    provider = None
    model_name = None
    if scorer is not None:
        provider = getattr(scorer, "provider_name", None) or getattr(
            scorer, "provider", None
        )
        model_name = getattr(scorer, "model_name", None)
    return {
        "schema_version": rubric_pipeline.IMPORT_SCHEMA_VERSION,
        "source_kind": "file_import",
        "rubric": {
            "name": name,
            "version": version,
            "description": description,
            "business_profile_key": "thesis",
            "workflow_profile": "template_driven" if template else "manual_json",
        },
        "files": {
            "rules_file_name": Path(rules).name,
            "template_file_name": Path(template).name if template else None,
        },
        "compiler": {
            "parser_version": "rubric-file-parser@2",
            "compiler_version": "atomic-rule-compiler@1",
            "prompt_version": "rubric-compilation-prompt@2",
            "model_provider": provider,
            "model_name": model_name,
            "sampling_params": {},
        },
        "version": {"hash_scheme": "rubric-content-v2"},
    }


def _prepare_cli_file_import(*, rules, template, name, version, description, scorer):
    """Parse/compile files without opening or depending on a database session."""
    from backend.app.services.rubric_import import pipeline as rubric_pipeline

    rules_bytes = Path(rules).read_bytes()
    template_bytes = Path(template).read_bytes() if template else None
    prepared = rubric_pipeline.prepare_file_import(
        command=_cli_file_import_command(
            name=name,
            version=version,
            description=description,
            rules=rules,
            template=template,
            scorer=scorer,
        ),
        rules_bytes=rules_bytes,
        template_bytes=template_bytes,
        scorer=scorer,
    )
    return prepared


def _resolve_cli_frozen_version(session, rubric):
    """Resolve the one consistently frozen published version for CLI scoring."""

    from sqlalchemy import select

    from backend.app.db.models import RubricCompilation, RubricVersion

    versions = session.scalars(
        select(RubricVersion).where(RubricVersion.rubric_id == rubric.id)
    ).all()
    if not versions:
        return None
    eligible = []
    for version in versions:
        compilation = session.get(RubricCompilation, version.compilation_id)
        if (
            rubric.status == "published"
            and rubric.published_at is not None
            and compilation is not None
            and compilation.rubric_id == rubric.id
            and compilation.status == "validated"
            and compilation.reviewed_by is not None
            and compilation.reviewed_at is not None
            and compilation.published_at == rubric.published_at
            and compilation.reviewed_at == compilation.published_at
            and compilation.final_version_hash == version.version_hash
        ):
            eligible.append(version)
    if len(eligible) == 1:
        return eligible[0]
    if not eligible:
        raise typer.BadParameter("正式评分标准没有一致冻结的已发布版本")
    raise typer.BadParameter("评分标准存在多个冻结发布版本，无法唯一锁定")


_PROVIDER_OPT = typer.Option(None, "--provider", help="覆盖 LLM provider：mock / local（本地私有模型）/ openai / openai_compatible")
_MODEL_OPT = typer.Option(None, "--model", help="覆盖模型名（本地模型/云模型通用）")
_BASE_URL_OPT = typer.Option(None, "--base-url", help="覆盖 LLM 端点，如本地 http://localhost:8080/v1")


def _apply_llm_overrides(provider, model, base_url):
    """用 CLI 旗标覆盖 settings 的 LLM 选择，实现云/本地一站式切换（不写 .env）。"""
    from backend.app.services.llm.factory import COMPATIBLE_PROVIDERS, LOCAL_PROVIDERS

    if provider:
        settings.LLM_PROVIDER = provider.lower()
    target = (settings.LLM_PROVIDER or "mock").lower()
    if target in LOCAL_PROVIDERS:
        if base_url:
            settings.LOCAL_LLM_BASE_URL = base_url
        if model:
            settings.LOCAL_LLM_MODEL = model
    elif target == "openai":
        if base_url:
            settings.OPENAI_BASE_URL = base_url
        if model:
            settings.OPENAI_MODEL = model
    elif target in COMPATIBLE_PROVIDERS:
        if base_url:
            settings.OPENAI_COMPATIBLE_BASE_URL = base_url
        if model:
            settings.OPENAI_COMPATIBLE_MODEL = model


def _collect_files(paths):
    files = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(x for x in path.rglob("*") if x.suffix.lower() in (".docx", ".pdf")))
        elif path.exists():
            files.append(path)
        else:
            raise typer.BadParameter("找不到文件/目录：%s" % path)
    return files


def _resolve_rubric(session, rubric, rubric_file, template):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from backend.app.db.models import Rubric

    if rubric_file:
        raise typer.BadParameter(
            "数据库评分不再隐式导入未审核规则；请先用 `pgs import` 导入并完成审核发布，"
            "或使用 `pgs score --no-db --rubric-file ...` 做无状态评分"
        )

    if rubric:
        obj = session.get(Rubric, rubric)
        rubric_id = obj.id if obj else None
        loaded = session.scalar(
            select(Rubric)
            .where((Rubric.id == rubric_id) if rubric_id else (Rubric.name == rubric))
            .options(selectinload(Rubric.criteria))
            .order_by(Rubric.created_at.desc())
        )
        if loaded is None:
            raise typer.BadParameter("找不到评分标准：%s（用 `pgs rubrics` 查看，或 `pgs import`）" % rubric)
        if loaded.status != "published":
            raise typer.BadParameter(
                "评分标准尚未发布：%s；请完成规则审核、标准送审与发布后再评分"
                % loaded.name
            )
        return loaded

    raise typer.BadParameter(
        "请用 --rubric <id|名称> 指定已发布评分标准（或先 `pgs init --seed`）；"
        "一次性文件评分请加 --no-db --rubric-file"
    )


def _run_scoring(items, fn, workers, show_progress):
    """对 items 跑 fn；workers>1 时线程池并发，否则顺序（多篇带进度条）。返回结果列表。"""
    if not items:
        return []
    if workers <= 1:
        if show_progress and len(items) > 1:
            from rich.progress import track

            return [fn(item) for item in track(items, description="评分中", console=render.console)]
        return [fn(item) for item in items]
    import concurrent.futures

    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fn, item) for item in items]
        if show_progress:
            from rich.progress import Progress

            with Progress(console=render.console, transient=True) as progress:
                task = progress.add_task("评分中", total=len(futures))
                for future in concurrent.futures.as_completed(futures):
                    out.append(future.result())
                    progress.advance(task)
        else:
            for future in concurrent.futures.as_completed(futures):
                out.append(future.result())
    return out


def _print_score_summary(results, batch_id):
    from collections import Counter

    ok_rows = [r for r in results if r["status"] == "ok"]
    if not ok_rows:
        return
    avg = sum(r["total"] for r in ok_rows) / len(ok_rows)
    need = sum(1 for r in ok_rows if r.get("need_review"))
    grades = Counter(r.get("grade") or "?" for r in ok_rows)
    render.info(
        "成功 %d/%d    平均分 %.2f    需复核 %d    失败 %d"
        % (len(ok_rows), len(results), avg, need, len(results) - len(ok_rows))
    )
    render.hint("等级分布：%s" % "  ".join("%s×%d" % (grade, count) for grade, count in grades.most_common()))
    render.hint("逐项明细：pgs show <run_id>    （列任务：pgs runs --batch %s）" % batch_id)


def _score_stateless(files, rubric_file, template, mock, workers, report_dir, as_json):
    """无状态评分：从 --rubric-file + 论文文件直接组装 inputs → compute，全程零 DB、零落库。"""
    if not rubric_file:
        raise typer.BadParameter("--no-db 需要 --rubric-file 指定评分标准（无 DB 可查）")
    if report_dir:
        raise typer.BadParameter("--no-db 不支持 --report-dir（报告需 DB）；改用普通模式，或用 --json 取明细")

    from backend.app.services.document_parser.parser import parse_document
    from backend.app.services.rubric_import.parser import parse_rubric_files
    from backend.app.services.scoring.engine import collect_inputs_from_parsed
    from backend.app.services.scoring.engine import compute_scoring

    if mock:
        from backend.app.services.llm.mock import MockLLMScorer

        scorer = MockLLMScorer()
    else:
        scorer = _safe_scorer()
        if scorer is None:
            render.error("未配置可用 LLM（加 --mock，或在 .env 配置真实 provider）")
            return 2

    try:
        imported = parse_rubric_files(
            rules_bytes=Path(rubric_file).read_bytes(),
            template_bytes=Path(template).read_bytes() if template else None,
            scorer=scorer,
        )
    except ValueError as exc:
        render.error("评分标准解析失败：%s" % exc)
        return 2
    rubric_label = "%s（%d 项，无状态）" % (Path(rubric_file).stem, len(imported.criteria))

    def _score_one(path):
        try:
            parsed_obj = parse_document(str(path))
            inputs = collect_inputs_from_parsed(
                parsed_obj,
                imported.criteria,
                rubric_total_score=imported.total_score,
                paper_path=str(path),
                format_spec=imported.format_spec,
            )
            result = compute_scoring(inputs, scorer)
            return {
                "file": path.name,
                "status": "ok",
                "total": float(result.final_total or 0),
                "grade": result.grade,
                "need_review": bool(result.need_review),
                "tokens": result.total_tokens,
                "items": [
                    {
                        "criterion_id": d["criterion_id"],
                        "score": d["ai_score"],
                        "max": d["max_score"],
                        "evidence_sufficient": d["evidence_sufficient"],
                        "need_review": d["need_manual_review"],
                    }
                    for d in result.items
                ],
            }
        except Exception as exc:  # 单篇失败不中断整批
            return {"file": path.name, "status": "失败", "error": str(exc)}

    results = _run_scoring(list(files), _score_one, workers, show_progress=not as_json)
    results.sort(key=lambda item: item["file"])
    any_failed = any(r["status"] != "ok" for r in results)

    if as_json:
        render.dump_json({"rubric": rubric_label, "stateless": True, "results": results})
        return 1 if any_failed else 0

    render.info("评分标准：%s    （无状态，未写任何 DB）" % rubric_label)
    rows = [
        (
            r["file"],
            r.get("total", "-"),
            r.get("grade", "-"),
            ("是" if r.get("need_review") else "否") if r["status"] == "ok" else "-",
            r.get("tokens", "-"),
            r["status"] + (("：" + str(r["error"])) if r.get("error") else ""),
        )
        for r in results
    ]
    render.render_table(
        "评分结果（无状态）",
        ["文件", "总分", "等级", "需复核", "Token", "状态"],
        rows,
        style_fn=lambda row: None if str(row[5]) == "ok" else "red",
    )
    ok_rows = [r for r in results if r["status"] == "ok"]
    if ok_rows:
        from collections import Counter

        avg = sum(r["total"] for r in ok_rows) / len(ok_rows)
        need = sum(1 for r in ok_rows if r.get("need_review"))
        grades = Counter(r.get("grade") or "?" for r in ok_rows)
        render.info(
            "成功 %d/%d    平均分 %.2f    需复核 %d    失败 %d"
            % (len(ok_rows), len(results), avg, need, len(results) - len(ok_rows))
        )
        render.hint(
            "等级分布：%s    （无状态：不落库、无 run_id；--json 取逐项明细）"
            % "  ".join("%s×%d" % (grade, count) for grade, count in grades.most_common())
        )
    return 1 if any_failed else 0


def _score_stateless_profiled(
    files,
    rubric_file,
    template,
    mock,
    workers,
    report_dir,
    as_json,
    *,
    profile_key,
    profile_version,
):
    """Run the legacy file rubric through an explicit Profile/Core request."""

    if profile_key != "thesis":
        raise typer.BadParameter(
            "--no-db currently supports only the explicit thesis Profile; "
            "other Profiles require a published RubricVersion in local SQLite"
        )
    if not rubric_file:
        raise typer.BadParameter("--no-db 需要 --rubric-file 指定评分标准（无 DB 可查）")
    if report_dir:
        raise typer.BadParameter(
            "--no-db 不支持 --report-dir（报告需 DB）；改用普通模式，或用 --json 取明细"
        )

    from backend.app.services.document_parser.parser import parse_document
    from backend.app.services.rubric_import.parser import parse_rubric_files
    from backend.app.services.scoring.adapters.legacy_rubric import (
        LegacyRubricAdapter,
    )
    from backend.app.services.scoring.core.canonical import canonical_sha256
    from backend.app.services.scoring.core.contracts import ScoringRequest
    from backend.app.services.scoring.core.engine import score_submission
    from backend.app.services.scoring.core.identity import hash_source_artifact
    from backend.app.services.scoring.core.identity import (
        scoring_request_idempotency_projection,
    )
    from backend.app.services.scoring.core.policy import (
        build_corrected_thesis_policy,
    )
    from backend.app.services.scoring.core.policy import (
        validate_weight_configuration,
    )
    from backend.app.services.scoring.engine import _build_legacy_execution_plan
    from backend.app.services.scoring.profiles.registry import get_profile

    if mock:
        from backend.app.services.llm.mock import MockLLMScorer

        scorer = MockLLMScorer()
    else:
        scorer = _safe_scorer()
        if scorer is None:
            render.error("未配置可用 LLM（加 --mock，或在 .env 配置真实 provider）")
            return 2

    profile = get_profile(
        profile_key=profile_key,
        profile_version=profile_version,
    )
    try:
        imported = parse_rubric_files(
            rules_bytes=Path(rubric_file).read_bytes(),
            template_bytes=Path(template).read_bytes() if template else None,
            scorer=scorer,
        )
    except ValueError as exc:
        render.error("评分标准解析失败：%s" % exc)
        return 2
    weights = validate_weight_configuration(
        imported.criteria,
        total_score=imported.total_score,
    )
    policy = build_corrected_thesis_policy(
        imported.total_score,
        weights.mode,
    )
    adapter = LegacyRubricAdapter()
    rubric_snapshot = adapter.adapt(
        rubric={
            "name": Path(rubric_file).stem,
            "status": "published",
            "total_score": imported.total_score,
        },
        criteria=imported.criteria,
        policy_snapshot=policy.to_mapping(),
        business_profile_key=profile.profile_key,
        compilation_rows=(),
    )
    registry = profile.build_checker_registry()
    plan = _build_legacy_execution_plan(
        rubric_snapshot=rubric_snapshot,
        profile=profile,
        registry=registry,
        compatibility_nodes=adapter.adapt_compatibility_nodes(
            criteria=imported.criteria,
            rubric_source_kind="legacy_unversioned",
        ),
    )

    def _score_one(path):
        try:
            raw_bytes = Path(path).read_bytes()
            artifact_hash = hash_source_artifact(raw_bytes)
            parsed = parse_document(str(path)).to_dict()
            paper = SimpleNamespace(
                id="stateless:" + artifact_hash,
                title=parsed.get("title") or path.stem,
                parse_quality=parsed.get("parse_quality"),
                student_id=None,
                student_name=None,
                department=None,
                major=None,
                advisor=None,
            )
            snapshots = profile.adapt_paper(
                paper=paper,
                parsed=parsed,
                source_artifact_hash=artifact_hash,
            )
            request_mapping = {
                "schema_version": "scoring-request@2",
                "submission": snapshots.submission.to_mapping(),
                "document": snapshots.document.to_mapping(),
                "plan": plan.to_mapping(),
                "runtime_identity": profile.build_runtime_identity(scorer),
                "rescore_generation": 0,
            }
            request_mapping["idempotency_key"] = canonical_sha256(
                scoring_request_idempotency_projection(request_mapping)
            )
            outcome = score_submission(
                request=ScoringRequest.from_mapping(request_mapping),
                checker_registry=registry,
                llm_runtime=profile.build_llm_runtime(scorer),
                profile=profile,
            ).to_mapping()
            identity = {
                **deepcopy(outcome["request_identity"]),
                "profile_version": profile.profile_version,
                "runtime_identity": deepcopy(outcome["audit_identity"]),
            }
            return {
                "file": path.name,
                "status": "ok" if outcome["status"] == "completed" else outcome["status"],
                "total": (
                    None
                    if outcome["final_total"] is None
                    else float(outcome["final_total"])
                ),
                "grade": outcome["grade"],
                "need_review": bool(outcome["review_issues"]),
                "tokens": 0,
                "items": deepcopy(outcome["criterion_outcomes"]),
                "identity": identity,
            }
        except Exception as exc:
            return {"file": path.name, "status": "失败", "error": str(exc)}

    results = _run_scoring(list(files), _score_one, workers, show_progress=not as_json)
    results.sort(key=lambda item: item["file"])
    any_failed = any(item["status"] != "ok" for item in results)
    payload = {
        "contract": "scoring-core@1",
        "rubric": "%s（%d 项，无状态）"
        % (Path(rubric_file).stem, len(imported.criteria)),
        "stateless": True,
        "profile": {
            "key": profile.profile_key,
            "version": profile.profile_version,
        },
        "results": results,
    }
    if as_json:
        render.dump_json(payload)
    else:
        render.info(
            "Profile：%s / %s（无状态 Core）"
            % (profile.profile_key, profile.profile_version)
        )
        render.render_table(
            "评分结果（无状态 Core）",
            ["文件", "总分", "等级", "需复核", "状态"],
            [
                (
                    item["file"],
                    item.get("total", "-"),
                    item.get("grade", "-"),
                    "是" if item.get("need_review") else "否",
                    item["status"],
                )
                for item in results
            ],
        )
    return 1 if any_failed else 0


def _score_profiled(
    *,
    files,
    rubric,
    rubric_file,
    template,
    mock,
    workers,
    report_dir,
    as_json,
    db,
    storage,
    profile_key,
    profile_version,
    metadata,
):
    """Local SQLite v2 Submission → Profile → Core scoring flow."""

    _bootstrap(db, storage)
    from backend.app.schemas.submission import EvaluationBatchCreate
    from backend.app.services.dev_user import ensure_dev_user
    from backend.app.services.report.generic_export import build_run_export_v2
    from backend.app.services.report.generic_generator import generate_report_v2
    from backend.app.services.scoring.profiles.registry import get_profile
    from backend.app.services.scoring.profiles.registry import get_profile_by_key
    from backend.app.services.submissions.lifecycle import create_evaluation_batch
    from backend.app.services.submissions.lifecycle import ingest_submission
    from backend.app.services.submissions.lifecycle import score_generic_submission

    selected = (
        get_profile(
            profile_key=profile_key,
            profile_version=profile_version,
        )
        if profile_version
        else get_profile_by_key(profile_key)
    )
    exact_version = selected.profile_version
    scorer = None
    if mock:
        from backend.app.services.llm.mock import MockLLMScorer

        scorer = MockLLMScorer()
    else:
        scorer = _safe_scorer()
        if scorer is None:
            render.error("未配置可用 LLM（加 --mock，或在 .env 配置真实 provider）")
            return 2

    submissions = []
    with clidb.cli_session() as session:
        user = ensure_dev_user(session)
        rub = _resolve_rubric(session, rubric, rubric_file, template)
        version = _resolve_cli_frozen_version(session, rub)
        if version is None:
            raise typer.BadParameter(
                "显式 Profile 评分需要唯一、持续一致发布的 RubricVersion"
            )
        if version.business_profile_key != profile_key:
            raise typer.BadParameter(
                "评分标准 RubricVersion 与 --profile 不匹配"
            )
        batch = create_evaluation_batch(
            session,
            EvaluationBatchCreate(
                name="CLI-v2-%s" % datetime.now().strftime("%Y%m%d-%H%M%S"),
                rubric_version_id=version.id,
                business_profile_key=profile_key,
                business_profile_version=exact_version,
            ),
            creator_id=user.id,
        )
        rubric_label = "%s / %s" % (rub.name, rub.version)
        for path in files:
            try:
                submission, _snapshot = ingest_submission(
                    session,
                    evaluation_batch_id=batch.id,
                    file_name=path.name,
                    uploaded_media_type=None,
                    raw_bytes=path.read_bytes(),
                    metadata=deepcopy(metadata),
                    creator_id=user.id,
                )
                submissions.append((path.name, path.stem, submission.id))
            except Exception as exc:
                session.rollback()
                submissions.append((path.name, path.stem, None, str(exc)))
        batch_id = batch.id
        rubric_version_id = version.id

    def _score_one(item):
        if len(item) == 4:
            return {"file": item[0], "status": "失败", "error": item[3]}
        name, stem, submission_id = item
        with clidb.cli_session() as session:
            try:
                run = score_generic_submission(
                    session,
                    submission_id,
                    rescore_generation=0,
                    scorer=scorer,
                )
                exported = build_run_export_v2(session, run.id)
                if report_dir:
                    Path(report_dir).mkdir(parents=True, exist_ok=True)
                    html = generate_report_v2(session, run.id)
                    shutil.copyfile(html, Path(report_dir) / (stem + ".html"))
                return {
                    "file": name,
                    "status": "ok",
                    "total": exported["run"]["final_total_score"],
                    "grade": exported["run"]["grade"],
                    "need_review": exported["run"]["need_manual_review"],
                    "run_id": run.id,
                    "submission_id": submission_id,
                    "document_snapshot_id": exported["submission"][
                        "document_snapshot_id"
                    ],
                    "identity": exported["identity"],
                }
            except Exception as exc:
                session.rollback()
                return {"file": name, "status": "失败", "error": str(exc)}

    results = _run_scoring(
        submissions,
        _score_one,
        workers,
        show_progress=not as_json,
    )
    results.sort(key=lambda item: item["file"])
    any_failed = any(item["status"] != "ok" for item in results)
    payload = {
        "contract": "grading-core/cli-profile-score@1",
        "rubric": rubric_label,
        "rubric_version_id": rubric_version_id,
        "batch_id": batch_id,
        "profile": {"key": profile_key, "version": exact_version},
        "results": results,
    }
    if as_json:
        render.dump_json(payload)
    else:
        render.info(
            "Profile：%s / %s    RubricVersion：%s"
            % (profile_key, exact_version, rubric_version_id)
        )
        render.render_table(
            "v2 Profile/Core 评分结果",
            ["文件", "总分", "等级", "需复核", "状态"],
            [
                (
                    item["file"],
                    item.get("total", "-"),
                    item.get("grade", "-"),
                    "是" if item.get("need_review") else "否",
                    item["status"],
                )
                for item in results
            ],
        )
    return 1 if any_failed else 0


# ---- 命令 ----
@app.command()
def init(
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
    seed: bool = typer.Option(False, "--seed", help="顺带造默认评分标准 + 演示批次"),
):
    """初始化本地数据库（建表），可选填充演示数据，并给出 provider/离线上手引导。"""
    _bootstrap(db, storage)
    if seed:
        from backend.app.scripts.seed_dev import seed as seed_dev_seed

        with clidb.cli_session() as session:
            seed_dev_seed(session)
        render.info("✓ 已填充默认评分标准与演示批次")
    render.info("✓ 本地库就绪：%s" % settings.DATABASE_URL)
    render.hint("storage: %s" % settings.STORAGE_ROOT)

    from backend.app.services.llm.factory import provider_network_scope

    render.info("下一步 · 选 LLM（当前 LLM_PROVIDER=%s，network=%s）：" % (settings.LLM_PROVIDER, provider_network_scope()))
    render.hint("  • 演示/离线：保持 mock（零配置不触网）")
    render.hint("  • 本地私有模型（推荐离线）：起 llama-server 后 `pgs score ... --provider local --base-url http://localhost:8080/v1`")
    render.hint("  • 云厂商：`--provider openai_compatible`（需 API Key）。详见 docs/本地模型与离线部署.md")
    render.hint("自检：`pgs check`（连通）/ `pgs doctor`（是否纯本地零外呼）")


@app.command()
def check(
    mock: bool = typer.Option(False, "--mock", help="强制按 Mock 自检（不读真实 provider）"),
    provider: Optional[str] = _PROVIDER_OPT,
    model: Optional[str] = _MODEL_OPT,
    base_url: Optional[str] = _BASE_URL_OPT,
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
):
    """LLM 连通自检（mock 直接 ok；真实 provider 发极小请求测连通）。"""
    if mock:
        settings.LLM_PROVIDER = "mock"
    _apply_llm_overrides(provider, model, base_url)
    from backend.app.services.llm.diagnostics import check_connectivity

    result = check_connectivity()
    if as_json:
        render.dump_json(result)
    else:
        render.render_table("LLM 连通自检", ["字段", "值"], [(k, v) for k, v in result.items()])
        (render.info if result.get("ok") else render.error)(
            "ok=%s  stage=%s  network=%s" % (result.get("ok"), result.get("stage"), result.get("network"))
        )
    raise typer.Exit(0 if result.get("ok") else 1)


def _scope_zh(scope):
    return {"offline": "离线·不触网", "local": "本地·连本地端口", "external": "外呼·云/网络"}.get(scope, scope)


@app.command()
def doctor(
    offline: bool = typer.Option(False, "--offline", help="按离线模式预览（硬禁网络型导出）"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
):
    """离线就绪自检：盘点网络触点（LLM / 表格导出），判定当前配置是否纯本地、零外呼。"""
    if offline:
        settings.OFFLINE_MODE = True
    from backend.app.services.offline import network_touchpoints, offline_ready

    points = network_touchpoints()
    ready = offline_ready()
    if as_json:
        render.dump_json({"offline_ready": ready, "offline_mode": settings.OFFLINE_MODE, "touchpoints": points})
    else:
        render.render_table(
            "网络触点审计",
            ["环节", "网络", "说明"],
            [(p["component"], _scope_zh(p["scope"]), p["detail"]) for p in points],
        )
        (render.info if ready else render.error)(
            "离线就绪：%s（OFFLINE_MODE=%s）" % ("是 ✓ 纯本地零外呼" if ready else "否 ✗ 存在外呼环节", settings.OFFLINE_MODE)
        )
    raise typer.Exit(0)


@app.command("core-cutover-audit")
def core_cutover_audit(
    as_json: bool = typer.Option(False, "--json", help="输出稳定 JSON 报告"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """只读盘点活跃 Rubric 的 Core 可执行性；有 blocker 时退出 1。"""

    _bootstrap(db, storage)
    from backend.app.services.deployment.cutover_inventory import (
        build_core_cutover_inventory,
    )

    with clidb.cli_session() as session:
        report = build_core_cutover_inventory(session)
    if as_json:
        render.dump_json(report)
    else:
        rows = [
            (
                item["rubric_id"],
                item["rubric_source_kind"],
                item["rubric_version_id"] or "-",
                item["status"],
                ",".join(item["blocker_codes"]) or "-",
                len(item["affected_batches"]),
            )
            for item in report["targets"]
        ]
        render.render_table(
            "Core 默认切换 Rubric 盘点",
            ["Rubric", "来源", "Version", "状态", "Blocker", "批次数"],
            rows,
        )
        message = (
            "Rubric 盘点通过（仅代表 inventory scope，不代表生产发布批准）"
            if report["inventory_clear"]
            else "Rubric 盘点存在 %s 个 blocker"
            % report["summary"]["blockers"]
        )
        (render.info if report["inventory_clear"] else render.error)(message)
    raise typer.Exit(0 if report["inventory_clear"] else 1)


@app.command()
def rubrics(
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """列出本地库中的评分标准。"""
    _bootstrap(db, storage)
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from backend.app.db.models import Rubric

    with clidb.cli_session() as session:
        items = session.scalars(
            select(Rubric).options(selectinload(Rubric.criteria)).order_by(Rubric.created_at.desc())
        ).all()
        data = [
            {"id": r.id, "name": r.name, "version": r.version, "status": r.status, "codes": [c.code for c in r.criteria]}
            for r in items
        ]
    if as_json:
        render.dump_json(data)
        return
    if not data:
        render.warn("（本地库暂无评分标准；用 `pgs import` 或 `pgs init --seed`）")
        return
    render.render_table(
        "评分标准",
        ["id", "名称", "版本", "状态", "评分项 code"],
        [(d["id"], d["name"], d["version"], d["status"], ",".join(d["codes"])) for d in data],
    )


@app.command("import")
def import_rubric(
    rules: Path = typer.Argument(..., help="Excel 评分规则 .xlsx/.xlsm"),
    name: str = typer.Option(..., "--name", help="评分标准名称"),
    version: str = typer.Option("v1.0", "--version"),
    template: Optional[Path] = typer.Option(None, "--template", help="Word 论文模板 .docx（可选）"),
    description: Optional[str] = typer.Option(None, "--description"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """从 Excel 规则（+可选 Word 模板）导入评分标准到本地库。"""
    _bootstrap(db, storage)
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import selectinload

    from backend.app.db.models import Rubric
    from backend.app.services.dev_user import ensure_dev_user
    from backend.app.services.rubric_import import pipeline as rubric_pipeline

    scorer = _safe_scorer()
    try:
        # Parsing and optional LLM compilation deliberately happen before a
        # database session is opened.  The returned DTO is immutable.
        prepared = _prepare_cli_file_import(
            rules=rules,
            template=template,
            name=name,
            version=version,
            description=description,
            scorer=scorer,
        )
    except (OSError, ValueError) as exc:
        render.error("解析失败：%s" % exc)
        raise typer.Exit(2)

    prepared_graph = prepared.to_mapping()
    with clidb.cli_session() as session:
        ensure_dev_user(session)
        session.commit()
        try:
            identity = rubric_pipeline.persist_prepared_import(
                session=session,
                prepared=prepared,
                actor_id=settings.DEFAULT_DEV_USER_ID,
            )
        except (IntegrityError, ValueError) as exc:
            session.rollback()
            message = (
                "rubric name and version already exist"
                if isinstance(exc, IntegrityError)
                else str(exc)
            )
            render.error(message)
            raise typer.Exit(2)
        rubric = session.scalar(
            select(Rubric)
            .where(Rubric.id == identity.rubric_id)
            .options(selectinload(Rubric.criteria))
        )
        if rubric is None:  # defensive: persistence returned an invalid identity
            session.rollback()
            exc = ValueError("导入完成后找不到评分标准")
            render.error(str(exc))
            raise typer.Exit(2)
        rows = [(c.code, c.name, c.max_score) for c in rubric.criteria]
        rid, rname, rver = rubric.id, rubric.name, rubric.version
        warnings = list(prepared_graph["compilation"].get("warnings") or [])
    render.info("✓ 已导入：%s（%s）  id=%s" % (rname, rver, rid))
    render.render_table("评分项", ["code", "名称", "满分"], rows)
    for warning in warnings:
        render.warn("⚠ %s" % warning)


@app.command()
def score(
    paths: List[Path] = typer.Argument(..., help="docx/pdf 文件或目录（目录递归 .docx/.pdf）"),
    rubric: Optional[str] = typer.Option(None, "--rubric", help="评分标准 id 或名称"),
    rubric_file: Optional[Path] = typer.Option(
        None,
        "--rubric-file",
        help="--no-db 无状态评分使用的 Excel 规则",
    ),
    template: Optional[Path] = typer.Option(None, "--template", help="--rubric-file 配套 Word 模板"),
    mock: bool = typer.Option(False, "--mock", help="强制用 Mock 评分器（不调真实 LLM）"),
    provider: Optional[str] = _PROVIDER_OPT,
    model: Optional[str] = _MODEL_OPT,
    base_url: Optional[str] = _BASE_URL_OPT,
    report_dir: Optional[Path] = typer.Option(None, "--report-dir", help="为每篇生成 HTML 报告到该目录"),
    workers: int = typer.Option(1, "--workers", min=1, help="并发评分线程数（>1 适合真实 LLM 批量；6.1 解耦后可真正并行）"),
    no_db: bool = typer.Option(False, "--no-db", help="无状态：从文件直接评分，不建 sqlite/不落库（需 --rubric-file）"),
    profile_key: Optional[str] = typer.Option(
        None,
        "--profile",
        help="显式业务 Profile；不传保持默认 thesis 兼容路径",
    ),
    profile_version: Optional[str] = typer.Option(
        None,
        "--profile-version",
        help="显式 Profile 版本；省略时解析该 key 唯一注册版本",
    ),
    metadata_json: str = typer.Option(
        "{}",
        "--metadata-json",
        help="显式 Profile 的业务元数据 JSON 对象",
    ),
    as_json: bool = typer.Option(False, "--json"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """对一个或多个论文文件评分（复用与 Web 同款内核）。任一篇失败则非零退出。"""
    _apply_llm_overrides(provider, model, base_url)
    files = _collect_files(paths)
    if not files:
        render.error("没有可评分的文件")
        raise typer.Exit(2)
    try:
        metadata = json.loads(metadata_json)
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter("--metadata-json 必须是 JSON 对象") from exc
    if not isinstance(metadata, dict):
        raise typer.BadParameter("--metadata-json 必须是 JSON 对象")
    if profile_key:
        if no_db:
            from backend.app.services.scoring.profiles.registry import (
                get_profile,
            )
            from backend.app.services.scoring.profiles.registry import (
                get_profile_by_key,
            )

            selected = (
                get_profile(
                    profile_key=profile_key,
                    profile_version=profile_version,
                )
                if profile_version
                else get_profile_by_key(profile_key)
            )
            raise typer.Exit(
                _score_stateless_profiled(
                    files,
                    rubric_file,
                    template,
                    mock,
                    workers,
                    report_dir,
                    as_json,
                    profile_key=profile_key,
                    profile_version=selected.profile_version,
                )
            )
        raise typer.Exit(
            _score_profiled(
                files=files,
                rubric=rubric,
                rubric_file=rubric_file,
                template=template,
                mock=mock,
                workers=workers,
                report_dir=report_dir,
                as_json=as_json,
                db=db,
                storage=storage,
                profile_key=profile_key,
                profile_version=profile_version,
                metadata=metadata,
            )
        )
    if profile_version:
        raise typer.BadParameter("--profile-version 需要同时指定 --profile")
    if metadata:
        raise typer.BadParameter("--metadata-json 仅用于显式 --profile 路径")
    if no_db:
        raise typer.Exit(_score_stateless(files, rubric_file, template, mock, workers, report_dir, as_json))
    db_url = _bootstrap(db, storage)

    from sqlalchemy import select

    from backend.app.db.models import GradingBatch
    from backend.app.services.dev_user import ensure_dev_user
    from backend.app.services.papers.ingestion import ingest_file
    from backend.app.services.report.generator import generate_report
    from backend.app.services.scoring.engine import score_paper

    scorer = None
    if mock:
        from backend.app.services.llm.mock import MockLLMScorer

        scorer = MockLLMScorer()

    results = []
    any_failed = False
    to_score = []  # (file_name, paper_id, stem)

    # 阶段 1：解析+落库（主会话，顺序）
    with clidb.cli_session() as session:
        ensure_dev_user(session)
        rub = _resolve_rubric(session, rubric, rubric_file, template)
        rubric_label = "%s / %s" % (rub.name, rub.version)
        formal_version = _resolve_cli_frozen_version(session, rub)
        rubric_version_id = formal_version.id if formal_version else None
        batch = GradingBatch(
            name="CLI-%s" % datetime.now().strftime("%Y%m%d-%H%M%S"),
            rubric_id=rub.id,
            rubric_version_id=rubric_version_id,
            status="draft",
            created_by=settings.DEFAULT_DEV_USER_ID,
        )
        session.add(batch)
        session.commit()
        batch_id = batch.id
        for path in files:
            try:
                paper = ingest_file(session, batch_id, str(path), path.name)
                session.commit()
                if paper.status != "parsed":
                    any_failed = True
                    results.append({"file": path.name, "status": "解析失败", "error": paper.error_message})
                else:
                    to_score.append((path.name, paper.id, path.stem))
            except Exception as exc:  # 解析阶段单篇失败不中断
                session.rollback()
                any_failed = True
                results.append({"file": path.name, "status": "失败", "error": str(exc)})

    if workers > 1 and db_url.startswith("sqlite") and not as_json:
        render.hint("提示：本地 SQLite 已启用 WAL + busy_timeout；worker 可并行计算，持久化采用短事务并由幂等键防止重复权威 run。")

    # 阶段 2：评分（每 worker 独立会话，互不串扰）
    def _score_one(item):
        name, paper_id, stem = item
        with clidb.cli_session() as scoring_session:
            try:
                run = score_paper(scoring_session, paper_id, scorer=scorer)
                result = {
                    "file": name,
                    "status": "ok",
                    "total": float(run.final_total_score or 0),
                    "grade": run.grade,
                    "need_review": bool(run.need_manual_review),
                    "tokens": run.total_tokens or 0,
                    "run_id": run.id,
                    "paper_id": paper_id,
                }
                if report_dir:
                    Path(report_dir).mkdir(parents=True, exist_ok=True)
                    html = generate_report(scoring_session, run.id)
                    shutil.copyfile(html, Path(report_dir) / ("%s.html" % stem))
                return result
            except Exception as exc:  # 单篇失败不中断整批
                scoring_session.rollback()
                return {"file": name, "status": "失败", "error": str(exc)}

    scored = _run_scoring(to_score, _score_one, workers, show_progress=not as_json)
    results.extend(scored)
    results.sort(key=lambda item: item["file"])
    any_failed = any_failed or any(r["status"] != "ok" for r in scored)

    if as_json:
        render.dump_json({"rubric": rubric_label, "batch_id": batch_id, "results": results})
        raise typer.Exit(1 if any_failed else 0)

    render.info("评分标准：%s    批次：%s" % (rubric_label, batch_id))
    rows = [
        (
            r["file"],
            r.get("total", "-"),
            r.get("grade", "-"),
            ("是" if r.get("need_review") else "否") if r["status"] == "ok" else "-",
            r.get("tokens", "-"),
            r["status"] + (("：" + str(r["error"])) if r.get("error") else ""),
        )
        for r in results
    ]
    render.render_table(
        "评分结果",
        ["文件", "总分", "等级", "需复核", "Token", "状态"],
        rows,
        style_fn=lambda row: None if str(row[5]) == "ok" else "red",
    )
    _print_score_summary(results, batch_id)
    if report_dir:
        render.hint("报告已写入：%s" % report_dir)
    raise typer.Exit(1 if any_failed else 0)


@app.command()
def report(
    run_id: str = typer.Argument(..., help="评分任务 id"),
    output: Optional[Path] = typer.Option(None, "-o", "--output", help="输出路径"),
    fmt: str = typer.Option("html", "--format", help="html（默认）或 json（结构化导出，供下游处理）"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """生成某次评分的报告：HTML（人读）或 JSON（结构化导出）。"""
    _bootstrap(db, storage)

    with clidb.cli_session() as session:
        from backend.app.db.models import ScoringRun

        run = session.get(ScoringRun, run_id)
        if run is None:
            render.error("评分任务不存在")
            raise typer.Exit(2)
        is_submission_run = run.submission_id is not None
        if fmt == "json":
            if is_submission_run:
                from backend.app.services.report.generic_export import (
                    build_run_export_v2 as build_run_export,
                )
            else:
                from backend.app.services.report.json_export import build_run_export

            try:
                data = build_run_export(session, run_id)
            except ValueError as exc:
                render.error(str(exc))
                raise typer.Exit(2)
            if output:
                Path(output).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                render.info("✓ 结构化 JSON：%s" % output)
            else:
                render.dump_json(data)
            return
        if is_submission_run:
            from backend.app.services.report.generic_generator import (
                generate_report_v2 as generate_report,
            )
        else:
            from backend.app.services.report.generator import generate_report

        try:
            path = Path(generate_report(session, run_id))
        except ValueError as exc:
            render.error(str(exc))
            raise typer.Exit(2)
        if output:
            shutil.copyfile(path, output)
            path = Path(output)
    render.info("✓ 报告：%s" % path)


@app.command()
def export(
    batch_id: str = typer.Argument(..., help="批次 id"),
    output: Optional[Path] = typer.Option(None, "-o", "--output", help="输出 .xlsx 路径"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """导出某批次评分到 Excel。"""
    _bootstrap(db, storage)
    from backend.app.services.spreadsheet.excel import export_batch_excel

    with clidb.cli_session() as session:
        try:
            path = Path(export_batch_excel(session, batch_id))
        except ValueError as exc:
            render.error(str(exc))
            raise typer.Exit(2)
        if output:
            shutil.copyfile(path, output)
            path = Path(output)
    render.info("✓ Excel：%s" % path)


def guard_review_write(operation, what: str):
    """执行一次复核写操作，把服务层的拒绝翻译成可读退出。

    归档守卫抛的是 `BatchArchived`（非 `ValueError`）。只捕 `ValueError` 会让它
    漏成一串 traceback——运维看到的是崩溃，而不是「这个批次已归档」。崩溃与
    「按规则拒绝」是两件事，输出必须能区分。
    """
    from backend.app.services.batches.state import BatchStateError

    try:
        return operation()
    except (ValueError, BatchStateError) as exc:
        render.error("%s 失败：%s" % (what, exc))
        raise typer.Exit(2)


@app.command()
def review(
    run_id: str = typer.Argument(..., help="评分任务 id"),
    set_scores: List[str] = typer.Option(None, "--set", help="覆盖单项分：CODE=分数（可重复），如 --set C01=18"),
    note: Optional[str] = typer.Option(None, "--note", help="复核意见/理由（写入 ReviewLog）"),
    submit: bool = typer.Option(False, "--submit", help="提交并标记该任务为已复核"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """人工复核：按评分项 code 覆盖单项分 / 提交复核（写 ReviewLog，自动重算总分）。"""
    _bootstrap(db, storage)
    set_scores = set_scores or []
    if not set_scores and not submit:
        raise typer.BadParameter("至少给一个 --set CODE=分数 或 --submit")

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from backend.app.db.models import ScoreItem
    from backend.app.db.models import ScoringRun
    from backend.app.services.dev_user import ensure_dev_user
    from backend.app.services.scoring.engine import submit_review
    from backend.app.services.scoring.engine import update_score_item

    reason = note or "CLI 人工复核"
    with clidb.cli_session() as session:
        ensure_dev_user(session)
        run = session.get(ScoringRun, run_id)
        if run is None:
            render.error("找不到评分任务：%s（用 `pgs runs` 查看）" % run_id)
            raise typer.Exit(2)
        items = session.scalars(
            select(ScoreItem).where(ScoreItem.scoring_run_id == run_id).options(selectinload(ScoreItem.criterion))
        ).all()
        by_code = {item.criterion.code: item for item in items if item.criterion}

        changes = []  # (item_id, code, score) —— 先全部解析校验，再应用
        for spec in set_scores:
            code, sep, raw = spec.partition("=")
            code = code.strip()
            if not sep:
                raise typer.BadParameter("--set 格式应为 CODE=分数，收到：%s" % spec)
            try:
                score = float(raw.strip())
            except ValueError:
                raise typer.BadParameter("分数非法：%s" % spec)
            item = by_code.get(code)
            if item is None:
                raise typer.BadParameter("该任务无评分项 code=%s（可选：%s）" % (code, ",".join(sorted(by_code))))
            changes.append((item.id, code, score))

        for item_id, code, score in changes:
            guard_review_write(
                lambda item_id=item_id, score=score: update_score_item(
                    session, item_id, score, reason, settings.DEFAULT_DEV_USER_ID
                ),
                "覆盖 %s" % code,
            )
        if submit:
            guard_review_write(
                lambda: submit_review(
                    session, run_id, reason, settings.DEFAULT_DEV_USER_ID
                ),
                "提交复核",
            )

        final = session.get(ScoringRun, run_id)
        final_total = float(final.final_total_score or 0)
        grade, status, need_review = final.grade, final.status, bool(final.need_manual_review)

    for _, code, score in changes:
        render.info("✓ %s → %.2f" % (code, score))
    if submit:
        render.info("✓ 已提交复核")
    render.info(
        "总分 %.2f    等级 %s    状态 %s    需复核 %s"
        % (final_total, grade, status, "是" if need_review else "否")
    )


@app.command("scores-template")
def scores_template(
    rubric_id: str = typer.Argument(..., help="评分标准 id 或名称"),
    output: Path = typer.Option(..., "-o", "--output", help="输出 .xlsx 路径"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """生成教师成绩表模板（列随评分项 code 自动展开），供 QWK 填写。"""
    _bootstrap(db, storage)
    from sqlalchemy import select

    from backend.app.db.models import Rubric
    from backend.app.eval.scores_template import build_scores_table_template

    with clidb.cli_session() as session:
        rubric = session.get(Rubric, rubric_id)
        if rubric is None:
            rubric = session.scalar(select(Rubric).where(Rubric.name == rubric_id).order_by(Rubric.created_at.desc()))
        if rubric is None:
            render.error("找不到评分标准：%s（用 `pgs rubrics` 查看）" % rubric_id)
            raise typer.Exit(2)
        data = build_scores_table_template(list(rubric.criteria))
    Path(output).write_bytes(data)
    render.info("✓ 成绩表模板：%s（按论文逐行填 文件名/总分/各 code 分）" % output)


@app.command("eval")
def eval_cmd(
    rubric_id: str = typer.Option(..., "--rubric", help="教师评分所用的 rubric id"),
    papers_dir: Path = typer.Option(..., "--papers-dir", help="真实论文文件夹（仓库外）"),
    scores: Path = typer.Option(..., "--scores", help="教师成绩表 .xlsx/.csv"),
    baseline: Optional[Path] = typer.Option(None, "--baseline", help="基线 JSON（默认 storage/eval/baseline.json）"),
    profile_key: Optional[str] = typer.Option(
        None,
        "--profile",
        help="评估使用的业务 Profile；默认保持 thesis 兼容",
    ),
    profile_version: Optional[str] = typer.Option(
        None,
        "--profile-version",
        help="显式 Profile 版本；省略时锁定该 key 的注册版本",
    ),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """QWK 留出集评估（系统分 vs 教师分）+ 基线/回归门禁。回归则非零退出。"""
    _bootstrap(db, storage)
    from backend.app.eval.labeled_dataset import build_labeled_eval
    from backend.app.eval.run_eval import _write_report
    from backend.app.eval.runner import assert_no_regression
    from backend.app.eval.runner import baseline_from_report
    from backend.app.services.scoring.profiles.registry import get_profile
    from backend.app.services.scoring.profiles.registry import get_profile_by_key

    if profile_version and not profile_key:
        raise typer.BadParameter("--profile-version 需要同时指定 --profile")

    with clidb.cli_session() as session:
        rubric = _resolve_rubric(session, rubric_id, None, None)
        version = _resolve_cli_frozen_version(session, rubric)
        selected_key = profile_key or (
            version.business_profile_key if version is not None else "thesis"
        )
        if version is not None and version.business_profile_key != selected_key:
            raise typer.BadParameter(
                "评分标准 RubricVersion 与 --profile 不匹配"
            )
        try:
            profile = (
                get_profile(
                    profile_key=selected_key,
                    profile_version=profile_version,
                )
                if profile_version
                else get_profile_by_key(selected_key)
            )
        except (LookupError, ValueError) as exc:
            raise typer.BadParameter(str(exc)) from exc
        if profile.profile_key != "thesis":
            raise typer.BadParameter(
                "当前教师留出集导入仅支持 thesis Profile；"
                "其他 Profile 需提供对应的标注数据适配器"
            )
        kwargs = {"rubric_version_id": version.id} if version is not None else {}
        data = build_labeled_eval(
            session,
            rubric.id,
            str(papers_dir),
            str(scores),
            **kwargs,
        )
    report_path = _write_report(data)
    render.render_table(
        "QWK 评估",
        ["指标", "值"],
        [
            ("QWK", data.get("qwk")),
            ("MAE", data.get("mae")),
            ("RMSE", data.get("rmse")),
            ("同档一致率", data.get("exact_grade_agreement")),
            ("相邻档一致率", data.get("adjacent_grade_agreement")),
            ("样本 n", data.get("n")),
            ("数据集", data.get("dataset_size")),
            ("错误数", len(data.get("errors") or [])),
        ],
    )
    per = data.get("per_criterion") or {}
    if per:
        render.render_table(
            "逐维度 bias（>0 偏宽 / <0 偏严）",
            ["code", "bias", "mae", "n"],
            [(code, s["bias"], s["mae"], s["n"]) for code, s in per.items()],
        )
    identity = data.get("evaluation_identity") or {}
    if identity:
        rounding = identity.get("rounding") or {}
        render.render_table(
            "评估身份（可复现）",
            ["字段", "值"],
            [
                ("Profile", "%s / %s" % (
                    identity.get("business_profile_key") or profile.profile_key,
                    identity.get("business_profile_version") or profile.profile_version,
                )),
                ("RubricVersion", identity.get("rubric_version_id") or (version.id if version else "legacy")),
                ("Policy hash", identity.get("policy_hash") or "-"),
                ("GradeScale hash", identity.get("grade_scale_sha256") or "-"),
                ("Rounding", "%s / %s" % (
                    rounding.get("mode", "-"),
                    rounding.get("digits", "-"),
                )),
            ],
        )
    render.hint("报告：%s" % report_path)

    baseline_path = Path(baseline) if baseline else (settings.STORAGE_ROOT / "eval" / "baseline.json")
    issues = []
    if baseline_path.exists():
        base = json.loads(baseline_path.read_text(encoding="utf-8"))
        issues = assert_no_regression(data, base)
        if issues:
            render.error("✗ 回归门禁未通过：")
            for issue in issues:
                render.error("  - %s" % issue)
        else:
            render.info("✓ 回归门禁通过（未低于基线）")
    else:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(baseline_from_report(data), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        render.info("✓ 首次运行：已固化基线 → %s" % baseline_path)
    raise typer.Exit(1 if issues else 0)


@app.command()
def publish(
    rubric_id: str = typer.Argument(..., help="评分标准 id 或名称"),
    compilation_id: Optional[str] = typer.Option(
        None,
        "--compilation-id",
        help="显式选择待发布 compilation；存在多个活动候选时必填",
    ),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """发布已经完成规则审核与可执行性校验的评分标准。"""
    _bootstrap(db, storage)
    from sqlalchemy import select

    from backend.app.db.models import Rubric, RubricCompilation
    from backend.app.services.dev_user import ensure_dev_user
    from backend.app.services.rubrics import lifecycle as rubric_lifecycle

    with clidb.cli_session() as session:
        ensure_dev_user(session)
        rubric = session.get(Rubric, rubric_id)
        if rubric is None:
            rubric = session.scalar(
                select(Rubric).where(Rubric.name == rubric_id).order_by(Rubric.created_at.desc())
            )
        if rubric is None:
            render.error("找不到评分标准：%s（用 `pgs rubrics` 查看）" % rubric_id)
            raise typer.Exit(2)
        compilations = session.scalars(
            select(RubricCompilation)
            .where(RubricCompilation.rubric_id == rubric.id)
            .order_by(RubricCompilation.created_at.desc())
        ).all()
        if not compilations:
            render.error("旧草稿缺少来源图；请先执行显式升级")
            raise typer.Exit(2)
        if compilation_id is not None:
            compilation = session.get(RubricCompilation, compilation_id)
            if compilation is None or compilation.rubric_id != rubric.id:
                render.error("指定 compilation 不存在或不属于该评分标准")
                raise typer.Exit(2)
        else:
            active = [
                item
                for item in compilations
                if item.status != "superseded" and item.published_at is None
            ]
            validated = [item for item in active if item.status == "validated"]
            if len(active) != 1 or len(validated) != 1:
                render.error(
                    "无法唯一选择活动 validated compilation；"
                    "请使用 --compilation-id 显式指定"
                )
                raise typer.Exit(2)
            compilation = validated[0]
        try:
            rubric_lifecycle.publish_rubric(
                session,
                rubric.id,
                compilation.id,
                settings.DEFAULT_DEV_USER_ID,
            )
            session.commit()
        except rubric_lifecycle.RubricLifecycleError as exc:
            session.rollback()
            render.error(str(exc))
            raise typer.Exit(2)
        name, version = rubric.name, rubric.version
    render.info("✓ 已发布：%s（%s）" % (name, version))


def _cli_resolve_rubric(session, rubric_id: str):
    from sqlalchemy import select

    from backend.app.db.models import Rubric

    rubric = session.get(Rubric, rubric_id)
    if rubric is None:
        rubric = session.scalar(
            select(Rubric)
            .where(Rubric.name == rubric_id)
            .order_by(Rubric.created_at.desc())
        )
    if rubric is None:
        raise typer.BadParameter("找不到评分标准：%s" % rubric_id)
    return rubric


@app.command("rubric-graph")
def rubric_graph(
    rubric_id: str = typer.Argument(..., help="评分标准 id 或名称"),
    as_json: bool = typer.Option(False, "--json"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """显示可供审核/恢复调用的安全执行图标识与状态。"""

    _bootstrap(db, storage)
    from backend.app.services.rubrics.draft_graph import read_execution_draft

    with clidb.cli_session() as session:
        rubric = _cli_resolve_rubric(session, rubric_id)
        data = read_execution_draft(session=session, rubric_id=rubric.id)
    if as_json:
        render.dump_json(data)
        return
    active = data.get("active_compilation")
    if active is None:
        render.warn(data.get("ambiguity") or "没有活动 compilation")
    else:
        version = active.get("version") or {}
        render.info(
            "active compilation=%s  version=%s  status=%s"
            % (active["id"], version.get("id", "-"), active["status"])
        )
        render.render_table(
            "原子规则",
            ["rule_code", "方向", "effect", "状态", "reviewer"],
            [
                (
                    item["rule_code"],
                    item["direction"],
                    item["effect_type"],
                    item["status"],
                    item.get("reviewed_by"),
                )
                for item in active.get("rules", [])
            ],
        )
        if active.get("template_links"):
            render.render_table(
                "模板映射",
                ["link_id", "rule_code", "状态"],
                [
                    (item["id"], item["rule_code"], item["review_status"])
                    for item in active["template_links"]
                ],
            )


@app.command("rubric-submit-review")
def rubric_submit_review(
    rubric_id: str = typer.Argument(...),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """显式提交评分标准进入发布审核。"""

    _bootstrap(db, storage)
    from backend.app.services.rubrics import lifecycle

    with clidb.cli_session() as session:
        rubric = _cli_resolve_rubric(session, rubric_id)
        try:
            lifecycle.submit_for_review(session, rubric.id)
            session.commit()
        except lifecycle.RubricLifecycleError as exc:
            session.rollback()
            render.error(str(exc))
            raise typer.Exit(2)
    render.info("✓ 评分标准已提交审核")


@app.command("rubric-return-draft")
def rubric_return_draft(
    rubric_id: str = typer.Argument(...),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """显式把审核中的评分标准退回草稿。"""

    _bootstrap(db, storage)
    from backend.app.services.rubrics import lifecycle

    with clidb.cli_session() as session:
        rubric = _cli_resolve_rubric(session, rubric_id)
        try:
            lifecycle.return_to_draft(session, rubric.id)
            session.commit()
        except lifecycle.RubricLifecycleError as exc:
            session.rollback()
            render.error(str(exc))
            raise typer.Exit(2)
    render.info("✓ 评分标准已退回 draft")


@app.command("rubric-upgrade")
def rubric_upgrade(
    rubric_id: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """显式把无 provenance 的旧草稿升级为可审核 M4 图。"""

    _bootstrap(db, storage)
    from backend.app.services.dev_user import ensure_dev_user
    from backend.app.services.rubrics import lifecycle

    with clidb.cli_session() as session:
        ensure_dev_user(session)
        rubric = _cli_resolve_rubric(session, rubric_id)
        try:
            lifecycle.upgrade_legacy_draft(
                session,
                rubric.id,
                settings.DEFAULT_DEV_USER_ID,
                reason=reason,
            )
            session.commit()
        except lifecycle.RubricLifecycleError as exc:
            session.rollback()
            render.error(str(exc))
            raise typer.Exit(2)
    render.info("✓ 旧草稿已升级为 M4 provenance 图")


def _run_rule_lifecycle_cli(
    *,
    rubric_id: str,
    target_id: str,
    service_name: str,
    reason: str,
    db: Optional[Path],
    storage: Optional[Path],
    changes: dict | None = None,
    decision: str | None = None,
):
    _bootstrap(db, storage)
    from backend.app.services.dev_user import ensure_dev_user
    from backend.app.services.rubrics import lifecycle as rubric_lifecycle

    with clidb.cli_session() as session:
        ensure_dev_user(session)
        service = getattr(rubric_lifecycle, service_name)
        try:
            if service_name == "edit_atomic_rule":
                service(
                    session,
                    rubric_id,
                    target_id,
                    changes,
                    settings.DEFAULT_DEV_USER_ID,
                    reason,
                )
            elif service_name == "review_template_link":
                service(
                    session,
                    rubric_id,
                    target_id,
                    settings.DEFAULT_DEV_USER_ID,
                    decision,
                    reason,
                )
            else:
                service(
                    session,
                    rubric_id,
                    target_id,
                    settings.DEFAULT_DEV_USER_ID,
                    reason,
                )
            session.commit()
        except rubric_lifecycle.RubricLifecycleError as exc:
            session.rollback()
            render.error(str(exc))
            raise typer.Exit(2)


@app.command("rule-submit")
def rule_submit(
    rubric_id: str = typer.Argument(...),
    rule_code: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """提交一个 draft 原子规则进入人工审核。"""
    _run_rule_lifecycle_cli(
        rubric_id=rubric_id,
        target_id=rule_code,
        service_name="submit_atomic_rule_for_review",
        reason=reason,
        db=db,
        storage=storage,
    )


@app.command("rule-edit")
def rule_edit(
    rubric_id: str = typer.Argument(...),
    rule_code: str = typer.Argument(...),
    changes_json: str = typer.Option(..., "--changes-json"),
    reason: str = typer.Option(..., "--reason"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """按 allowlist 编辑一个 draft 原子规则。"""
    try:
        changes = json.loads(changes_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("--changes-json 必须是 JSON 对象") from exc
    if not isinstance(changes, dict):
        raise typer.BadParameter("--changes-json 必须是 JSON 对象")
    _run_rule_lifecycle_cli(
        rubric_id=rubric_id,
        target_id=rule_code,
        service_name="edit_atomic_rule",
        reason=reason,
        changes=changes,
        db=db,
        storage=storage,
    )


@app.command("rule-approve")
def rule_approve(
    rubric_id: str = typer.Argument(...),
    rule_code: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """批准一个 review 原子规则。"""
    _run_rule_lifecycle_cli(
        rubric_id=rubric_id,
        target_id=rule_code,
        service_name="approve_atomic_rule",
        reason=reason,
        db=db,
        storage=storage,
    )


@app.command("rule-reject")
def rule_reject(
    rubric_id: str = typer.Argument(...),
    rule_code: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """驳回一个 review 原子规则。"""
    _run_rule_lifecycle_cli(
        rubric_id=rubric_id,
        target_id=rule_code,
        service_name="reject_atomic_rule",
        reason=reason,
        db=db,
        storage=storage,
    )


@app.command("rule-reopen")
def rule_reopen(
    rubric_id: str = typer.Argument(...),
    rule_code: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """保留驳回审计并把 rejected 原子规则重新打开为 draft。"""
    _run_rule_lifecycle_cli(
        rubric_id=rubric_id,
        target_id=rule_code,
        service_name="reopen_atomic_rule",
        reason=reason,
        db=db,
        storage=storage,
    )


@app.command("template-link-review")
def template_link_review(
    rubric_id: str = typer.Argument(...),
    link_id: str = typer.Argument(...),
    decision: str = typer.Option(..., "--decision"),
    reason: str = typer.Option(..., "--reason"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """确认或驳回一个规则到模板条目的映射。"""
    _run_rule_lifecycle_cli(
        rubric_id=rubric_id,
        target_id=link_id,
        service_name="review_template_link",
        reason=reason,
        decision=decision,
        db=db,
        storage=storage,
    )


@app.command()
def batches(
    as_json: bool = typer.Option(False, "--json"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """列出本地批次（含论文数）。"""
    _bootstrap(db, storage)
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from backend.app.db.models import GradingBatch

    with clidb.cli_session() as session:
        items = session.scalars(
            select(GradingBatch)
            .options(selectinload(GradingBatch.papers), selectinload(GradingBatch.rubric))
            .order_by(GradingBatch.created_at.desc())
        ).all()
        data = [
            {
                "id": b.id,
                "name": b.name,
                "rubric": b.rubric.name if b.rubric else "",
                "papers": len(b.papers),
                "status": b.status,
                "created_at": str(b.created_at)[:19] if b.created_at else "",
            }
            for b in items
        ]
    if as_json:
        render.dump_json(data)
        return
    if not data:
        render.warn("（暂无批次；先 `pgs score`）")
        return
    render.render_table(
        "批次",
        ["id", "名称", "评分标准", "论文数", "状态", "创建"],
        [(d["id"], d["name"], d["rubric"], d["papers"], d["status"], d["created_at"]) for d in data],
    )


@app.command()
def runs(
    batch: Optional[str] = typer.Option(None, "--batch", help="只看某批次 id"),
    as_json: bool = typer.Option(False, "--json"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """列出评分任务（按时间倒序）。"""
    _bootstrap(db, storage)
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from backend.app.db.models import Paper
    from backend.app.db.models import ScoringRun

    with clidb.cli_session() as session:
        query = select(ScoringRun).options(selectinload(ScoringRun.paper)).order_by(ScoringRun.started_at.desc())
        if batch:
            query = query.join(Paper, ScoringRun.paper_id == Paper.id).where(Paper.batch_id == batch)
        items = session.scalars(query).all()
        data = [
            {
                "run_id": r.id,
                "paper": (r.paper.title or r.paper.file_name) if r.paper else "",
                "total": float(r.final_total_score or 0),
                "grade": r.grade,
                "need_review": bool(r.need_manual_review),
                "tokens": r.total_tokens or 0,
                "status": r.status,
                "started_at": str(r.started_at)[:19] if r.started_at else "",
            }
            for r in items
        ]
    if as_json:
        render.dump_json(data)
        return
    if not data:
        render.warn("（暂无评分任务；先 `pgs score`）")
        return
    render.render_table(
        "评分任务",
        ["run_id", "论文", "总分", "等级", "复核", "Token", "状态", "时间"],
        [
            (d["run_id"], d["paper"], d["total"], d["grade"], "是" if d["need_review"] else "否", d["tokens"], d["status"], d["started_at"])
            for d in data
        ],
    )


@app.command()
def show(
    run_id: str = typer.Argument(..., help="评分任务 id"),
    as_json: bool = typer.Option(False, "--json"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """查看某次评分的逐项明细 + 篇章一致性/格式问题。"""
    _bootstrap(db, storage)
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from backend.app.db.models import ScoreItem
    from backend.app.db.models import ScoringRun

    with clidb.cli_session() as session:
        run = session.scalar(
            select(ScoringRun)
            .where(ScoringRun.id == run_id)
            .options(
                selectinload(ScoringRun.items).selectinload(ScoreItem.criterion),
                selectinload(ScoringRun.paper),
            )
        )
        if run is None:
            render.error("找不到评分任务：%s（用 `pgs runs` 查看）" % run_id)
            raise typer.Exit(2)
        header = {
            "run_id": run.id,
            "paper": (run.paper.title or run.paper.file_name) if run.paper else run_id,
            "ai_total": float(run.ai_total_score or 0),
            "final_total": float(run.final_total_score or 0),
            "grade": run.grade,
            "need_review": bool(run.need_manual_review),
            "tokens": run.total_tokens or 0,
            "status": run.status,
        }
        items = [
            {
                "code": it.criterion.code if it.criterion else "",
                "name": it.criterion.name if it.criterion else "",
                "score": float(it.final_score if it.final_score is not None else (it.ai_score or 0)),
                "max": float(it.max_score or 0),
                "evidence_sufficient": bool(it.evidence_sufficient),
                "confidence": it.confidence,
                "need_review": bool(it.need_manual_review),
                "reason": it.reason or "",
                "deductions": it.deduction_items or [],
            }
            for it in run.items
        ]
        coherence = list(run.coherence_findings or [])
        fmt = list(run.format_findings or [])

    if as_json:
        render.dump_json({"run": header, "items": items, "coherence_findings": coherence, "format_findings": fmt})
        return
    render.info(
        "论文：%s    总分(AI/终)：%.2f/%.2f    等级：%s    需复核：%s    Token：%s"
        % (
            header["paper"],
            header["ai_total"],
            header["final_total"],
            header["grade"],
            "是" if header["need_review"] else "否",
            header["tokens"],
        )
    )
    render.render_table(
        "逐项评分",
        ["code", "评分项", "得分/满分", "证据足", "置信", "复核"],
        [
            (i["code"], i["name"], "%.1f/%.1f" % (i["score"], i["max"]), "是" if i["evidence_sufficient"] else "否", i["confidence"], "是" if i["need_review"] else "否")
            for i in items
        ],
        style_fn=lambda row: "yellow" if row[5] == "是" else None,
    )
    findings = [("篇章", f.get("severity", ""), f.get("kind") or f.get("field", ""), f.get("message", "")) for f in coherence]
    findings += [("格式", f.get("severity", ""), f.get("kind") or f.get("field", ""), f.get("message", "")) for f in fmt]
    if findings:
        render.render_table("篇章一致性 / 格式问题", ["类别", "级别", "类型", "说明"], findings)


def run():
    """console_script 入口（pyproject [project.scripts] pgs）。"""
    app()


if __name__ == "__main__":
    run()
