"""`pgs` 命令行端：本地零服务，复用与 Web 同一套评分内核（services/*）。

子命令：init / check / import / rubrics / score / report / export / eval。
所有需库的命令先 `_bootstrap`（注入本地 sqlite+storage 到 settings 并建表），再用自建会话调内核。
"""

import json
import shutil
from datetime import datetime
from datetime import timezone
from pathlib import Path
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
        from backend.app.services.rubric_import.parser import parse_rubric_files
        from backend.app.services.rubric_import.persist import persist_imported_rubric

        imported = parse_rubric_files(
            rules_bytes=Path(rubric_file).read_bytes(),
            template_bytes=Path(template).read_bytes() if template else None,
            scorer=_safe_scorer(),
        )
        version = "cli-%s" % datetime.now().strftime("%Y%m%d-%H%M%S")
        return persist_imported_rubric(session, Path(rubric_file).stem, version, None, imported)

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
        return loaded

    raise typer.BadParameter("请用 --rubric <id|名称> 或 --rubric-file 指定评分标准（或先 `pgs init --seed`）")


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


# ---- 命令 ----
@app.command()
def init(
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
    seed: bool = typer.Option(False, "--seed", help="顺带造默认评分标准 + 演示批次"),
):
    """初始化本地数据库（建表），可选填充演示数据。"""
    _bootstrap(db, storage)
    if seed:
        from backend.app.scripts.seed_dev import seed as seed_dev_seed

        with clidb.cli_session() as session:
            seed_dev_seed(session)
        render.info("✓ 已填充默认评分标准与演示批次")
    render.info("✓ 本地库就绪：%s" % settings.DATABASE_URL)
    render.hint("storage: %s" % settings.STORAGE_ROOT)


