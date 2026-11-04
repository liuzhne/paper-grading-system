"""判定范围：一条语义规则该看论文的哪一部分（docs/论文评分第二阶段改造方案.md 第 5 节）。

只从规则快照推断（``applies_to`` 与规则文字），不调模型，结果可复现；推断规则的
版本号 ``JUDGE_SCOPE_VERSION`` 进入证据选择身份。范围只决定证据怎么取，不出分。
"""

from __future__ import annotations

import re


JUDGE_SCOPE_VERSION = "judge-scope@1"

SECTION = "section"
CROSS_SECTION = "cross_section"
DOCUMENT = "document"

_GLOBAL_MARKERS = ("global", "全文", "全篇", "整篇", "通篇", "全局", "整体", "全部", "各章", "所有章节")
_SPLIT_RE = re.compile(r"[、，,;；/]+")
_CROSS_MARKERS = ("一致", "对应", "呼应", "回应", "贯穿", "闭环", "前后")
_NUMBERING_RE = re.compile(
    r"^\s*(?:第\s*[0-9一二三四五六七八九十百零〇]+\s*[章节部分篇]|[0-9]+(?:[.．][0-9]+)*)[、.．:：\s]*"
)
# (角色, 匹配论文标题的词, 从规则文字里认角色的词)。规则文字里的“研究问题”指绪论，
# 但论文标题里很少这样写，所以两组词分开；“实现”“需求”单字面太宽，只认完整说法。
_ROLES = (
    ("摘要", ("摘要", "abstract"), ("摘要",)),
    (
        "绪论",
        ("绪论", "引言", "前言", "研究背景", "introduction"),
        ("绪论", "引言", "前言", "研究背景", "研究问题", "研究目标", "研究目的"),
    ),
    (
        "文献综述",
        ("文献综述", "相关工作", "研究现状", "国内外研究"),
        ("文献综述", "相关工作", "研究现状"),
    ),
    ("需求", ("需求分析", "需求"), ("需求分析",)),
    (
        "设计",
        ("系统设计", "总体设计", "详细设计", "方案设计"),
        ("系统设计", "总体设计", "详细设计", "方案设计"),
    ),
    ("实现", ("系统实现", "实现"), ("系统实现",)),
    (
        "测试",
        ("系统测试", "测试", "实验", "结果与分析", "结果分析"),
        ("系统测试", "实验结果", "测试结果"),
    ),
    (
        "结论",
        ("结论", "总结", "结语", "结束语", "conclusion"),
        ("结论", "总结", "结语", "结束语"),
    ),
)


def _core(text) -> str:
    return _NUMBERING_RE.sub("", str(text or "")).strip().casefold()


def _unique(values):
    seen = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def _role_headings(label: str):
    core = _core(label)
    for role, headings, terms in _ROLES:
        if core == role or any(item in core for item in (*headings, *terms)):
            return headings
    return ()


def _roles_in(wording: str) -> list[str]:
    found = []
    for role, _headings, terms in _ROLES:
        positions = [wording.find(term) for term in terms if term in wording]
        if positions:
            found.append((min(positions), role))
    return [role for _position, role in sorted(found)]


def _applies_targets(rule) -> list[str]:
    applies = str(rule.get("applies_to") or "").strip()
    if not applies or any(marker in applies.casefold() for marker in _GLOBAL_MARKERS):
        return []
    return _unique(part.strip() for part in _SPLIT_RE.split(applies) if part.strip())


def _wording(rule) -> str:
    return " ".join(str(rule.get(field) or "") for field in ("name", "rule_text"))


