"""L2 锚点策展编排（A2）——**本地运行、数据放仓库外**。

从 98 篇已评论文按教师分各档选锚点，抽取评分项相关段落、脱敏，写入 CalibrationAnchor（按 rubric/评分项
归档）。评分时 `get_anchors` 取同项锚点作 few-shot，给模型注入"差/中/优"区分度（A1 发现 QWK≈0 的真瓶颈）。

硬约束（设计§7/§15.3）：锚点必须**从留出集剔除**（否则评估泄漏）；锚点用**脱敏范文**（去姓名/学号）。
不调 LLM；纯抽取+落库。锚点文本来自真实学生论文 → 只写本地 cli.db，**严禁入库**（仓库 public，见
PII 事件记录）。

用法：
  .venv/bin/python -m backend.app.scripts.build_anchors \
    --rubric <rubric_id> \
    --papers-dir /path/to/待评分-docx \
    --scores /path/to/成绩表-评阅教师.xlsx \
    --holdout /path/to/成绩表-抽样15.xlsx \
    [--per-criterion 3] [--clear]
"""

import argparse
from pathlib import Path

from backend.app.cli.db import cli_session
from backend.app.cli.db import configure
from backend.app.cli.db import ensure_schema
from backend.app.db.models import CalibrationAnchor
from backend.app.db.models import Rubric
from backend.app.eval.labeled_dataset import load_scores_table
from backend.app.services.calibration.curate import anchor_rationale
from backend.app.services.calibration.curate import desensitize
from backend.app.services.calibration.curate import select_anchor_targets
from backend.app.services.calibration.library import EXCERPT_CHARS
from backend.app.services.document_parser.chunking import build_chunks
from backend.app.services.document_parser.parser import parse_document
from backend.app.services.retrieval.keyword import retrieve_for_criterion_in_chunks


def _resolve(papers_dir, filename):
    base = Path(papers_dir)
    for cand in (filename, filename + ".docx", filename + ".pdf"):
        if (base / cand).exists():
            return base / cand
    for path in base.iterdir():  # 模糊：文件名为前缀/包含
        if filename in path.name:
            return path
    return None


def _excerpt_for(criterion, parsed, top_k=2):
    chunks = build_chunks(parsed, paper_id="anchor")
    ranked = retrieve_for_criterion_in_chunks(chunks, criterion, top_k=top_k)
    text = "\n".join((c.get("text") or "") for c in ranked) if ranked else (parsed.full_text or "")
    return text.strip()


def main():
    ap = argparse.ArgumentParser(description="L2 锚点策展（本地，数据放仓库外）")
    ap.add_argument("--rubric", required=True)
    ap.add_argument("--papers-dir", required=True)
    ap.add_argument("--scores", required=True, help="全量教师成绩表（候选池）")
    ap.add_argument("--holdout", required=True, help="留出集成绩表（这些文件名将被剔除）")
    ap.add_argument("--per-criterion", type=int, default=3)
    ap.add_argument("--clear", action="store_true", help="先清空该 rubric 既有锚点（幂等重建）")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    configure(args.db or "~/.paper-grading/cli.db", "~/.paper-grading/storage")
    ensure_schema()

    rows = load_scores_table(args.scores)
    holdout = {r["filename"] for r in load_scores_table(args.holdout)}
    print("候选池 %d 篇，留出集剔除 %d 篇" % (len(rows) - len(holdout & {r['filename'] for r in rows}), len(holdout)))

    with cli_session() as db:
        rubric = db.get(Rubric, args.rubric)
        if rubric is None:
            raise SystemExit("rubric not found: %s" % args.rubric)
        criteria = {c.code: c for c in rubric.criteria}
        targets = select_anchor_targets(rows, holdout, list(criteria), max_anchors=args.per_criterion)

        if args.clear:
            db.query(CalibrationAnchor).filter_by(rubric_id=rubric.id).delete()
            db.commit()

        created = 0
        for code, picks in targets.items():
            criterion = criteria[code]
            for pick in picks:
                source = _resolve(args.papers_dir, pick["filename"])
                if source is None:
                    print("  [skip] 未找到论文：%s" % pick["filename"])
                    continue
                parsed = parse_document(str(source))
                excerpt = desensitize(
                    _excerpt_for(criterion, parsed),
                    names=[parsed.student_name], ids=[parsed.student_id],
                )[:EXCERPT_CHARS]
                if not excerpt:
                    print("  [skip] 空摘录：%s/%s" % (code, pick["filename"]))
                    continue
                db.add(CalibrationAnchor(
                    rubric_id=rubric.id, criterion_code=code,
                    score=pick["score"], max_score=float(criterion.max_score),
                    label=pick["level"], excerpt=excerpt,
                    rationale=anchor_rationale(criterion.name, pick["score"], criterion.max_score, pick["level"]),
                    source="脱敏范文",
                ))
                created += 1
                print("  [ok] %s %s档 score=%g (%s)" % (code, pick["level"], pick["score"], source.name))
        db.commit()
    print("完成：写入 %d 条锚点到 rubric %s" % (created, args.rubric))


if __name__ == "__main__":
    main()
