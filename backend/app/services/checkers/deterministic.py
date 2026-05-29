"""确定性检查器（设计哲学#1 / §9 / 阶段2.1）。

把"确定性"评分项（criterion_type == 'deterministic'）交给代码逐项核验，**不调用大模型**：
结构完整性、字数、图表引用、引文-参考文献（编号制确定性核对）。

每个检查器返回与 LLM 评分一致的结构化结果（含带 points 的 deduction_items、证据、confidence=1），
得分天然为扣分制：awarded = max - Σpoints。格式检查器（字体/字号/行距，需解析 styles.xml/theme1.xml）
本轮先留 stub，见 _format_check。
"""

import re

from backend.app.services.scoring.validator import _format_deduction


COMPLETENESS_CODES = {
    "HAS_ABSTRACT_CN",
    "HAS_ABSTRACT_EN",
    "HAS_KEYWORDS",
    "HAS_TOC",
    "HAS_CONCLUSION",
    "HAS_REFERENCES",
    "SECTION_COUNT_MIN",
}

WORD_COUNT_MIN_DEFAULT = 3000

# 著者-出版年制引用特征：（张三, 2020） / (Smith, 2019) —— 命中则编号制确定性核对不适用，转人工/混合。
AUTHOR_YEAR_RE = re.compile(r"[（(][^）)]{1,40}[，,]\s*(19|20)\d{2}[）)]")
INTEXT_NUM_RE = re.compile(r"\[(\d{1,3})\]")
FIGURE_REF_RE = re.compile(r"(?:图|表)\s*(\d{1,3})")


def run_deterministic_checker(criterion, parsed):
    """按评分项名称分派到具体确定性检查器。"""
    name = getattr(criterion, "name", "") or ""
    if any(token in name for token in ["参考文献", "引文", "引用"]):
        return _citation_check(criterion, parsed)
    if any(token in name for token in ["字数", "篇幅", "字符"]):
        return _word_count_check(criterion, parsed)
    if any(token in name for token in ["图表", "图", "表"]):
        return _figure_check(criterion, parsed)
    return _structure_completeness_check(criterion, parsed)


# --------------------------------------------------------------------------- #
# 具体检查器
# --------------------------------------------------------------------------- #
def _structure_completeness_check(criterion, parsed):
    max_score = float(criterion.max_score)
    checks = [c for c in parsed.get("structure_checks", []) if c.get("code") in COMPLETENESS_CODES]
    if not checks:
        return _output(criterion, max_score, [], [], "%s：未配置可核验的结构项，按满分给出。" % criterion.name)

    per_fail = round(max_score / len(checks), 4)
    deduction_items = []
    evidence = []
    for check in checks:
        if check.get("passed"):
            evidence.append(_evidence(check.get("message") or check.get("name"), check.get("location")))
        else:
            deduction_items.append(
                _ded(per_fail, "缺少%s" % (check.get("name") or check.get("code")), rule_ref=check.get("code"))
            )
    awarded = max_score - sum(item["points"] for item in deduction_items)
    reason = "%s 按结构完整性确定性核验：%d 项中通过 %d 项。" % (
        criterion.name,
        len(checks),
        len(checks) - len(deduction_items),
    )
    return _output(criterion, awarded, deduction_items, evidence, reason, checker="structure")


def _word_count_check(criterion, parsed):
    max_score = float(criterion.max_score)
    length = len(parsed.get("full_text") or "")
    minimum = _word_count_minimum(parsed)
    if length >= minimum:
        return _output(
            criterion,
            max_score,
            [],
            [_evidence("正文约 %d 字，达到下限 %d" % (length, minimum), "全文")],
            "%s：正文约 %d 字，达标。" % (criterion.name, length),
            checker="word_count",
        )
    ratio = length / minimum if minimum else 0
    awarded = round(max_score * ratio, 2)
    deduction_items = [_ded(round(max_score - awarded, 2), "正文约 %d 字，低于下限 %d" % (length, minimum), rule_ref="WORD_COUNT_MIN")]
    return _output(
        criterion,
        awarded,
        deduction_items,
        [_evidence("正文约 %d 字" % length, "全文")],
        "%s：正文约 %d 字，低于下限 %d，按比例扣分。" % (criterion.name, length, minimum),
        checker="word_count",
    )


