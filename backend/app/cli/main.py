"""`pgs` 命令行端：本地零服务，复用与 Web 同一套评分内核（services/*）。

子命令：init / check / import / rubrics / score / report / export / eval。
所有需库的命令先 `_bootstrap`（注入本地 sqlite+storage 到 settings 并建表），再用自建会话调内核。
"""

import json
import shutil
from datetime import datetime
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
def check(as_json: bool = typer.Option(False, "--json", help="输出 JSON")):
    """LLM 连通自检（mock 直接 ok；真实 provider 发极小请求测连通）。"""
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
    as_json: bool = typer.Option(False, "--json"),
    db: Optional[Path] = _DB_OPT,
    storage: Optional[Path] = _STORAGE_OPT,
):
    """对一个或多个论文文件评分（复用与 Web 同款内核）。任一篇失败则非零退出。"""
    _bootstrap(db, storage)
    files = _collect_files(paths)
    if not files:
        render.error("没有可评分的文件")
        raise typer.Exit(2)

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
                    continue
                run = score_paper(session, paper.id, scorer=scorer)
                results.append(
                    {
                        "file": path.name,
                        "status": "ok",
                        "total": float(run.final_total_score or 0),
                        "grade": run.grade,
                        "need_review": bool(run.need_manual_review),
                        "tokens": run.total_tokens or 0,
                        "run_id": run.id,
                        "paper_id": paper.id,
                    }
                )
                if report_dir:
                    Path(report_dir).mkdir(parents=True, exist_ok=True)
                    html = generate_report(session, run.id)
                    shutil.copyfile(html, Path(report_dir) / ("%s.html" % path.stem))
            except Exception as exc:  # 单篇失败不中断整批
                session.rollback()
                any_failed = True
                results.append({"file": path.name, "status": "失败", "error": str(exc)})

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


def run():
    """console_script 入口（pyproject [project.scripts] pgs）。"""
    app()


if __name__ == "__main__":
    run()
