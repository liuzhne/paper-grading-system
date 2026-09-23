"""LLM 结构建议的差异、合入计划与指纹（解析重构方案 §8、阶段 6）。

合入 = 按用户确认的结构从台账重新解析，生成新草稿取代当前草稿。为避免重新解析
静默改写或丢失内容：新增 / 补全可一键合入；修改须逐条确认（未确认的字段保留当前值）；
当前评分项在新结构下消失（removed）一律阻断——已有评分项被旧版本原子规则引用，
只能增、改，不能删；冲突（同名重复、编号撞车）须排除后才能合入。
"""

from __future__ import annotations

import json
from collections import Counter
from hashlib import sha256

from backend.app.services.rubric_import.extraction.llm_structure import STRUCTURE_PROMPT_VERSION

DIFF_FIELDS = ("max_score", "description", "deduction_rules")
RECORD_KEYS = {"name": "评分项", "max_score": "分值", "description": "评分说明", "deduction_rules": "扣分规则"}


def criterion_view(item) -> dict:
    rules = item.get("deduction_rules") or []
    return {
        "code": str(item.get("code") or ""),
        "name": str(item.get("name") or "").strip(),
        "max_score": float(item.get("max_score") or 0),
        "description": (str(item.get("description")).strip() or None) if item.get("description") else None,
        "deduction_rules": [str(rule) for rule in rules] if isinstance(rules, list) else [str(rules)],
        "row_number": item.get("row_number"),
    }


def _empty(value) -> bool:
    return value in (None, "", [], 0.0)


def diff_criteria(current, proposed) -> list[dict]:
    current = [criterion_view(item) for item in current]
    proposed = [criterion_view(item) for item in proposed]
    by_name = {item["name"]: item for item in current}
    current_codes = {item["code"] for item in current}
    duplicate_names = {name for name, count in Counter(p["name"] for p in proposed).items() if count > 1}
    matched: set[str] = set()
    items: list[dict] = []
    for item in proposed:
        row = item["row_number"]
        if item["name"] in duplicate_names:
            items.append({"id": f"conflict:{item['code']}:row{row}", "kind": "conflict", "code": item["code"],
                          "row_number": row, "reason": "duplicate_name", "after": item})
            continue
        existing = by_name.get(item["name"])
        if existing is None:
            if item["code"] in current_codes:
                items.append({"id": f"conflict:{item['code']}:row{row}", "kind": "conflict", "code": item["code"],
                              "row_number": row, "reason": "code_collision", "after": item})
            else:
                items.append({"id": f"new:{item['code']}:row{row}", "kind": "new", "code": item["code"],
                              "row_number": row, "after": item})
            continue
        matched.add(existing["code"])
        for field_name in DIFF_FIELDS:
            before, after = existing[field_name], item[field_name]
            if before == after or (_empty(before) and _empty(after)):
                continue
            kind = "fill" if _empty(before) else "modify"
            items.append({"id": f"{kind}:{existing['code']}:{field_name}", "kind": kind, "code": existing["code"],
                          "field": field_name, "row_number": row, "before": before, "after": after})
    for item in current:
        if item["code"] not in matched:
            items.append({"id": f"removed:{item['code']}", "kind": "removed", "code": item["code"], "before": item})
    return items


def merge_plan(items, *, confirm=frozenset(), exclude=frozenset()) -> dict:
    blocked, keep_values, exclude_rows = [], [], []
    for item in items:
        kind = item["kind"]
        if kind == "removed":
            # 已有评分项被旧版本原子规则引用，重新解析只能增、改，不能删。
            blocked.append(item["id"])
        elif kind == "conflict":
            if item["id"] in exclude:
                exclude_rows.append(item["row_number"])
            else:
                blocked.append(item["id"])
        elif kind == "new" and item["id"] in exclude:
            exclude_rows.append(item["row_number"])
        elif (kind == "modify" and item["id"] not in confirm) or (kind == "fill" and item["id"] in exclude):
            keep_values.append({"row_number": item["row_number"], "field": item["field"], "value": item["before"]})
    return {"blocked": blocked, "keep_values": keep_values, "exclude_rows": sorted(set(exclude_rows))}


def structure_fingerprint(ledger_mapping, current_criteria, override) -> str:
    canonical = json.dumps(
        {
            "prompt_version": STRUCTURE_PROMPT_VERSION,
            "units": [[u["unit_id"], u["text"]] for u in (ledger_mapping or {}).get("units") or []],
            "criteria": [
                {k: v for k, v in criterion_view(item).items() if k != "row_number"} for item in current_criteria
            ],
            "override": override,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()