def _figure_check(criterion, parsed):
    max_score = float(criterion.max_score)
    full_text = parsed.get("full_text") or ""
    refs = sorted({int(n) for n in FIGURE_REF_RE.findall(full_text)})
    if refs:
        return _output(
            criterion,
            max_score,
            [],
            [_evidence("检测到图/表引用编号：%s" % "、".join(str(n) for n in refs[:10]), "正文")],
            "%s：检测到 %d 处图/表引用编号。" % (criterion.name, len(refs)),
            checker="figure",
        )
    deduction_items = [_ded(round(max_score * 0.5, 2), "正文未检测到任何图/表引用（如“见图1/表1”）", rule_ref="FIGURE_REF")]
    return _output(
        criterion,
        max_score - deduction_items[0]["points"],
        deduction_items,
        [],
        "%s：正文未检测到图/表引用编号，按确定性规则扣分。" % criterion.name,
        need_review=True,
        checker="figure",
    )


def _citation_check(criterion, parsed):
    max_score = float(criterion.max_score)
    full_text = parsed.get("full_text") or ""
    references = parsed.get("references") or []
    ref_count = len(references)
    intext = sorted({int(n) for n in INTEXT_NUM_RE.findall(full_text)})

    # 著者-出版年制：编号制确定性核对不适用 → 标记人工/混合复核。
    if AUTHOR_YEAR_RE.search(full_text) and len(intext) <= 1:
        return _output(
            criterion,
            max_score,
            [],
            [_evidence("检测到著者-出版年制引用", "正文")],
            "%s：检测到著者-出版年制引用，编号制确定性核对不适用，转人工/混合核验。" % criterion.name,
            need_review=True,
            checker="citation_author_year",
        )

    deduction_items = []
    if ref_count == 0 and "参考文献" not in full_text:
        deduction_items.append(_ded(round(max_score * 0.8, 2), "未检测到参考文献条目", rule_ref="HAS_REFERENCES"))
    else:
        missing = [n for n in intext if n > ref_count] if ref_count else list(intext)
        if missing:
            per = round(min(max_score * 0.15, max_score / max(len(intext), 1)), 2)
            for number in missing[:8]:
                deduction_items.append(
                    _ded(per, "文中引用 [%d] 在参考文献中无对应条目（共 %d 条）" % (number, ref_count), rule_ref="CITATION_MISSING")
                )
    awarded = max_score - sum(item["points"] for item in deduction_items)
    evidence = [_evidence("文中编号引用 %d 处，参考文献 %d 条" % (len(intext), ref_count), "参考文献")]
    reason = "%s：编号制引文-参考文献双向核对，发现 %d 处问题。" % (criterion.name, len(deduction_items))
    return _output(criterion, awarded, deduction_items, evidence, reason, checker="citation")


def _format_check(criterion, parsed):  # pragma: no cover - 阶段2后续实现
    """格式检查（字体/字号/行距/页边距）：需解析 styles.xml + theme1.xml 算有效值并设 unknown 第三态。
    本轮先不实现，留待阶段2后续；调用方不应把格式类项标 deterministic 直到此函数完成。"""
    raise NotImplementedError("format checker (styles.xml/theme1.xml) not implemented yet")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _word_count_minimum(parsed):
    # 阈值目前固定（与 document_parser 的 WORD_COUNT_MIN 一致）；后续可改为按评分项配置。
    return WORD_COUNT_MIN_DEFAULT


def _ded(points, reason, rule_ref=None, location="", quote=""):
    return {
        "points": round(float(points), 2),
        "reason": reason,
        "rule_ref": rule_ref,
        "evidence_location": location,
        "evidence_quote": quote,
    }


def _evidence(quote, location):
    return {"quote": str(quote or ""), "location": str(location or ""), "chunk_id": None}


def _output(criterion, awarded, deduction_items, evidence, reason, *, need_review=False, suggestion="", checker="structure"):
    max_score = float(criterion.max_score)
    awarded = round(max(0.0, min(float(awarded), max_score)), 2)
    return {
        "criterion_id": criterion.id,
        "criterion_name": criterion.name,
        "max_score": max_score,
        "score": awarded,
        "evidence_sufficient": True,
        "reason": reason,
        "deductions": [_format_deduction(item) for item in deduction_items],
        "deduction_items": deduction_items,
        "evidence": evidence,
        "suggestion": suggestion,
        "confidence": 1.0,
        "need_manual_review": need_review,
        "checker_kind": checker,
        "scoring_mode": "deterministic",
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
