"""findings → 自动扣分（设计§6.3 / §9）：把 coherence/format 发现按**评分项维度**领取，
再用该评分项**编译好的结构化扣分规则**把发现转成带 rule_ref+证据的结构化扣分。

原则：
- 只有 warning 级发现可被扣分；info（unknown/未引用/著者-年制/语义跳过）一律不判错（设计：unknown 不判错、人在回路）。
- 扣几分**来自规则**（rule.points），不写死；发现匹配不到任何规则则不扣（仍进报告供人工）。
- 仅"显式启用"（deterministic + 有 dimension + 有结构化规则）的评分项才走这里（见 engine）。
"""

from backend.app.services.scoring.validator import _format_deduction

# finding 类别 ↔ 维度关键词（文档化默认约定；评分项 dimension 含对应关键词即领取该类发现）。
CATEGORY_KEYWORDS = {
    "format": ["格式"],
    "reference": ["规范", "引文", "参考文献", "引用"],
    "semantic": ["逻辑", "论证", "一致", "连贯"],
}


def _category(kind):
    kind = kind or ""
    if kind.startswith("format_"):
        return "format"
    if kind.startswith(("figure_", "citation_", "reference_")):
        return "reference"
    if kind in ("research_question_unanswered", "conclusion_claim_unsupported", "coherence_semantic_skipped"):
        return "semantic"
    return "other"


def findings_for_dimension(dimension, findings):
    text = str(dimension or "")
    matched = []
    for finding in findings or []:
        keywords = CATEGORY_KEYWORDS.get(_category(finding.get("kind")), [])
        if any(keyword in text for keyword in keywords):
            matched.append(finding)
    return matched


def _match_rule(finding, rules):
    haystack = "%s %s" % (finding.get("message") or "", finding.get("kind") or "")
    for rule in rules or []:
        match = str(rule.get("match") or "").strip()
        if match and match in haystack:
            return rule
    return None


def score_from_findings(criterion, all_findings, rules):
    """显式启用的评分项：按维度领取 warning 发现 + 命中规则扣分。返回与其它执行器一致的输出。"""
    max_score = float(getattr(criterion, "max_score", None) or 0)
    relevant = [f for f in findings_for_dimension(getattr(criterion, "dimension", None), all_findings) if f.get("severity") == "warning"]
    code = getattr(criterion, "code", None)

    deduction_items = []
    evidence = []
    for finding in relevant:
        rule = _match_rule(finding, rules)
        if rule is None:
            continue  # 维度相关但无规则匹配 → 不自动扣，留报告
        points = float(rule.get("points") or 0)
        deduction_items.append(
            {
                "points": points,
                "reason": rule.get("reason") or finding.get("message") or "",
                "rule_ref": code,
                "evidence_location": finding.get("location") or "",
                "evidence_quote": finding.get("message") or "",
            }
        )
        evidence.append({"quote": finding.get("message") or "", "location": finding.get("location") or "", "chunk_id": None})
        finding["deducted_by"] = code  # 标记已转扣分（同一 dict 也在 run.coherence/format_findings 里）
        finding["deducted_points"] = points

    total = sum(item["points"] for item in deduction_items)
    awarded = round(max(0.0, min(max_score - total, max_score)), 2)
    overdrawn = total > max_score
    return {
        "criterion_id": criterion.id,
        "criterion_name": criterion.name,
        "max_score": max_score,
        "score": awarded,
        "evidence_sufficient": True,
        "reason": "%s 按规则将篇章/格式发现转为扣分：满分 %.2f，扣分合计 %.2f，得 %.2f/%.2f。"
        % (criterion.name, max_score, total, awarded, max_score),
        "deductions": [_format_deduction(item) for item in deduction_items],
        "deduction_items": deduction_items,
        "evidence": evidence,
        "suggestion": "确定性发现已按评分项规则计入；建议人工复核被扣项与未匹配规则的发现。",
        "confidence": 1.0,
        "need_manual_review": overdrawn,
        "checker_kind": "findings",
        "scoring_mode": "deductive",
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def is_findings_enabled(criterion):
    """显式启用判据：deterministic + 有维度 + 有结构化扣分规则（均来自用户授权的 rubric）。"""
    return bool(
        getattr(criterion, "criterion_type", "") == "deterministic"
        and getattr(criterion, "dimension", None)
        and (getattr(criterion, "deduction_rules_structured", None) or [])
    )
