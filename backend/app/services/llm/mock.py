from backend.app.services.llm.base import LLMScorer


class MockLLMScorer(LLMScorer):
    provider = "mock"
    model_name = "mock-criterion-scorer"
    model_version = "v1"

    def score_criterion(self, paper, criterion, evidence_candidates, structure_checks):
        max_score = float(criterion.max_score)
        evidence_text = "\n".join(item.get("text", "") for item in evidence_candidates)
        evidence_sufficient = len(evidence_candidates) > 0 and len(evidence_text) >= 40
        failed_checks = [check for check in structure_checks if not check.get("passed")]

        penalty_ratio = 0.08
        deductions = []
        if not evidence_sufficient:
            penalty_ratio += 0.35
            deductions.append("当前评分项可用原文证据不足。")
        else:
            if len(evidence_text) < 500:
                penalty_ratio += 0.08
                deductions.append("相关论述篇幅较短，支撑材料有限。")

        name = criterion.name
        if "参考文献" in name and any(check.get("code") == "HAS_REFERENCES" for check in failed_checks):
            penalty_ratio += 0.2
            deductions.append("未稳定检测到参考文献章节或参考文献内容。")
        if "写作" in name or "规范" in name:
            missing = [check.get("name") for check in failed_checks if check.get("code") != "WORD_COUNT_MIN"]
            if missing:
                penalty_ratio += min(0.2, 0.04 * len(missing))
                deductions.append("模板完整性检查存在缺项：%s。" % "、".join(missing[:5]))
        if "方法" in name and not _contains_any(evidence_text, ["方法", "实验", "数据", "模型", "问卷", "访谈"]):
            penalty_ratio += 0.14
            deductions.append("证据中对研究方法、数据来源或实验设计的描述不充分。")
        if "创新" in name and not _contains_any(evidence_text, ["创新", "贡献", "改进", "提出"]):
            penalty_ratio += 0.12
            deductions.append("证据中对创新点或贡献的表达不够明确。")

        penalty_ratio = min(penalty_ratio, 0.75)
        score = round(max_score * (1 - penalty_ratio), 2)
        confidence = 0.82 if evidence_sufficient else 0.48
        if deductions:
            confidence -= min(0.18, 0.03 * len(deductions))
        confidence = round(max(0.35, min(confidence, 0.92)), 3)

        if not deductions:
            deductions.append("未发现明显扣分点，评分基于召回证据保守给出。")

        evidence = []
        for candidate in evidence_candidates[:3]:
            quote = _quote(candidate.get("text", ""))
            if quote:
                evidence.append(
                    {
                        "quote": quote,
                        "location": candidate.get("location") or candidate.get("section_title") or "未知位置",
                        "chunk_id": candidate.get("chunk_id"),
                    }
                )

        result = {
            "criterion_id": criterion.id,
            "criterion_name": criterion.name,
            "max_score": max_score,
            "score": score,
            "evidence_sufficient": evidence_sufficient,
            "reason": _reason(criterion.name, score, max_score, evidence_sufficient),
            "deductions": deductions,
            "evidence": evidence,
            "suggestion": _suggestion(criterion.name, evidence_sufficient),
            "confidence": confidence,
            "need_manual_review": (not evidence_sufficient) or confidence < 0.65,
        }
        # 模式感知（与真实模型行为对齐，便于端到端测试）：
        mode = getattr(criterion, "scoring_mode", "llm_direct")
        if mode == "deductive":
            result["deduction_items"] = [
                {
                    "points": round(max_score - score, 2),
                    "reason": "；".join(deductions)[:120] or "综合扣分",
                    "rule_ref": getattr(criterion, "code", None),
                    "evidence_location": "",
                    "evidence_quote": "",
                }
            ]
        elif mode == "banded":
            bands = [b for b in (getattr(criterion, "rubric_levels", None) or []) if isinstance(b, dict) and b.get("points") is not None]
            if bands:
                chosen = min(bands, key=lambda b: abs(float(b["points"]) - score))
                result["band_selection"] = {
                    "level": chosen.get("label"),
                    "rationale": result["reason"],
                    "rule_ref": getattr(criterion, "code", None),
                    "evidence_location": "",
                    "evidence_quote": "",
                }
        return result


    def complete_json(self, instructions, payload):
        # Mock 无法做真正的语义核验：返回空核验结构（不产出语义一致性发现）。
        return {"research_questions": [], "conclusion_claims": []}


def _contains_any(text, keywords):
    return any(keyword in text for keyword in keywords)


def _quote(text):
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""
    return cleaned[:180]


def _reason(name, score, max_score, evidence_sufficient):
    if evidence_sufficient:
        return "%s评分基于召回证据完成，得分 %.2f/%.2f。" % (name, score, max_score)
    return "%s缺少足够证据，已按保守规则评分并建议人工复核。" % name


def _suggestion(name, evidence_sufficient):
    if not evidence_sufficient:
        return "补充与%s直接相关的章节内容或材料，便于教师复核。" % name
    return "建议围绕%s补充更明确的论证、数据或引用依据。" % name
