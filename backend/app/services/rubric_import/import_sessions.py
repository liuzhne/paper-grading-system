"""数据库临时导入会话。

文件解析与正式评分标准生命周期之间必须有一道人为确认边界。本模块保存可编辑
草稿，并在确认前把 prepared graph 保持为不可执行数据；正式 Rubric 仍由既有
``persist_prepared_import`` 短事务创建。
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from decimal import InvalidOperation
from decimal import ROUND_HALF_UP
from hashlib import sha256

from sqlalchemy.orm import Session

from backend.app.db import models


def _decimal_text(value: object) -> str:
    number = Decimal(str(value))
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _round_score(value: object) -> tuple[int, str, bool]:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("评分项分值必须是数字") from exc
    rounded = int(number.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if rounded <= 0:
        raise ValueError("评分项分值四舍五入后必须大于 0")
    return rounded, _decimal_text(number), number != Decimal(rounded)


def _source_refs(graph: dict, criterion: dict) -> list[dict]:
    tokens = {
        str(criterion.get("code") or ""),
        str(criterion.get("parent_criterion_code") or ""),
    }
    refs = []
    for source in graph.get("source_rules") or []:
        if str(source.get("source_rule_code") or "") not in tokens:
            continue
        refs.append(
            {
                "kind": "excel" if source.get("sheet_name") != "word" else "word",
                "sheet_name": source.get("sheet_name"),
                "row_number": source.get("row_number"),
                "locator": source.get("cell_locator"),
                "text": source.get("raw_text"),
            }
        )
    return refs


def draft_from_prepared(prepared: dict) -> tuple[dict, list[dict]]:
    criteria = []
    adjustments = []
    by_code: dict[str, dict] = {}
    for index, source in enumerate(prepared["legacy_projection"]["criteria"]):
        rounded, original, changed = _round_score(source["max_score"])
        code = str(source["code"])
        refs = _source_refs(prepared, source)
        existing = by_code.get(code)
        if existing is not None:
            if existing["name"] != str(source["name"]) or existing["max_score"] != rounded:
                raise ValueError(f"评分项编号 {code} 对应了不一致的名称或分值，请先修正源文件")
            seen_refs = {repr(item) for item in existing["source_refs"]}
            existing["source_refs"].extend(item for item in refs if repr(item) not in seen_refs)
            continue
        item = {
            "code": code,
            "name": str(source["name"]),
            "max_score": rounded,
            "description": source.get("description"),
            "display_order": len(criteria),
            "source_refs": refs,
            "parse_status": "rounded" if changed else "parsed",
            "deleted": False,
        }
        by_code[code] = item
        criteria.append(item)
        if changed:
            adjustments.append(
                {
                    "code": code,
                    "original": original,
                    "rounded": rounded,
                    "message": f"{code} 分值 {original} 已按四舍五入调整为 {rounded}。",
                }
            )
    raw = prepared["compilation"].get("raw_parse_output") or {}
    draft = {
        "criteria": criteria,
        "template_summary": deepcopy(raw.get("template_summary") or {}),
        "coverage": deepcopy(raw.get("coverage") or {}),
        "conflicts": deepcopy(raw.get("source_conflicts") or []),
    }
    return draft, adjustments


def create(
    db: Session,
    *,
    prepared: dict,
    actor_id: str,
    organization_id: str | None,
    visibility: str,
    rules_file_name: str | None = None,
    rules_file_bytes: bytes | None = None,
    template_file_name: str | None = None,
    template_file_bytes: bytes | None = None,
) -> models.RubricImportSession:
    draft, adjustments = draft_from_prepared(prepared)
    rubric = prepared["rubric"]
    warnings = [
        item if isinstance(item, str) else str(item.get("message") or item.get("code"))
        for item in prepared["compilation"].get("warnings") or []
    ]
    row = models.RubricImportSession(
        organization_id=organization_id,
        owner_id=actor_id,
        name=str(rubric["name"]),
        version=str(rubric["version"]),
        description=rubric.get("description"),
        visibility=visibility,
        total_score=sum(item["max_score"] for item in draft["criteria"]),
        prepared_graph=deepcopy(prepared),
        draft_data=draft,
        warnings=warnings,
        score_adjustments=adjustments,
        rules_file_name=rules_file_name,
        rules_file_bytes=rules_file_bytes,
        template_file_name=template_file_name,
        template_file_bytes=template_file_bytes,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def serialize(row: models.RubricImportSession) -> dict:
    draft = row.draft_data or {}
    return {
        "id": row.id,
        "status": row.status,
        "state_version": row.state_version,
        "rubric_id": row.rubric_id,
        "name": row.name,
        "version": row.version,
        "description": row.description,
        "visibility": row.visibility,
        "total_score": row.total_score,
        "criteria": deepcopy(draft.get("criteria") or []),
        "warnings": list(row.warnings or []),
        "score_adjustments": list(row.score_adjustments or []),
        "files": {
            "rules": row.rules_file_name,
            "template": row.template_file_name,
        },
        "template_summary": deepcopy(draft.get("template_summary") or {}),
        "coverage": deepcopy(draft.get("coverage") or {}),
        "conflicts": deepcopy(draft.get("conflicts") or []),
        "expires_at": row.expires_at,
    }


def update(row: models.RubricImportSession, payload: dict) -> None:
    if row.status != "draft":
        raise ValueError("只有待确认的导入会话可以修改")
    if row.state_version != payload["expected_state_version"]:
        raise RuntimeError("导入草稿已被其他操作更新，请刷新后重试")
    for field in ("name", "version", "description", "total_score"):
        if field in payload and payload[field] is not None:
            setattr(row, field, payload[field])
    if payload.get("criteria") is not None:
        criteria = deepcopy(payload["criteria"])
        codes = [item["code"] for item in criteria if not item.get("deleted")]
        if len(codes) != len(set(codes)):
            raise ValueError("评分项编号必须唯一")
        row.draft_data = {**deepcopy(row.draft_data or {}), "criteria": criteria}
    row.state_version += 1


def resolve_conflict(
    row: models.RubricImportSession,
    conflict_id: str,
    *,
    expected_state_version: int,
    decision: str,
    reason: str,
) -> None:
    if row.status != "draft":
        raise ValueError("只有待确认的导入会话可以处理来源冲突")
    if row.state_version != expected_state_version:
        raise RuntimeError("导入草稿已被其他操作更新，请刷新后重试")
    draft = deepcopy(row.draft_data or {})
    conflicts = draft.get("conflicts") or []
    target = next(
        (item for item in conflicts if item.get("anchor_unit_id") == conflict_id),
        None,
    )
    if target is None:
        raise LookupError("来源冲突不存在")
    if target.get("resolved"):
        raise ValueError("来源冲突已经处理")
    if decision not in {"use_excel", "use_word"}:
        raise ValueError("不支持的来源冲突处理方式")

    criteria = draft.get("criteria") or []
    if decision == "use_word":
        word_code = str(target.get("word_code") or "")
        existing = next((item for item in criteria if item.get("code") == word_code), None)
        rounded, original, changed = _round_score(target.get("word_max_score"))
        if existing is None:
            criteria.append(
                {
                    "code": word_code,
                    "name": str(target.get("word_name") or word_code),
                    "max_score": rounded,
                    "description": None,
                    "display_order": len(criteria),
                    "source_refs": [
                        {"kind": "word", "locator": target.get("anchor_unit_id")}
                    ],
                    "parse_status": "added_requires_confirmation",
                    "deleted": False,
                }
            )
        else:
            existing["max_score"] = rounded
            existing["parse_status"] = "changed_requires_confirmation"
        if changed:
            adjustments = list(row.score_adjustments or [])
            adjustments.append(
                {
                    "code": word_code,
                    "original": original,
                    "rounded": rounded,
                    "message": f"{word_code} 分值 {original} 已按四舍五入调整为 {rounded}。",
                }
            )
            row.score_adjustments = adjustments

    target.update({"resolved": True, "decision": decision, "reason": reason})
    draft["criteria"] = criteria
    draft["conflicts"] = conflicts
    row.draft_data = draft
    row.state_version += 1


def cancel(row: models.RubricImportSession, *, expected_state_version: int) -> None:
    if row.status != "draft":
        raise ValueError("只有待确认的导入会话可以取消")
    if row.state_version != expected_state_version:
        raise RuntimeError("导入草稿已被其他操作更新，请刷新后重试")
    row.status = "cancelled"
    row.state_version += 1


def validate_confirmable(row: models.RubricImportSession) -> None:
    draft = row.draft_data or {}
    criteria = [item for item in draft.get("criteria") or [] if not item.get("deleted")]
    if not criteria:
        raise ValueError("至少需要一个评分项")
    codes = [str(item.get("code") or "") for item in criteria]
    if any(not code for code in codes) or len(codes) != len(set(codes)):
        raise ValueError("评分项编号不能为空且必须唯一")
    total = sum(int(item["max_score"]) for item in criteria)
    if total != row.total_score:
        raise ValueError(f"评分项分值合计 {total} 与满分 {row.total_score} 不一致")
    unresolved = [item for item in draft.get("conflicts") or [] if not item.get("resolved")]
    if unresolved:
        raise ValueError(f"仍有 {len(unresolved)} 个来源冲突需要确认")


def prepared_for_confirmation(row: models.RubricImportSession) -> dict:
    """把人工确认的第一步草稿投影回既有 prepared graph。"""

    validate_confirmable(row)
    graph = deepcopy(row.prepared_graph)
    active = [
        deepcopy(item)
        for item in (row.draft_data or {}).get("criteria") or []
        if not item.get("deleted")
    ]
    edits = {item["code"]: item for item in active}
    original = {str(item["code"]): item for item in graph.get("criteria") or []}
    criteria = []
    for order, edit in enumerate(active):
        value = deepcopy(original.get(edit["code"]) or {})
        value.update(
            {
                "code": edit["code"],
                "name": edit["name"],
                "max_score": str(edit["max_score"]),
                "description": edit.get("description"),
                "display_order": order,
                "weight": value.get("weight"),
                "evidence_hints": list(value.get("evidence_hints") or []),
                "deduction_rules": list(value.get("deduction_rules") or []),
                "criterion_type": value.get("criterion_type") or "llm_judgment",
                "scoring_mode": value.get("scoring_mode") or "review_only",
                "applies_to": value.get("applies_to") or "global",
                "rubric_levels": list(value.get("rubric_levels") or value.get("levels") or []),
                "sub_checks": list(value.get("sub_checks") or []),
                "dimension": value.get("dimension"),
                "deduction_rules_structured": list(value.get("deduction_rules_structured") or []),
            }
        )
        criteria.append(value)

    active_codes = set(edits)
    removed_rule_codes = {
        rule["rule_code"]
        for rule in graph.get("atomic_rules") or []
        if rule.get("criterion_code") not in active_codes
    }
    graph["criteria"] = criteria
    graph["legacy_projection"]["criteria"] = [deepcopy(item) for item in criteria]
    graph["atomic_rules"] = [
        rule for rule in graph.get("atomic_rules") or []
        if rule.get("criterion_code") in active_codes
    ]
    graph["template_links"] = [
        link for link in graph.get("template_links") or []
        if link.get("rule_code") not in removed_rule_codes
    ]
    graph["rubric"].update(
        {
            "name": row.name,
            "version": row.version,
            "description": row.description,
            "total_score": str(row.total_score),
        }
    )
    raw = graph["compilation"].setdefault("raw_parse_output", {})
    raw["criteria"] = deepcopy(criteria)
    policy = graph["version"].setdefault("global_policy", {})
    aggregation = policy.setdefault("aggregation", {})
    aggregation["total_score"] = str(row.total_score)
    return graph


def reupload_fingerprint(
    row: models.RubricImportSession,
    *,
    rules_bytes: bytes | None,
    template_bytes: bytes | None,
) -> str:
    digest = sha256()
    digest.update(row.id.encode("utf-8"))
    digest.update(str(row.state_version).encode("ascii"))
    for label, payload in ((b"rules", rules_bytes), (b"template", template_bytes)):
        digest.update(label)
        digest.update(payload or b"")
    return digest.hexdigest()


def reupload_diff(row: models.RubricImportSession, prepared: dict) -> tuple[dict, list[dict], list[dict]]:
    """生成差异和替换草稿；不修改会话。"""

    next_draft, adjustments = draft_from_prepared(prepared)
    previous_source, _ = draft_from_prepared(deepcopy(row.prepared_graph))
    baseline = {item["code"]: item for item in previous_source["criteria"]}
    current = {
        item["code"]: deepcopy(item)
        for item in (row.draft_data or {}).get("criteria") or []
    }
    incoming = {item["code"]: item for item in next_draft["criteria"]}
    merged = []
    differences = []
    for code, next_item in incoming.items():
        old_source = baseline.get(code)
        old_current = current.get(code)
        if old_source is None:
            value = deepcopy(next_item)
            value["parse_status"] = "added_requires_confirmation"
            change_type = "added"
        elif (
            old_source["name"] == next_item["name"]
            and old_source["max_score"] == next_item["max_score"]
        ):
            value = deepcopy(old_current or next_item)
            change_type = "unchanged"
        else:
            value = deepcopy(next_item)
            value["parse_status"] = "changed_requires_confirmation"
            change_type = "modified"
        merged.append(value)
        differences.append(
            {
                "code": code,
                "name": next_item["name"],
                "change_type": change_type,
                "old_name": (old_current or old_source or {}).get("name"),
                "new_name": next_item["name"],
                "old_max_score": (old_current or old_source or {}).get("max_score"),
                "new_max_score": next_item["max_score"],
            }
        )
    for code, old_item in current.items():
        if code in incoming:
            continue
        if code not in baseline:
            # 人工新增项不属于重新上传文件的差异范围，完整保留用户工作。
            merged.append(deepcopy(old_item))
            continue
        removed = {**deepcopy(old_item), "deleted": True, "parse_status": "removed"}
        merged.append(removed)
        differences.append(
            {
                "code": code,
                "name": old_item["name"],
                "change_type": "removed",
                "old_name": old_item["name"],
                "new_name": None,
                "old_max_score": old_item["max_score"],
                "new_max_score": None,
            }
        )
    next_draft["criteria"] = merged
    return next_draft, adjustments, differences


def apply_reupload(
    row: models.RubricImportSession,
    *,
    prepared: dict,
    draft: dict,
    adjustments: list[dict],
    rules_file_name: str | None,
    rules_file_bytes: bytes | None,
    template_file_name: str | None,
    template_file_bytes: bytes | None,
) -> None:
    if row.status != "draft":
        raise ValueError("只有待确认的导入会话可以重新上传")
    row.prepared_graph = deepcopy(prepared)
    row.draft_data = deepcopy(draft)
    row.warnings = [
        item if isinstance(item, str) else str(item.get("message") or item.get("code"))
        for item in prepared["compilation"].get("warnings") or []
    ]
    row.score_adjustments = deepcopy(adjustments)
    row.total_score = sum(
        int(item["max_score"])
        for item in draft.get("criteria") or []
        if not item.get("deleted")
    )
    row.rules_file_name = rules_file_name
    row.rules_file_bytes = rules_file_bytes
    row.template_file_name = template_file_name
    row.template_file_bytes = template_file_bytes
    row.state_version += 1


__all__ = [
    "create",
    "cancel",
    "draft_from_prepared",
    "prepared_for_confirmation",
    "reupload_diff",
    "reupload_fingerprint",
    "apply_reupload",
    "serialize",
    "resolve_conflict",
    "update",
    "validate_confirmable",
]