def explain_judge_scope(rule) -> dict:
    """推断结果加来源：``source`` 是 deterministic / explicit / applies_to / wording / default。

    人工指定的 ``evidence_policy.judge_scope`` 优先；章节名只来自 ``applies_to``
    或规则文字，指定了范围却一个章节也找不到时按全文处理。
    """

    if rule.get("judge_type") == "deterministic":
        return {"scope": DOCUMENT, "targets": [], "source": "deterministic"}
    explicit = (rule.get("evidence_policy") or {}).get("judge_scope")
    applies = _applies_targets(rule)
    wording = _wording(rule)
    if explicit == DOCUMENT:
        return {"scope": DOCUMENT, "targets": [], "source": "explicit"}
    if explicit in {SECTION, CROSS_SECTION}:
        targets = applies or _roles_in(wording)
        if targets:
            return {"scope": explicit, "targets": targets, "source": "explicit"}
        return {"scope": DOCUMENT, "targets": [], "source": "explicit"}
    if len(applies) == 1:
        return {"scope": SECTION, "targets": applies, "source": "applies_to"}
    if len(applies) > 1:
        return {"scope": CROSS_SECTION, "targets": applies, "source": "applies_to"}
    if any(marker in wording for marker in _CROSS_MARKERS):
        roles = _roles_in(wording)
        if len(roles) > 1:
            return {"scope": CROSS_SECTION, "targets": roles, "source": "wording"}
    return {"scope": DOCUMENT, "targets": [], "source": "default"}


def infer_judge_scope(rule) -> dict:
    """``{"scope": ..., "targets": [...]}``；targets 是章节名或角色名。"""

    explained = explain_judge_scope(rule)
    return {"scope": explained["scope"], "targets": explained["targets"]}


_ABSENCE_WORDS = ("缺失", "缺少", "不存在", "未包含", "没有")


def review_hint(rule) -> str | None:
    """规则审查页的提示：问“某一章有没有”的语义规则，更适合交给结构检查器。"""

    if rule.get("judge_type") != "semantic":
        return None
    if explain_judge_scope(rule)["scope"] != DOCUMENT:
        return None
    wording = _wording(rule)
    if any(word in wording for word in _ABSENCE_WORDS) and any(
        term in wording for _role, headings, terms in _ROLES for term in (*headings, *terms)
    ):
        return "这条规则问的是某一章有没有，交给结构检查器（必备章节）更可靠；语义判断只能看到部分段落。"
    return None


def heading_matches(target: str, heading: str) -> bool:
    target_core = _core(target)
    heading_core = _core(heading)
    if not target_core or not heading_core:
        return False
    if target_core in heading_core or heading_core in target_core:
        return True
    return any(alias in heading_core for alias in _role_headings(target))


def unit_target(unit, targets) -> str | None:
    """The first target whose name matches any heading of the unit's section path."""

    for target in targets:
        if any(heading_matches(target, heading) for heading in unit.get("section_path") or ()):
            return target
    return None


def select_scoped_units(units, scope, *, rank, top_k):
    """Rank inside each target's sections; fall back to the whole paper when none match.

    ``rank`` orders a list of units (BM25 + section diversity).  Cross-section
    targets are interleaved so every found target is represented first.
    """

    targets = scope["targets"]
    pools = []
    for target in targets:
        pool = [unit for unit in units if unit_target(unit, [target]) == target]
        if pool:
            pools.append(rank(pool))
    if not pools:
        return rank(list(units))[:top_k]
    selected = []
    seen = set()
    depth = 0
    while len(selected) < top_k:
        added = False
        for ranked in pools:
            if depth < len(ranked):
                unit = ranked[depth]
                if unit["evidence_unit_id"] not in seen:
                    seen.add(unit["evidence_unit_id"])
                    selected.append(unit)
                    if len(selected) >= top_k:
                        break
                added = True
        if not added:
            break
        depth += 1
    return selected


def scope_view(rules, coverage_mode: str, units) -> dict | None:
    """What the provider is told about the scope, derived from the frozen envelope.

    Uses the lowest rule code like evidence selection does, so every member of
    a mutex group is told the same scope.
    """

    if coverage_mode not in {SECTION, CROSS_SECTION} or not rules:
        return None
    rule = min(rules, key=lambda item: item["rule_code"])
    targets = infer_judge_scope(rule)["targets"]
    if not targets:
        return None
    counts = {target: 0 for target in targets}
    for unit in units:
        target = unit_target(unit, targets)
        if target is not None:
            counts[target] += 1
    return {
        "type": coverage_mode,
        "targets": [{"name": target, "evidence": counts[target]} for target in targets],
    }


__all__ = [
    "CROSS_SECTION",
    "DOCUMENT",
    "JUDGE_SCOPE_VERSION",
    "SECTION",
    "explain_judge_scope",
    "heading_matches",
    "infer_judge_scope",
    "review_hint",
    "scope_view",
    "select_scoped_units",
    "unit_target",
]
