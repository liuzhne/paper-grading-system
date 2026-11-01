"""只读查看一份 Word 模板会被读成什么样（docs/Word评分规范抽取与对应方案.md 分期 A）。

用法::

    python -m backend.app.scripts.inspect_requirement_items 模板.docx
    python -m backend.app.scripts.inspect_requirement_items 模板.docx --rules 评分表.xlsx
    python -m backend.app.scripts.inspect_requirement_items 模板.docx --json > 结果.json

只读本地文件：不连数据库、不调模型、不写任何东西，开关 ``REQUIREMENT_ITEMS_ENABLED`` 关着也能跑。
带 ``--rules`` 时走真实的导入流水线，能看到 Word 佐证预填和发布阻断；不带时没有 Excel 可比对。
输出包含模板原文，真实模板的结果不要提交到仓库。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from backend.app.core.config import settings
from backend.app.services.rubric_import import pipeline
from backend.app.services.rubric_import.classification.signals import profile_signal_terms
from backend.app.services.rubric_import.coverage import compute_coverage
from backend.app.services.rubric_import.extraction.docx_extractor import mark_remaining_blocks
from backend.app.services.rubric_import.requirements.items import render_item
from backend.app.services.rubric_import.requirements.outline import section_path
from backend.app.services.rubric_import.requirements.state import assemble_requirements
from backend.app.services.rubric_import.requirements.state import template_read_warnings
from backend.app.services.rubric_import.sources.docx_adapter import load_docx
from backend.app.services.rubric_import.sources.units import SourceLedger

DOCX_ID = "word"


def _with_rules(template: bytes, rules: bytes, profile_key: str) -> dict:
    command = {
        "schema_version": pipeline.IMPORT_SCHEMA_VERSION,
        "source_kind": "file_import",
        "rubric": {"name": "只读核对", "version": "inspect", "business_profile_key": profile_key},
        "files": {"rules_file_name": "rules.xlsx", "template_file_name": "template.docx"},
        "compiler": {"parser_version": "inspect", "compiler_version": "inspect", "prompt_version": "none"},
        "version": {"hash_scheme": "rubric-content-v2"},
    }
    previous = settings.REQUIREMENT_ITEMS_ENABLED
    settings.REQUIREMENT_ITEMS_ENABLED = True
    try:
        graph = pipeline.prepare_file_import(command=command, rules_bytes=rules, template_bytes=template).to_mapping()
    finally:
        settings.REQUIREMENT_ITEMS_ENABLED = previous
    raw = graph["compilation"]["raw_parse_output"]
    return {
        "ledger": SourceLedger.from_mapping(raw["source_ledger"]),
        "outline": raw["outline"],
        "items": raw["requirement_items"],
        "coverage": raw["coverage"],
        "warnings": graph["compilation"]["warnings"],
    }


def _without_rules(template: bytes, profile_key: str) -> dict:
    ledger = SourceLedger()
    view = load_docx(template, ledger, doc_id=DOCX_ID, doc_role="template", deep=True)
    payload = assemble_requirements(view, ledger)
    terms = profile_signal_terms(profile_key)
    mark_remaining_blocks(view, ledger, profile_terms=terms)
    for comment in view.comments:  # 和导入一样：批注已认领到 template_items，不进待处理
        ledger.claim(comment["unit_id"], f"template_items.WORD-COMMENT-{comment['comment_id']}")
    return {
        "ledger": ledger,
        "outline": payload["outline"],
        "items": payload["requirement_items"],
        "coverage": compute_coverage(ledger, profile_terms=terms),
        "warnings": template_read_warnings(view.read_report),
    }


def inspect(template: bytes, *, rules: bytes | None = None, profile_key: str = "general_document") -> dict:
    data = _with_rules(template, rules, profile_key) if rules else _without_rules(template, profile_key)
    ledger, outline, items = data["ledger"], data["outline"], data["items"]
    word_units = [unit for unit in ledger.units(doc_id=DOCX_ID)]
    blocking = [unit for unit in data["coverage"]["unclaimed"] if unit["blocking"] and unit["doc_id"] == DOCX_ID]
    return {
        "read_report": items.get("read_report"),
        "warnings": [item.get("message") if isinstance(item, dict) else str(item) for item in data["warnings"]],
        "counts": {
            "units": dict(Counter(unit.kind for unit in word_units)),
            "status": dict(Counter(ledger.status(unit.unit_id).status for unit in word_units)),
            "items": dict(Counter(item["kind"] for item in items["items"])),
            "excluded": dict(Counter(entry["reason"] for entry in items["excluded"])),
            "table_headers": len(items["table_headers"]),
            "sections": dict(Counter(node["source"] for node in outline["nodes"])),
            "blocking": len(blocking),
        },
        "outline": outline["nodes"],
        "items": [
            {
                "item_id": item["item_id"],
                "kind": item["kind"],
                "section_path": section_path(outline, item["section_id"]),
                "text": render_item(item, ledger),
                "unit_ids": item["unit_ids"],
                "prefill": item["prefill"],
            }
            for item in items["items"]
        ],
        "excluded": [
            {**entry, "text": ledger.unit(entry["unit_id"]).text} for entry in items["excluded"]
        ],
        "blocking": [{"unit_id": unit["unit_id"], "text": unit["text"], "signals": unit["signals"]} for unit in blocking],
    }


def _print_text(report: dict) -> None:
    print("== 读取情况 ==")
    print(json.dumps(report["read_report"], ensure_ascii=False))
    for message in report["warnings"]:
        print("警告：", message)
    print("\n== 统计 ==")
    print(json.dumps(report["counts"], ensure_ascii=False))
    print("\n== 章节树 ==")
    for node in report["outline"]:
        indent = "  " * max(node["level"], 0)
        print(f"{indent}{node['title']}  ({node['source']}, {node['unit_id']})")
    print("\n== 条目 ==")
    for item in report["items"]:
        section = " / ".join(item["section_path"]) or "（无章节）"
        prefill = f"  预填 {item['prefill']['criterion_codes']}" if item["prefill"] else ""
        print(f"[{item['kind']}] {section}{prefill}  {item['unit_ids']}")
        for line in item["text"].splitlines():
            print(f"    {line}")
    print("\n== 排除 ==")
    for entry in report["excluded"]:
        print(f"[{entry['reason']}] {entry['unit_id']}: {entry['text']}")
    print("\n== 会阻断发布的单元 ==")
    for unit in report["blocking"]:
        print(f"{unit['unit_id']} {unit['signals']}: {unit['text']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="只读查看 Word 模板的章节和评分规范条目。")
    parser.add_argument("template", type=Path, help="Word 模板（.docx）")
    parser.add_argument("--rules", type=Path, help="对应的 Excel 评分表（可选）")
    parser.add_argument("--profile", default="general_document", help="业务类型，默认 general_document")
    parser.add_argument("--json", action="store_true", help="输出 JSON，方便比对")
    args = parser.parse_args(argv)
    report = inspect(
        args.template.read_bytes(),
        rules=args.rules.read_bytes() if args.rules else None,
        profile_key=args.profile,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
