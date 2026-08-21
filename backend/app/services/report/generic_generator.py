"""Generic HTML renderer for submission-only Core runs."""

from __future__ import annotations

from html import escape
import json

from backend.app.core.config import settings
from backend.app.services.report.generic_export import build_run_export_v2


def _text(value):
    return "" if value is None else str(value)


def _evidence(values):
    if not values:
        return '<p class="muted">无证据条目。</p>'
    rows = []
    for value in values:
        label = value.get("type") or value.get("evidence_unit_id") or "evidence"
        detail = value.get("quote") or json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
        )
        location = value.get("location") or ""
        rows.append(
            "<li><strong>%s</strong> %s <small>%s</small></li>"
            % (
                escape(_text(label)),
                escape(_text(detail)),
                escape(_text(location)),
            )
        )
    return "<ul>%s</ul>" % "".join(rows)


def _criteria(criteria):
    sections = []
    for criterion in criteria:
        rule_rows = []
        rule_evidence = []
        for rule in criterion["rules"]:
            rule_rows.append(
                "<tr><td>{code}</td><td>{direction}</td><td>{status}</td>"
                "<td>{level}</td><td>{effect}</td><td>{observations}</td></tr>".format(
                    code=escape(_text(rule["rule_code"])),
                    direction=escape(_text(rule["direction"])),
                    status=escape(_text(rule["status"])),
                    level=escape(_text(rule["level_code"])),
                    effect=escape(_text(rule["calculated_effect"])),
                    observations=escape(
                        json.dumps(
                            rule["observations"],
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    ),
                )
            )
            rule_evidence.extend(rule["evidence"])
        sections.append(
            """
<section class="criterion">
  <h3>{code} · {name}</h3>
  <p><strong>分数：</strong>AI {ai} / 人工终分 {final} / 满分 {maximum}</p>
  <p><strong>自动状态：</strong>{status}；<strong>需复核：</strong>{review}</p>
  <p>{reason}</p>
  <table><thead><tr><th>规则</th><th>方向</th><th>状态</th><th>档位</th><th>效果</th><th>Observations</th></tr></thead>
  <tbody>{rules}</tbody></table>
  <h4>证据</h4>
  {evidence}
</section>
""".format(
                code=escape(_text(criterion["criterion_code"])),
                name=escape(_text(criterion["criterion_name"])),
                ai=escape(_text(criterion["ai_score"])),
                final=escape(_text(criterion["final_score"])),
                maximum=escape(_text(criterion["max_score"])),
                status=escape(_text(criterion["automatic_status"])),
                review="是" if criterion["need_manual_review"] else "否",
                reason=escape(_text(criterion["reason"])),
                rules="".join(rule_rows),
                evidence=_evidence(criterion["evidence"] + rule_evidence),
            )
        )
    return "".join(sections)


def _reviews(logs):
    if not logs:
        return '<p class="muted">暂无人工复核记录。</p>'
    rows = "".join(
        "<tr><td>{time}</td><td>{type}</td><td>{item}</td><td>{before}</td>"
        "<td>{after}</td><td>{reviewer}</td><td>{reason}</td></tr>".format(
            time=escape(_text(log["created_at"])),
            type=escape(_text(log["resolution_type"])),
            item=escape(_text(log["score_item_id"])),
            before=escape(_text(log["before_score"])),
            after=escape(_text(log["after_score"])),
            reviewer=escape(_text(log["reviewer_id"])),
            reason=escape(_text(log["reason"])),
        )
        for log in logs
    )
    return (
        "<table><thead><tr><th>时间</th><th>类型</th><th>评分项</th>"
        "<th>修改前</th><th>修改后</th><th>复核人</th><th>说明</th></tr>"
        "</thead><tbody>%s</tbody></table>" % rows
    )


def render_run_report_v2(export: dict) -> str:
    run = export["run"]
    submission = export["submission"]
    identity = export["identity"]
    identity_rows = "".join(
        "<tr><th>{key}</th><td>{value}</td></tr>".format(
            key=escape(key),
            value=escape(
                json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, (dict, list))
                else _text(value)
            ),
        )
        for key, value in identity.items()
    )
    extensions = escape(
        json.dumps(
            export["profile_extensions"],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>评分运行报告</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 36px; color: #243447; line-height: 1.55; }}
    h1, h2, h3 {{ color: #153e5c; }}
    .summary {{ background: #eef5f9; border-left: 5px solid #2878a8; padding: 16px 20px; }}
    .criterion {{ border-top: 1px solid #cad8e2; padding: 16px 0; }}
    table {{ border-collapse: collapse; width: 100%; margin: 10px 0 18px; }}
    th, td {{ border-bottom: 1px solid #dbe5ec; padding: 8px; text-align: left; vertical-align: top; overflow-wrap: anywhere; }}
    th {{ background: #f4f7f9; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #f7f9fb; padding: 14px; }}
    .muted, small {{ color: #607687; }}
  </style>
</head>
<body>
  <h1>评分运行报告</h1>
  <div class="summary">
    <div><strong>Run：</strong>{run_id}</div>
    <div><strong>Submission：</strong>{submission_id}</div>
    <div><strong>Profile：</strong>{profile_key} / {profile_version}</div>
    <div><strong>状态：</strong>{status}</div>
    <div><strong>总分：</strong>AI {ai_total} / 人工终分 {final_total} / 等级 {grade}</div>
  </div>
  <h2>运行身份</h2>
  <table><tbody>{identity}</tbody></table>
  <h2>评分项与规则</h2>
  {criteria}
  <h2>证据</h2>
  <p class="muted">证据按评分项与规则展示，均来自冻结 DocumentSnapshot。</p>
  <h2>人工复核记录</h2>
  {reviews}
  <h2>Profile 扩展</h2>
  <pre>{extensions}</pre>
</body>
</html>
""".format(
        run_id=escape(_text(run["id"])),
        submission_id=escape(_text(submission["id"])),
        profile_key=escape(_text(identity["business_profile_key"])),
        profile_version=escape(_text(identity["business_profile_version"])),
        status=escape(_text(run["status"])),
        ai_total=escape(_text(run["ai_total_score"])),
        final_total=escape(_text(run["final_total_score"])),
        grade=escape(_text(run["grade"])),
        identity=identity_rows,
        criteria=_criteria(export["criteria"]),
        reviews=_reviews(export["review_logs"]),
        extensions=extensions,
    )


def generate_report_v2(db, run_id: str):
    export = build_run_export_v2(db, run_id)
    html = render_run_report_v2(export)
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    path = settings.reports_dir / ("generic_scoring_report_%s.html" % run_id)
    path.write_text(html, encoding="utf-8")
    return path


__all__ = ["generate_report_v2", "render_run_report_v2"]
