#!/usr/bin/env python3
"""评分助手评测的报告（Agent 化方案 §27）。只用标准库：在 GitHub 的工作流机器上跑，不装项目依赖。

    python3 scripts/assistant_eval_report.py spent docs/评测报告/评分助手
        打印历次正式评测的费用合计（美元），供下一轮核对总预算。
    python3 scripts/assistant_eval_report.py render <实例输出> --previous-dir docs/评测报告/评分助手 --out <目录>
        从实例的输出里取出两行标记之间的 JSON；正式运行写出 <日期>-<期>-<模型>.md 与同名 .json，
        并把 Markdown 打印出来（工作流贴进运行摘要）；估算只打印摘要。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MARK_BEGIN = "=====ASSISTANT_EVAL_JSON_BEGIN====="
MARK_END = "=====ASSISTANT_EVAL_JSON_END====="


def extract(raw: str) -> dict:
    if MARK_BEGIN not in raw or MARK_END not in raw:
        raise ValueError("实例输出里没有评测结果的标记")
    body = raw.split(MARK_BEGIN, 1)[1].split(MARK_END, 1)[0]
    return json.loads(body)


def reports(directory: Path) -> list[dict]:
    found = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("schema") == 1 and data.get("mode") == "run":
            found.append(data)
    return sorted(found, key=lambda item: item.get("started_at") or "")


def spent(directory: Path) -> float:
    return round(sum(float((item.get("summary") or {}).get("cost_usd") or 0) for item in reports(directory)), 6)


def slug(model_name: str | None) -> str:
    name = str(model_name or "model").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "model"


def _yes(value) -> str:
    return "✅ 是" if value else "❌ 否"


def _pct(value) -> str:
    return "%.1f%%" % (float(value or 0) * 100)


def render_estimate(data: dict) -> str:
    estimate = data.get("estimate") or {}
    budget = data.get("budget") or {}
    lines = [
        "# 评分助手评测 · 估算（%s）" % data.get("phase"),
        "",
        "| 项 | 值 |", "|---|---|",
        "| 模型 | %s（%s，%s） |" % ((data.get("model") or {}).get("name"), (data.get("model") or {}).get("host"),
                                   (data.get("model") or {}).get("source")),
        "| 评测集 | %s 条（版本 %s） |" % ((data.get("eval_set") or {}).get("count"), (data.get("eval_set") or {}).get("version")),
        "| 预计模型调用 | %s 次 |" % estimate.get("model_calls"),
        "| 预计 token | 输入 %s，输出 %s（不计缓存折扣，偏高） |" % (estimate.get("prompt_tokens"), estimate.get("completion_tokens")),
        "| **预计费用** | **%.4f 美元** |" % float(estimate.get("usd") or 0),
        "| 本轮预算 | %.2f 美元 |" % float(budget.get("round_usd") or 0),
        "| 已花 / 总预算 | %.4f / %.2f 美元 |" % (float(budget.get("spent_before_usd") or 0), float(budget.get("total_usd") or 0)),
    ]
    if data.get("refused"):
        lines += ["", "**不能运行**：%s" % data["refused"]]
    return "\n".join(lines) + "\n"


def render_run(data: dict, previous: dict | None) -> str:
    summary = data.get("summary") or {}
    model = data.get("model") or {}
    budget = data.get("budget") or {}
    tokens = summary.get("tokens") or {}
    checks = summary.get("provider_checks") or {}
    search = summary.get("search") or {}
    call_tool = summary.get("call_tool") or {}
    latency = summary.get("latency_ms") or {}
    lines = [
        "# 评分助手评测报告 · %s · %s" % (data.get("phase"), model.get("name")),
        "",
        "## 运行信息", "",
        "| 项 | 值 |", "|---|---|",
        "| 开始 / 结束（UTC） | %s / %s |" % (data.get("started_at"), data.get("finished_at")),
        "| 代码提交 | `%s` |" % (data.get("commit") or "未知"),
        "| 模型 | %s（%s，连接类型 %s，来源 %s；不含密钥） |" % (model.get("name"), model.get("host"),
                                                        model.get("provider_type"), model.get("source")),
        "| 评测集 | %s 条，版本 `%s`；运行 %s 条，跳过 %s 条%s |" % (
            (data.get("eval_set") or {}).get("count"), (data.get("eval_set") or {}).get("version"),
            summary.get("ran"), summary.get("skipped"), ("（%s）" % data["stopped"]) if data.get("stopped") else ""),
        "| 数据 | 隔离的内存数据库 + 合成种子数据；没有学生信息 |",
        "",
        "## 费用", "",
        "| 项 | 值 |", "|---|---|",
        "| token | 输入 %s（其中缓存命中 %s），输出 %s |" % (tokens.get("prompt_tokens"), tokens.get("cached_tokens"),
                                                tokens.get("completion_tokens")),
        "| **本轮费用** | **%.4f 美元**（估算 %.4f，本轮预算 %.2f） |" % (
            float(summary.get("cost_usd") or 0), float((data.get("estimate") or {}).get("usd") or 0),
            float(budget.get("round_usd") or 0)),
        "| 累计（含本轮） / 总预算 | %.4f / %.2f 美元 |" % (
            float(budget.get("spent_before_usd") or 0) + float(summary.get("cost_usd") or 0), float(budget.get("total_usd") or 0)),
        "| 价目（每百万 token，美元） | %s |" % json.dumps(data.get("prices_usd_per_million"), ensure_ascii=False),
        "",
        "## 上线门槛", "",
        "| 门槛 | 结果 | 实际 |", "|---|---|---|",
        "| 动作准确率 ≥ 95%% | %s | %s（%s/%s） |" % (_yes((summary.get("gates") or {}).get("动作准确率 ≥ 95%")),
                                             _pct(summary.get("accuracy")), summary.get("passed"), summary.get("ran")),
        "| 违规 0 次 | %s | %s 次 |" % (_yes((summary.get("gates") or {}).get("违规 0 次")), summary.get("violations")),
        "| P90 延迟 ≤ 6 秒 | %s | P50 %s ms，P90 %s ms，最长 %s ms |" % (
            _yes((summary.get("gates") or {}).get("P90 延迟 ≤ 6 秒")), latency.get("p50"), latency.get("p90"), latency.get("max")),
        "",
        "## 各类别", "",
        "| 类别 | 通过 / 条数 |", "|---|---|",
    ]
    for category, entry in (summary.get("by_category") or {}).items():
        lines.append("| %s | %s / %s |" % (category, entry.get("passed"), entry.get("total")))
    lines += [
        "",
        "## 模型行为核对（方案 Q8）", "",
        "| 项 | 结果 |", "|---|---|",
        "| `tool_choice: \"required\"` 生效（模型从不直接给文字） | %s（直接给文字 %s 次） |" % (
            _yes(checks.get("tool_choice_required_honored")), checks.get("text_replies")),
        "| 函数声明的 schema 被接受（没有 400 降级） | %s（降级 %s 次：%s） |" % (
            _yes(checks.get("schema_accepted")), checks.get("degraded"), "、".join(checks.get("degraded_reasons") or []) or "无"),
        "| 工具调用上的思考签名 | %s 个 |" % checks.get("thought_signatures"),
        "| 缓存命中量有报告 | %s（命中占输入 %s） |" % (_yes(checks.get("cached_tokens_reported")),
                                              _pct(checks.get("cache_read_ratio"))),
        "",
        "## 搜索与 call_tool", "",
        "| 项 | 值 |", "|---|---|",
        "| 需要搜索的条目中搜到并用了正确工具 | %s / %s |" % (search.get("found_right"), search.get("needed")),
        "| 不该搜索却搜索了 | %s 条 |" % search.get("unneeded"),
        "| 延迟工具调用 / 参数不合格 | %s / %s |" % (call_tool.get("deferred_calls"), call_tool.get("param_errors")),
        "",
    ]
    failed = [item for item in data.get("items") or [] if not item.get("passed")]
    lines += ["## 失败条目", ""]
    if not failed:
        lines.append("无。")
    else:
        lines += ["| id | 说法 | 原因 | 实际（工具 / 结束方式） | 回复摘录 |", "|---|---|---|---|---|"]
        for item in failed:
            lines.append("| %s | %s | %s | %s / %s | %s |" % (
                item.get("id"), item.get("utterance"), "；".join(item.get("reasons") or []),
                "、".join(item.get("tools_called") or []) or "无", item.get("outcome"),
                str(item.get("reply_text") or "").replace("\n", " ").replace("|", "／")[:80]))
    lines += ["", "## 与上一轮对比", ""]
    if previous is None:
        lines.append("这是第一轮。")
    else:
        before = previous.get("summary") or {}
        lines += [
            "| 项 | 上一轮（%s） | 本轮 |" % previous.get("started_at"), "|---|---|---|",
            "| 动作准确率 | %s | %s |" % (_pct(before.get("accuracy")), _pct(summary.get("accuracy"))),
            "| 违规 | %s | %s |" % (before.get("violations"), summary.get("violations")),
            "| P90 延迟（ms） | %s | %s |" % ((before.get("latency_ms") or {}).get("p90"), latency.get("p90")),
            "| 费用（美元） | %.4f | %.4f |" % (float(before.get("cost_usd") or 0), float(summary.get("cost_usd") or 0)),
        ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="评分助手评测报告")
    sub = parser.add_subparsers(dest="command", required=True)
    spent_cmd = sub.add_parser("spent")
    spent_cmd.add_argument("directory", type=Path)
    render_cmd = sub.add_parser("render")
    render_cmd.add_argument("raw", type=Path)
    render_cmd.add_argument("--previous-dir", type=Path, required=True)
    render_cmd.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "spent":
        print("%.6f" % spent(args.directory))
        return 0
    data = extract(args.raw.read_text(encoding="utf-8"))
    if data.get("mode") != "run" or data.get("refused") or not data.get("summary"):
        print(render_estimate(data))
        return 0
    history = reports(args.previous_dir)
    markdown = render_run(data, history[-1] if history else None)
    name = "%s-%s-%s" % (str(data.get("started_at") or "")[:10], data.get("phase"), slug((data.get("model") or {}).get("name")))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / ("%s.json" % name)).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (args.out / ("%s.md" % name)).write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
