import re


# 论文正文/证据文本属于不可信数据；命中以下特征视为疑似提示注入，标记人工复核（绝不据其改分）。
INJECTION_PATTERNS = [
    re.compile(r"忽略(以上|上述|前面|之前|前述).{0,8}(指令|提示|要求|规则|内容)"),
    re.compile(r"(请|务必|必须|麻烦|帮我).{0,6}(给|打|评).{0,4}(满分|最高分|高分)"),
    re.compile(r"(直接)?(给|打).{0,2}(满分|最高分)"),
    re.compile(r"(忽略|无视).{0,6}(评分|扣分)(标准|规则)"),
    re.compile(r"ignore\s+(the\s+)?(previous|above|prior|all).{0,24}instruction", re.IGNORECASE),
    re.compile(r"(full|maximum|perfect)\s+(marks?|score)", re.IGNORECASE),
    re.compile(r"system\s+prompt|you\s+are\s+now", re.IGNORECASE),
]


def detect_injection(text):
    if not text:
        return False
    return any(pattern.search(str(text)) for pattern in INJECTION_PATTERNS)


REQUIRED_SCORE_KEYS = {
    "criterion_id",
    "criterion_name",
    "max_score",
    "score",
    "evidence_sufficient",
    "reason",
    "deductions",
    "evidence",
    "suggestion",
    "confidence",
    "need_manual_review",
}


def validate_score_output(output, criterion, evidence_candidates):
    missing = REQUIRED_SCORE_KEYS - set(output.keys())
    if missing:
        raise ValueError("score output missing keys: %s" % ", ".join(sorted(missing)))

    output["deductions"] = coerce_string_list(output.get("deductions"))
    output["deduction_items"] = coerce_deduction_items(
        output.get("deduction_items"), fallback_strings=output["deductions"]
    )
    output["evidence"] = coerce_evidence_list(output.get("evidence"))
    output["reason"] = str(output.get("reason") or "")
    output["suggestion"] = str(output.get("suggestion") or "")

    score = float(output["score"])
    max_score = float(criterion.max_score)
    if score < 0 or score > max_score:
        raise ValueError("score %.2f out of range 0..%.2f" % (score, max_score))
    confidence = float(output["confidence"])
    if confidence < 0 or confidence > 1:
        raise ValueError("confidence must be between 0 and 1")

    candidate_by_id = {item["chunk_id"]: item for item in evidence_candidates}
    validated_evidence = []
    evidence_failed = False
    for evidence in output.get("evidence", []):
        chunk_id = evidence.get("chunk_id")
        quote = evidence.get("quote") or ""
        candidate = candidate_by_id.get(chunk_id)
        if not candidate:
            evidence_failed = True
            continue
        if quote and _compact(quote) not in _compact(candidate.get("text", "")):
            evidence_failed = True
        validated_evidence.append(evidence)

    output["evidence"] = validated_evidence
    notes = []
    if evidence_failed:
        output["need_manual_review"] = True
        notes.append("部分证据引用未通过原文校验。")
    if any(detect_injection(item.get("text", "")) for item in evidence_candidates):
        output["need_manual_review"] = True
        output["injection_flagged"] = True
        notes.append("检测到证据文本疑似提示注入，已标记人工复核；未采纳其中任何指令。")
    for note in notes:
        output["deduction_items"].append(_blank_deduction(note))
    # deductions（list[str]）始终是 deduction_items 的展示投影，保持二者一致。
    output["deductions"] = [_format_deduction(item) for item in output["deduction_items"]]
    return output


def coerce_string_list(value):
    if value is None or value is False:
        return []
    if isinstance(value, (int, float)):
        return [] if float(value) == 0 else [str(value)]
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        result = []
        for item in value:
            if item is None or item is False:
                continue
            if isinstance(item, dict):
                text = item.get("reason") or item.get("text") or item.get("deduction") or json_safe(item)
            else:
                text = str(item)
            text = text.strip()
            if text:
                result.append(text)
        return result
    return [json_safe(value)]


def _blank_deduction(reason, points=None, rule_ref=None):
    return {
        "points": points,
        "reason": str(reason or "").strip(),
        "rule_ref": str(rule_ref) if rule_ref else None,
        "evidence_location": "",
        "evidence_quote": "",
    }


def _coerce_points(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def coerce_deduction_items(value, fallback_strings=None):
    """归一化为结构化扣分项 [{points, reason, rule_ref, evidence_location, evidence_quote}]。
    模型若只给字符串 deductions（如 mock/历史输出），则按 fallback_strings 兜底，points 置空。"""
    items = []
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict):
                reason = str(entry.get("reason") or entry.get("text") or entry.get("deduction") or "").strip()
                points = _coerce_points(entry.get("points"))
                if not reason and points is None:
                    continue
                items.append(
                    {
                        "points": points,
                        "reason": reason,
                        "rule_ref": str(entry.get("rule_ref")) if entry.get("rule_ref") else None,
                        "evidence_location": str(entry.get("evidence_location") or ""),
                        "evidence_quote": str(entry.get("evidence_quote") or ""),
                    }
                )
            elif isinstance(entry, str) and entry.strip():
                items.append(_blank_deduction(entry))
    if not items:
        for text in fallback_strings or []:
            if str(text).strip():
                items.append(_blank_deduction(text))
    return items


def _format_points(points):
    if points is None:
        return ""
    if float(points) == int(points):
        return str(int(points))
    return ("%.2f" % float(points)).rstrip("0").rstrip(".")


def _format_deduction(item):
    reason = str(item.get("reason") or "").strip()
    points = item.get("points")
    if points:
        suffix = "（-%s分）" % _format_points(points)
        return "%s%s" % (reason, suffix) if reason else "扣%s分" % _format_points(points)
    return reason


def coerce_evidence_list(value):
    if value is None or value is False:
        return []
    items = value if isinstance(value, list) else [value]
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "quote": str(item.get("quote") or item.get("text") or ""),
                "location": str(item.get("location") or ""),
                "chunk_id": item.get("chunk_id"),
            }
        )
    return result


def json_safe(value):
    return str(value)


def _compact(text):
    return " ".join(str(text).split())
