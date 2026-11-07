"""ASAP 2.0 外部基准评测（docs/ASAP2.0外部基准评测方案.md）。

两步，数据与生成物都在仓库外：

    .venv/bin/python -m backend.app.scripts.run_asap_eval prepare \\
        --csv ASAP2_train_sourcetexts.csv --out ~/pgs-eval/asap2 --size 300

    DATABASE_URL=sqlite+pysqlite:///$HOME/pgs-eval/asap2/eval.db \\
    STORAGE_ROOT=$HOME/pgs-eval/asap2/storage \\
    .venv/bin/python -m backend.app.scripts.run_asap_eval run \\
        --data ~/pgs-eval/asap2 --levels <档位 JSON> --label baseline

``run`` 在进程内走工作台同一组接口建草稿、确认规则、提交审核、发布评分标准，
再用 ``build_labeled_eval`` 导入并评分，最后在 1–6 原始分上算指标。模型由
``LLM_PROVIDER`` 等环境变量或批次连接决定；真实模型会产生费用，需要授权预算。

开始评分前拒绝落在仓库里的输出目录、报告、SQLite 数据库文件与 ``STORAGE_ROOT``
（导入会把作文文档和解析出的正文存进去）。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timezone
import json
from pathlib import Path
import sys

from backend.app.core.config import settings
from backend.app.eval import asap
from backend.app.eval.gating import GateValidationError
from backend.app.eval.gating import ensure_external_artifact_dir


PROJECT_ROOT = Path(__file__).resolve().parents[3]
STAGE2_SETTINGS = (
    "SCORING_TOKEN_BUDGET_TARGET",
    "SCORING_JUDGE_SCOPE_ENABLED",
    "SCORING_INSUFFICIENT_EVIDENCE_MODE",
)


def publish_rubric(client, levels: dict, *, version: str) -> str:
    """建草稿 → 确认全部规则 → 提交审核 → 发布；返回评分标准 ID。"""

    prefix = settings.API_PREFIX.rstrip("/")
    created = client.post(prefix + "/rubrics", json=asap.rubric_payload(levels, version=version))
    if created.status_code != 200:
        raise RuntimeError("创建评分标准失败：%s" % created.text)
    rubric_id = created.json()["id"]
    workspace = client.get("%s/rubrics/%s/review-workspace" % (prefix, rubric_id)).json()
    for rule in workspace["rules"]:
        if rule["status"] in {"draft", "review"}:
            confirmed = client.post(
                "%s/rubrics/%s/rules/%s/confirm" % (prefix, rubric_id, rule["rule_code"]),
                json={
                    "compilation_id": workspace["compilation_id"],
                    "rule_id": rule["id"],
                    "content_token": rule["content_token"],
                    "reason": "ASAP 2.0 外部基准评测",
                },
            )
            if confirmed.status_code != 200:
                raise RuntimeError("确认规则失败：%s" % confirmed.text)
    submitted = client.post("%s/rubrics/%s/submit-review" % (prefix, rubric_id))
    if submitted.status_code != 200:
        raise RuntimeError("提交审核失败：%s" % submitted.text)
    published = client.post(
        "%s/rubrics/%s/publish" % (prefix, rubric_id),
        json={"compilation_id": workspace["compilation_id"], "reason": "ASAP 2.0 外部基准评测"},
    )
    if published.status_code != 200:
        raise RuntimeError("发布评分标准失败：%s" % published.text)
    return rubric_id


def _existing_rubric(db, version: str):
    from sqlalchemy import select

    from backend.app.db.models import Rubric

    return db.scalar(
        select(Rubric).where(Rubric.name == asap.RUBRIC_NAME, Rubric.version == version)
    )


def levels_version(levels: dict) -> str:
    """评分标准版本号由档位文件哈希得出；报告里的档位哈希与“是否官方”只对这个版本成立。"""

    return "levels-%s" % levels["sha256"][:12]


def resolve_rubric(db, client, levels: dict, rubric_id: str | None = None) -> str:
    """返回这份档位文件对应的已发布评分标准；没有就新建并发布。

    显式给 ``rubric_id`` 时同样核对名称、版本与发布状态：别的档位文件建的评分标准
    会让报告记下错误的档位哈希和“是否官方”，直接拒绝。
    """

    from backend.app.db.models import Rubric

    version = levels_version(levels)
    if rubric_id is None:
        existing = _existing_rubric(db, version)
        if existing is None:
            rubric_id = publish_rubric(client, levels, version=version)
            db.expire_all()
            return rubric_id
    else:
        existing = db.get(Rubric, rubric_id)
        if existing is None:
            raise RuntimeError("评分标准 %s 不存在" % rubric_id)
        if existing.name != asap.RUBRIC_NAME or existing.version != version:
            raise RuntimeError(
                "评分标准 %s（%s %s）不是用这份档位文件建的（应为 %s %s），报告的档位哈希会对不上"
                % (rubric_id, existing.name, existing.version, asap.RUBRIC_NAME, version)
            )
    if existing.status != "published":
        raise RuntimeError("评分标准 %s 已存在但未发布，请先在工作台处理或换档位文件" % existing.id)
    return existing.id


def evaluate(db, client, data_dir: Path, levels: dict, *, label: str, rubric_id: str | None = None) -> dict:
    """评分并汇总；同一档位文件只建一次评分标准，之后的运行沿用。"""

    from backend.app.eval.labeled_dataset import build_labeled_eval

    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    rubric_id = resolve_rubric(db, client, levels, rubric_id)
    sample_ids = {asap.filename_for(item["essay_id"]): item["essay_id"] for item in manifest["essays"]}
    base = build_labeled_eval(
        db,
        rubric_id,
        data_dir / "papers",
        data_dir / "scores.csv",
        sample_ids_by_filename=sample_ids,
    )
    return {
        "schema": asap.REPORT_SCHEMA,
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gating_eligible": False,
        "settings": {
            "LLM_PROVIDER": settings.LLM_PROVIDER,
            **{name: getattr(settings, name) for name in STAGE2_SETTINGS},
        },
        "levels": {key: levels[key] for key in ("official", "source", "sha256")},
        "rubric_id": rubric_id,
        "rubric_version_id": base.get("rubric_version_id"),
        "manifest": {key: manifest[key] for key in ("source_sha256", "size", "seed", "with_sources", "score_counts")},
        "summary": asap.summarize(base["per_sample"], manifest),
        "runs": {
            "dataset_size": base.get("dataset_size"),
            "completed_runs": base.get("completed_runs"),
            "review_rate": base.get("review_rate"),
            "blocked_rate": base.get("blocked_rate"),
            "errors": base.get("errors"),
        },
        "per_sample": [
            {key: sample.get(key) for key in ("sample_id", "human_total", "system_total", "need_manual_review", "blocked", "run_identity")}
            for sample in base["per_sample"]
        ],
    }


def _print_summary(report: dict) -> None:
    summary = report["summary"]
    print("组：%s  档位：%s  模型：%s" % (
        report["label"],
        "官方原文" if report["levels"]["official"] else "非官方描述（结论不能和文献比较）",
        report["settings"]["LLM_PROVIDER"],
    ))
    print("有效样本 %s 篇；无总分 %s 篇（判失败或评分项待人工定分，不进指标）；评分政策标记复核 %s 篇（总分照常计入）" % (
        summary["n"], summary["missing_total"], summary["policy_review"],
    ))
    if summary["not_run_total"]:
        print("没跑完 %s 篇（文件缺失、解析或评分出错，原因见报告 runs.errors）" % summary["not_run_total"])
    for name in ("qwk", "mae", "exact", "adjacent", "mean_bias"):
        value = summary[name]
        print("  %-9s %s" % (name, "-" if value is None else round(value, 4)))


def _require_outside(path, what: str) -> Path:
    """相对路径按当前目录解析（与应用读设置时一致）；落在仓库里就拒绝。

    不展开 ``~``：SQLAlchemy（只做 ``os.path.abspath``）和 ``STORAGE_ROOT`` 都不展开，
    ``~/eval.db`` 实际是当前目录下名为 ``~`` 的目录。命令行参数在读入时已展开。
    """

    resolved = Path(path).resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
        raise GateValidationError("%s 在仓库里（%s），评测数据只能放在仓库外" % (what, resolved))
    return resolved


def _sqlite_file(database_url: str) -> Path | None:
    """SQLite 文件库返回文件路径；内存库与其它数据库返回 None。

    URI 形式（``sqlite:///file:<路径>?uri=true``）取 ``file:`` 后面的路径；
    ``mode=memory`` 与 ``file::memory:`` 是内存库。不带 ``uri=true`` 时
    ``file:...`` 只是一个普通的相对文件名，按原样检查。
    """

    from urllib.parse import unquote
    from urllib.parse import urlsplit

    from sqlalchemy.engine import make_url
    from sqlalchemy.util import asbool

    url = make_url(database_url)
    database = url.database
    if url.get_backend_name() != "sqlite" or not database or database == ":memory:":
        return None
    if asbool(url.query.get("uri", False)) and database.startswith("file:"):
        if url.query.get("mode") == "memory":
            return None
        database = unquote(urlsplit(database).path)
        if not database or database == ":memory:":
            return None
    return Path(database)


def _report_path(data_dir: Path, label: str) -> Path:
    """报告固定写在 <data>/reports/ 下；组名只能是一个名字，不能带路径。

    报告文件不能事先存在（包括指向别处的符号链接），写入时再用独占创建兜底。
    """

    if not label or label in {".", ".."} or Path(label).name != label or "\\" in label:
        raise ValueError("--label 只能是一个名字，不能带路径：%s" % label)
    reports = _require_outside(data_dir / "reports", "报告目录")
    if reports.exists() and not reports.is_dir():
        raise ValueError("报告目录不是目录：%s" % reports)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = reports / ("%s-%s.json" % (label, stamp))
    if path.is_symlink() or path.exists():
        raise ValueError("报告文件已存在（或是符号链接），换一个组名再跑：%s" % path)
    return path


def check_run_environment(data_dir: Path, label: str) -> Path:
    """开始评分前检查：报告、SQLite 数据库文件与 STORAGE_ROOT 都要在仓库外。返回报告路径。"""

    path = _report_path(data_dir, label)
    database = _sqlite_file(str(settings.DATABASE_URL))
    if database is not None:
        _require_outside(database, "SQLite 数据库（DATABASE_URL）")
    # 用远程存储时文档也会缓存在本地 STORAGE_ROOT 下，所以不分存储方式都检查。
    _require_outside(settings.STORAGE_ROOT, "STORAGE_ROOT")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ASAP 2.0 外部基准评测（不是发布门禁）")
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="分层抽样，生成 .docx、成绩表与清单")
    prep.add_argument("--csv", required=True)
    prep.add_argument("--out", required=True)
    prep.add_argument("--size", type=int, default=300)
    prep.add_argument("--seed", type=int, default=20261104)
    prep.add_argument("--with-sources", action="store_true", help="把材料原文附在作文后面")
    prep.add_argument("--exclude", help="不参与抽样的 essay_id 列表文件（每行一个，如用作示例的作文）")
    run = commands.add_parser("run", help="建评分标准、评分并出报告")
    run.add_argument("--data", required=True)
    run.add_argument("--levels", required=True, help="档位描述 JSON")
    run.add_argument("--label", required=True, help="这一组的名字，写进报告文件名（不能带路径）")
    run.add_argument("--rubric-id", help="沿用已发布的评分标准（必须是同一份档位文件建的），不再新建")
    args = parser.parse_args(argv)

    try:
        if args.command == "prepare":
            out = ensure_external_artifact_dir(Path(args.out).expanduser(), PROJECT_ROOT)
            exclude = []
            if args.exclude:
                exclude = [line.strip() for line in Path(args.exclude).read_text(encoding="utf-8").splitlines() if line.strip()]
            manifest = asap.prepare(args.csv, out, size=args.size, seed=args.seed, with_sources=args.with_sources, exclude=exclude)
            print("已写入 %s：%d 篇，各分数 %s" % (out, manifest["size"], manifest["score_counts"]))
            return 0

        data_dir = ensure_external_artifact_dir(Path(args.data).expanduser(), PROJECT_ROOT)
        levels = asap.load_levels(args.levels)
        path = check_run_environment(data_dir, args.label)
    except (GateValidationError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    from fastapi.testclient import TestClient

    from backend.app.db.session import SessionLocal
    from backend.app.main import app

    db = SessionLocal()
    try:
        with TestClient(app) as client:
            report = evaluate(db, client, data_dir, levels, label=args.label, rubric_id=args.rubric_id)
    except (RuntimeError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        db.close()
    _print_summary(report)  # 先打印：报告万一写不进去，指标也不至于丢
    try:
        path.parent.mkdir(exist_ok=True)
        # "x" 独占创建：评分期间有人在这个位置放了文件或符号链接，就拒绝而不是跟随它写。
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    except OSError as exc:
        print("报告写不进去：%s" % exc, file=sys.stderr)
        return 2
    print("报告：%s" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
