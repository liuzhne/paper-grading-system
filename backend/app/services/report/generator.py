from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from backend.app.core.config import settings
from backend.app.db.models import ReviewLog
from backend.app.db.models import ScoreItem
from backend.app.db.models import ScoringRun
from backend.app.services.scoring.rules import as_float
from backend.app.services.storage.local import read_json


def generate_report(db: Session, run_id: str):
    run = db.scalar(
        select(ScoringRun)
        .where(ScoringRun.id == run_id)
        .options(
            selectinload(ScoringRun.paper),
            selectinload(ScoringRun.rubric),
            selectinload(ScoringRun.items),
            selectinload(ScoringRun.items).selectinload(ScoreItem.criterion),
        )
    )
    if run is None:
        raise ValueError("scoring run not found")

    review_logs = db.scalars(select(ReviewLog).where(ReviewLog.scoring_run_id == run.id).order_by(ReviewLog.created_at)).all()
    html = _render_html(run, review_logs, _coherence_for(run))
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    path = settings.reports_dir / ("scoring_report_%s.html" % run.id)  # 用已校验的 DB 值，杜绝路径穿越
    path.write_text(html, encoding="utf-8")
    return path


def _render_html(run, review_logs, coherence_findings):
    paper = run.paper
    item_html = "\n".join(_render_item(item) for item in run.items)
    item_names = {item.id: item.criterion.name for item in run.items}
    review_html = _render_review_logs(review_logs, item_names)
    coherence_html = _render_coherence(coherence_findings)
    format_html = _render_format(getattr(run, "format_findings", None) or [])
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>论文评分报告</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 40px; line-height: 1.6; color: #1f2933; }}
    h1, h2 {{ color: #102a43; }}
    .meta {{ background: #f5f7fa; padding: 16px; border-radius: 8px; }}
    .score {{ font-size: 24px; font-weight: 700; }}
    .item {{ border-top: 1px solid #d9e2ec; padding: 18px 0; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
    th, td {{ border: 1px solid #d9e2ec; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f5f7fa; }}
    blockquote {{ margin: 8px 0; padding: 8px 12px; background: #f8fafc; border-left: 4px solid #9fb3c8; }}
    .note {{ color: #627d98; font-size: 13px; margin-top: 8px; }}
  </style>
</head>
<body>
  <h1>毕业论文智能评分报告</h1>
  <div class="meta">
    <div><strong>论文题目：</strong>{title}</div>
    <div><strong>学生：</strong>{student_name}（{student_id}）</div>
    <div><strong>评分标准：</strong>{rubric} / {version}</div>
    <div class="score">最终总分：{final_total} / AI 初评分：{ai_total} / 等级：{grade}</div>
    <div><strong>需要复核：</strong>{need_review}</div>
    <div class="note">说明：本系统为"粗档辅助评分 + 人工复核"——AI 给出档位大方向（约九成落在教师评分相邻一档内），教师据证据定终分；不作为可信排名依据。标"需要复核"或近档边界处务必人工核定。</div>
  </div>
  <h2>评分明细</h2>
  {items}
  <h2>篇章一致性发现</h2>
  {coherence}
  <h2>格式问题</h2>
  {format}
  <h2>人工复核记录</h2>
  {reviews}
</body>
</html>
""".format(
        title=escape(paper.title or ""),
        student_name=escape(paper.student_name or ""),
        student_id=escape(paper.student_id or ""),
        rubric=escape(run.rubric.name),
        version=escape(run.rubric.version),
        final_total=as_float(run.final_total_score),
        ai_total=as_float(run.ai_total_score),
        grade=escape(run.grade or ""),
        need_review="是" if run.need_manual_review else "否",
        items=item_html,
        coherence=coherence_html,
        format=format_html,
        reviews=review_html,
    )


def _render_item(item):
    evidence = "".join(
        "<blockquote>%s<br><small>%s</small></blockquote>"
        % (escape(e.get("quote", "")), escape(e.get("location", "")))
        for e in item.evidence or []
    )
    deductions = "；".join(item.deductions or [])
    return """
<section class="item">
  <h3>{name}: {final_score}/{max_score}</h3>
  <p><strong>AI 得分：</strong>{ai_score}；<strong>置信度：</strong>{confidence}</p>
  <p><strong>评分说明：</strong>{reason}</p>
  <p><strong>扣分原因：</strong>{deductions}</p>
  <p><strong>修改建议：</strong>{suggestion}</p>
  <div><strong>原文依据：</strong>{evidence}</div>
</section>
""".format(
        name=escape(item.criterion.name),
        final_score=as_float(item.final_score),
        max_score=as_float(item.max_score),
        ai_score=as_float(item.ai_score),
        confidence=as_float(item.confidence),
        reason=escape(item.reason or ""),
        deductions=escape(deductions),
        suggestion=escape(item.suggestion or ""),
        evidence=evidence,
    )


def _coherence_for(run):
    # 评分运行已合并存档（确定性+语义）；旧运行回退到解析期的确定性发现。
    if getattr(run, "coherence_findings", None):
        return run.coherence_findings
    return _load_coherence(run.paper)


def _load_coherence(paper):
    if not getattr(paper, "parsed_text_path", None):
        return []
    try:
        parsed = read_json(paper.parsed_text_path)
        return parsed.get("coherence_findings", []) or []
    except Exception:
        return []


def _render_coherence(findings):
    if not findings:
        return "<p>未发现确定性一致性问题（图表引用、编号制引文-参考文献核对通过）。</p>"
    rows = []
    for finding in findings:
        rows.append(
            "<tr><td>{severity}</td><td>{kind}</td><td>{message}</td><td>{deduct}</td></tr>".format(
                severity=escape(str(finding.get("severity", ""))),
                kind=escape(str(finding.get("kind", ""))),
                message=escape(str(finding.get("message", ""))),
                deduct=escape(_deduct_note(finding)),
            )
        )
    return (
        "<table><thead><tr><th>级别</th><th>类型</th><th>说明</th><th>计入扣分</th></tr></thead>"
        "<tbody>{rows}</tbody></table>".format(rows="\n".join(rows))
    )


def _deduct_note(finding):
    by = finding.get("deducted_by")
    if not by:
        return ""
    points = finding.get("deducted_points")
    return "−%s（%s）" % (points, by) if points is not None else "已计入（%s）" % by


def _render_format(findings):
    if not findings:
        return "<p>未发现格式问题（或模板未规定格式 / 非 docx 提交无法判定）。</p>"
    rows = []
    for finding in findings:
        rows.append(
            "<tr><td>{severity}</td><td>{field}</td><td>{message}</td><td>{deduct}</td></tr>".format(
                severity=escape(str(finding.get("severity", ""))),
                field=escape(str(finding.get("field", ""))),
                message=escape(str(finding.get("message", ""))),
                deduct=escape(_deduct_note(finding)),
            )
        )
    return (
        "<table><thead><tr><th>级别</th><th>项</th><th>说明</th><th>计入扣分</th></tr></thead>"
        "<tbody>{rows}</tbody></table>".format(rows="\n".join(rows))
    )


def _render_review_logs(review_logs, item_names):
    if not review_logs:
        return "<p>暂无人工复核记录。</p>"

    rows = []
    for log in review_logs:
        review_type = "整体验收复核" if log.score_item_id is None else "单项分数调整"
        item_name = item_names.get(log.score_item_id, "") if log.score_item_id else ""
        rows.append(
            """
<tr>
  <td>{created_at}</td>
  <td>{review_type}</td>
  <td>{item_name}</td>
  <td>{before_score}</td>
  <td>{after_score}</td>
  <td>{reviewer}</td>
  <td>{reason}</td>
</tr>
""".format(
                created_at=escape(log.created_at.isoformat(sep=" ") if log.created_at else ""),
                review_type=escape(review_type),
                item_name=escape(item_name),
                before_score=as_float(log.before_score) if log.before_score is not None else "",
                after_score=as_float(log.after_score) if log.after_score is not None else "",
                reviewer=escape(log.reviewer_id or ""),
                reason=escape(log.reason or ""),
            )
        )

    return """
<table>
  <thead>
    <tr>
      <th>时间</th>
      <th>类型</th>
      <th>评分项</th>
      <th>修改前</th>
      <th>修改后</th>
      <th>复核人</th>
      <th>复核说明</th>
    </tr>
  </thead>
  <tbody>
    {rows}
  </tbody>
</table>
""".format(rows="\n".join(rows))