@app.command()
def check(
    mock: bool = typer.Option(False, "--mock", help="强制按 Mock 自检（不读真实 provider）"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
):
    """LLM 连通自检（mock 直接 ok；真实 provider 发极小请求测连通）。"""
    if mock:
        settings.LLM_PROVIDER = "mock"
    from backend.app.services.llm.diagnostics import check_connectivity

    result = check_connectivity()
    if as_json:
        render.dump_json(result)
    else:
        render.render_table("LLM 连通自检", ["字段", "值"], [(k, v) for k, v in result.items()])
        (render.info if result.get("ok") else render.error)(
            "ok=%s  stage=%s" % (result.get("ok"), result.get("stage"))
        )
    raise typer.Exit(0 if result.get("ok") else 1)


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
    from backend.app.services.dev_user import ensure_dev_user
    from backend.app.services.rubric_import.parser import parse_rubric_files
    from backend.app.services.rubric_import.persist import persist_imported_rubric

    rules_bytes = Path(rules).read_bytes()
    template_bytes = Path(template).read_bytes() if template else None
    with clidb.cli_session() as session:
        ensure_dev_user(session)
        session.commit()
        try:
            imported = parse_rubric_files(rules_bytes=rules_bytes, template_bytes=template_bytes, scorer=_safe_scorer())
        except ValueError as exc:
            render.error("解析失败：%s" % exc)
            raise typer.Exit(2)
        try:
            rubric = persist_imported_rubric(session, name, version, description, imported)
        except ValueError as exc:
            render.error(str(exc))
            raise typer.Exit(2)
        rows = [(c.code, c.name, c.max_score) for c in rubric.criteria]
        rid, rname, rver = rubric.id, rubric.name, rubric.version
        warnings = list(imported.warnings or [])
    render.info("✓ 已导入：%s（%s）  id=%s" % (rname, rver, rid))
    render.render_table("评分项", ["code", "名称", "满分"], rows)
    for warning in warnings:
        render.warn("⚠ %s" % warning)


@app.command()
def score(
    paths: List[Path] = typer.Argument(..., help="docx/pdf 文件或目录（目录递归 .docx/.pdf）"),
    rubric: Optional[str] = typer.Option(None, "--rubric", help="评分标准 id 或名称"),
    rubric_file: Optional[Path] = typer.Option(None, "--rubric-file", help="临时导入的 Excel 规则"),
    template: Optional[Path] = typer.Option(None, "--template", help="--rubric-file 配套 Word 模板"),
    mock: bool = typer.Option(False, "--mock", help="强制用 Mock 评分器（不调真实 LLM）"),
    report_dir: Optional[Path] = typer.Option(None, "--report-dir", help="为每篇生成 HTML 报告到该目录"),
    workers: int = typer.Option(1, "--workers", min=1, help="并发评分线程数（>1 适合真实 LLM 批量；6.1 解耦后可真正并行）"),
    no_db: bool = typer.Option(False, "--no-db", help="无状态：从文件直接评分，不建 sqlite/不落库（需 --rubric-file）"),
    as_json: bool = typer.Option(False, "--json"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """对一个或多个论文文件评分（复用与 Web 同款内核）。任一篇失败则非零退出。"""
    files = _collect_files(paths)
    if not files:
        render.error("没有可评分的文件")
        raise typer.Exit(2)
    if no_db:
        raise typer.Exit(_score_stateless(files, rubric_file, template, mock, workers, report_dir, as_json))
    db_url = _bootstrap(db, storage)

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
        batch = GradingBatch(
            name="CLI-%s" % datetime.now().strftime("%Y%m%d-%H%M%S"),
            rubric_id=rub.id,
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
        render.hint("提示：本地 sqlite 下评分事务跨 LLM 调用持写锁，多 worker 实际趋于串行；要真正并行可指向并发数据库。")

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
    output: Optional[Path] = typer.Option(None, "-o", "--output", help="输出 HTML 路径"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """生成某次评分的 HTML 报告。"""
    _bootstrap(db, storage)
    from backend.app.services.report.generator import generate_report

    with clidb.cli_session() as session:
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
            try:
                update_score_item(session, item_id, score, reason, settings.DEFAULT_DEV_USER_ID)
            except ValueError as exc:
                render.error("覆盖 %s 失败：%s" % (code, exc))
                raise typer.Exit(2)
        if submit:
            submit_review(session, run_id, reason, settings.DEFAULT_DEV_USER_ID)

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


@app.command("eval")
def eval_cmd(
    rubric_id: str = typer.Option(..., "--rubric", help="教师评分所用的 rubric id"),
    papers_dir: Path = typer.Option(..., "--papers-dir", help="真实论文文件夹（仓库外）"),
    scores: Path = typer.Option(..., "--scores", help="教师成绩表 .xlsx/.csv"),
    baseline: Optional[Path] = typer.Option(None, "--baseline", help="基线 JSON（默认 storage/eval/baseline.json）"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """QWK 留出集评估（系统分 vs 教师分）+ 基线/回归门禁。回归则非零退出。"""
    _bootstrap(db, storage)
    from backend.app.eval.labeled_dataset import build_labeled_eval
    from backend.app.eval.run_eval import _write_report
    from backend.app.eval.runner import assert_no_regression
    from backend.app.eval.runner import baseline_from_report

    with clidb.cli_session() as session:
        data = build_labeled_eval(session, rubric_id, str(papers_dir), str(scores))
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
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """发布草稿评分标准（draft → published）。"""
    _bootstrap(db, storage)
    from sqlalchemy import select

    from backend.app.db.models import Rubric

    with clidb.cli_session() as session:
        rubric = session.get(Rubric, rubric_id)
        if rubric is None:
            rubric = session.scalar(
                select(Rubric).where(Rubric.name == rubric_id).order_by(Rubric.created_at.desc())
            )
        if rubric is None:
            render.error("找不到评分标准：%s（用 `pgs rubrics` 查看）" % rubric_id)
            raise typer.Exit(2)
        rubric.status = "published"
        rubric.published_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.commit()
        name, version = rubric.name, rubric.version
    render.info("✓ 已发布：%s（%s）" % (name, version))


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
