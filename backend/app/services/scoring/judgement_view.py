"""Core 评分项的判定视图（docs/评分证据与理由改造方案.md G2、G4）。

只读投影，读取时从 ``ScoreItem.rule_results`` 与运行冻结的执行计划算出，
新旧评分都适用，不改库里的数据：

* 逐条规则的判定：扣分 / 选档 / 未发现问题 / 不适用 / 证据不足 / 无效，未扣分
  的评分项也有说明。说明由代码按判定写，模型写的只有开关打开后的一句理由。
* 证据充分度（取代 Core 评分项上一直是“未提供”的置信度）：高 / 中 / 低，
  由代码按证据情况给出，附原因；不编造百分比，也不参与算分或复核。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from backend.app.services.scoring.evidence_view import project_core_refs


SCHEMA_VERSION = "judgement-view@1"

_LEVEL_LABELS = {"high": "高", "medium": "中", "low": "低"}
_EFFECT_LABELS = {
    "review": "需人工复核",
    "block_submission": "阻断提交",
    "report_only": "仅提示",
}


def rule_snapshots(run) -> dict:
    """``rule_code`` → 运行冻结的原子规则快照。"""

    plan = getattr(run, "execution_plan_snapshot", None) or {}
    return {
        node.get("rule_code"): node.get("atomic_rule_snapshot") or {}
        for node in plan.get("nodes") or ()
        if isinstance(node, dict) and node.get("atomic_rule_snapshot")
    }


def _amount(value) -> Decimal:
    try:
        return abs(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _points(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _level_text(rule, level_code) -> str:
    for level in rule.get("levels") or ():
        if level.get("level_code") == level_code:
            return level.get("descriptor") or level_code
    return level_code


def _row(result, rule, *, insufficient, frozen, units):
    status = result.get("status")
    name = rule.get("name") or result.get("rule_code")
    contributions = result.get("score_contributions") or ()
    deducted = sum(
        (_amount(entry.get("amount")) for entry in contributions if entry.get("kind") == "deduction"),
        Decimal("0"),
    )
    effect = None
    if status == "triggered":
        if result.get("selected_level_code") or rule.get("direction") == "band":
            outcome, label = "level", "选档"
            effect = "选「%s」" % _level_text(rule, result.get("selected_level_code"))
        elif rule.get("effect_type") in _EFFECT_LABELS:
            outcome, label = "flagged", _EFFECT_LABELS[rule["effect_type"]]
        elif deducted > 0:
            outcome, label = "deducted", "扣分"
            effect = "扣 %s 分" % _points(deducted)
        else:
            outcome, label = "triggered", "命中（已达扣分上限，未再扣分）"
    elif status == "not_triggered":
        outcome, label = (
            ("insufficient", "证据不足") if insufficient else ("not_triggered", "未发现问题")
        )
    elif status == "not_applicable":
        outcome, label = "not_applicable", "不适用"
    elif status == "skipped":
        outcome, label = "skipped", "未执行（前置规则未命中）"
    else:
        outcome, label = "invalid", "判定无效"

    refs = [ref for ref in result.get("evidence_refs") or () if isinstance(ref, dict)]
    basis = result.get("basis") if isinstance(result.get("basis"), dict) else None
    basis_refs = [
        ref
        for ref in (basis or {}).get("evidence_refs") or ()
        if isinstance(ref, dict) and ref.get("payload_hash") not in {r.get("payload_hash") for r in refs}
    ]
    return {
        "rule_code": result.get("rule_code"),
        "rule_name": name,
        "judge_type": rule.get("judge_type"),
        "outcome": outcome,
        "label": label,
        "effect": effect,
        "rationale": (basis or {}).get("rationale"),
        "evidence": project_core_refs(frozen, refs, units=units),
        "basis_evidence": project_core_refs(frozen, basis_refs, units=units),
        "has_basis": basis is not None,
        "rejected_quote_count": int((basis or {}).get("rejected_quote_count") or 0),
        "evidence_selection": result.get("evidence_selection"),
        # 互斥组一次调用只有一个依据（见 _share_group_basis）。
        "basis_scope": None,
        "basis_shared_with": None,
        "_group_code": (basis or {}).get("group_code"),
    }


_UNCHOSEN = {"not_triggered", "not_applicable", "skipped", "insufficient"}


def _share_group_basis(rows):
    """互斥组的依据只显示一次：挂在被选中的那一档（都没选中时挂在第一档）。

    组内其它档照常显示自己的判定，但不再把组的理由当成自己的理由——否则
    “未发现问题”的那一档下面会出现“缺陷在哪里”的说明。
    """

    groups = {}
    for row in rows:
        code = row.pop("_group_code")
        if code:
            groups.setdefault(code, []).append(row)
    for members in groups.values():
        owner = next((row for row in members if row["outcome"] not in _UNCHOSEN), members[0])
        for row in members:
            if row is owner:
                row["basis_scope"] = "group"
                continue
            row["rationale"] = None
            row["basis_evidence"] = []
            row["rejected_quote_count"] = 0
            row["basis_shared_with"] = owner["rule_name"]


def _sufficiency(rows):
    low, medium, notes = [], [], []
    for row in rows:
        name = row["rule_name"]
        if row["outcome"] == "invalid":
            low.append("规则「%s」判定无效。" % name)
        if row["outcome"] == "insufficient":
            low.append("规则「%s」：证据没有覆盖它要检查的内容。" % name)
        if row["outcome"] == "deducted" and not row["evidence"]:
            low.append("规则「%s」扣分没有记录证据。" % name)
        if row["rejected_quote_count"]:
            low.append("规则「%s」有 %d 条依据引文与原文对不上，已丢弃。" % (name, row["rejected_quote_count"]))
        if row["judge_type"] != "semantic" or row["outcome"] == "skipped":
            continue
        selection = row["evidence_selection"]
        if selection is None:
            notes.append("规则「%s」是旧评分，没有记录选了多少证据。" % name)
        elif selection["selected_count"] == 0:
            low.append("规则「%s」没有看到任何证据。" % name)
        elif selection["selected_count"] < selection["requested_count"]:
            medium.append(
                "规则「%s」的证据受长度上限限制，少放了 %d 段。"
                % (name, selection["requested_count"] - selection["selected_count"])
            )
        if (
            row["has_basis"]
            and not row["basis_shared_with"]
            and row["outcome"] in {"not_triggered", "not_applicable"}
            and not row["basis_evidence"]
            and not row["evidence"]
        ):
            medium.append("规则「%s」没有引用原文说明为什么不扣分。" % name)
    level = "low" if low else "medium" if medium else "high"
    return {
        "level": level,
        "label": _LEVEL_LABELS[level],
        "reasons": low + medium,
        "notes": notes,
    }


def build_judgement_view(run, item, *, frozen, snapshots=None):
    """一个 Core 评分项的判定视图；旧评分路径（没有逐条规则结果）返回 ``None``。"""

    if frozen.kind != "core":
        return None
    results = [entry for entry in (item.rule_results or ()) if isinstance(entry, dict)]
    if not results:
        return None
    snapshots = rule_snapshots(run) if snapshots is None else snapshots
    units = frozen.units()
    insufficient = {
        entry.get("rule_code")
        for entry in (item.review_reasons or ())
        if isinstance(entry, dict) and entry.get("code") == "evidence_insufficient"
    }
    rows = [
        _row(
            result,
            snapshots.get(result.get("rule_code")) or {},
            insufficient=result.get("rule_code") in insufficient,
            frozen=frozen,
            units=units,
        )
        for result in results
    ]
    _share_group_basis(rows)
    return {
        "schema_version": SCHEMA_VERSION,
        "rules": rows,
        "evidence_sufficiency": _sufficiency(rows),
    }


__all__ = ["SCHEMA_VERSION", "build_judgement_view", "rule_snapshots"]
