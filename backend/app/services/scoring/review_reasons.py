"""结构化「需要确认的原因」（前端 v2 计划 §5-B）。

复核队列要回答的是「**为什么这条要我看**」。一个布尔 `need_manual_review`
answers 不了它——复核者拿到一堆待确认项却不知道每条的成因，只能逐个点开原文，
队列就失去了意义。

本模块只产出**确定性**原因。模型补充原因是另一条管线：它要改判分 prompt 的
输出契约、bump ``cache/llm_cache.PROMPT_VERSION``，并按设计 §15 用真实留出集
重锚 QWK。可门禁基线目前尚未建立（计划 §11 未决事项），因此拆为后续增量。
数据形状预留了 ``source`` 字段，接入时不必改结构。

排序上阻断性原因在前：提示注入、证据校验失败这类是「结论本身可能不成立」，
比「模型没把握」更要紧。
"""

from __future__ import annotations


#: 低于此置信度即提示人工确认。与 Mock/真实 provider 的阈值口径一致。
LOW_CONFIDENCE_THRESHOLD = 0.65

#: validator 在覆盖 need_manual_review 时产出的确定性文案 -> 结构化 code。
#: 这两件事发生在模型返回**之后**，模型无从知晓，因此必须由确定性管线补上。
_NOTE_CODES = {
    "部分证据引用未通过原文校验。": "evidence_verification_failed",
    "检测到证据文本疑似提示注入，已标记人工复核；未采纳其中任何指令。": (
        "prompt_injection_suspected"
    ),
}

#: 越小越靠前。阻断性原因排在建议性原因之前。
_PRIORITY = {
    "prompt_injection_suspected": 0,
    "evidence_verification_failed": 1,
    "evidence_insufficient": 2,
    "confidence_unavailable": 3,
    "low_confidence": 4,
    "reason_not_recorded": 90,
}


def _entry(code, message, *, source="deterministic", rule_code=None):
    return {
        "source": source,
        "code": code,
        "message": message,
        "rule_code": rule_code,
    }


def derive(output, *, criterion=None, notes=()):
    """从一次评分输出与 validator 备注推导原因列表。

    :param output: 已通过 validator 的评分输出映射。
    :param notes: validator 在强制覆盖 ``need_manual_review`` 时产生的文案。
    """
    reasons = []

    for note in notes or ():
        code = _NOTE_CODES.get(note)
        if code is None:
            # 未登记的文案仍要透出，只是无法归类——丢掉它会让原因栏空着。
            reasons.append(_entry("deterministic_note", note))
        else:
            reasons.append(_entry(code, note))

    if not output.get("evidence_sufficient", True):
        reasons.append(
            _entry("evidence_insufficient", "证据不足以支撑该项给分，需人工核对原文。")
        )

    confidence = output.get("confidence")
    if confidence is None:
        # Core 持久化当前不写 confidence。缺失不等于 0，更不等于满信心。
        if output.get("need_manual_review"):
            reasons.append(
                _entry(
                    "confidence_unavailable",
                    "该评分路径未提供置信度，无法据此判断把握程度。",
                )
            )
    elif float(confidence) < LOW_CONFIDENCE_THRESHOLD:
        reasons.append(
            _entry(
                "low_confidence",
                "模型置信度 %.2f 低于阈值 %.2f。"
                % (float(confidence), LOW_CONFIDENCE_THRESHOLD),
            )
        )

    reasons.sort(key=lambda entry: _PRIORITY.get(entry["code"], 99))
    return reasons


def recover_for_legacy_item(item):
    """历史评分项的原因显示回退（前端 v2 计划 §5-B）。

    `review_reasons` 是 0025 才加的列。在它之前完成的评分这一列为空，队列会
    显示一行标着「需要确认」却不给任何原因。这里只用**评分项上已有的数据**
    推导，不重新调用模型、不改写历史行——§5-B 明写「不为补文案重评历史」。

    推不出来时返回 ``reason_not_recorded``，而不是挑一条看起来合理的凑上去：
    编一个原因比不给原因更糟，复核者会照着那个不存在的线索去核对原文。

    返回空列表表示该项本就不需要复核。
    """
    if not getattr(item, "need_manual_review", False):
        return []

    # 旧管线把 validator 的确定性文案追加进了 deduction_items，那就是 §5-B 说的
    # 「已有 issue 数据」。按原文匹配，匹配不上的不猜。
    notes = []
    for entry in getattr(item, "deduction_items", None) or ():
        text = entry.get("reason") if isinstance(entry, dict) else None
        if text in _NOTE_CODES:
            notes.append(text)

    reasons = derive(
        {
            "evidence_sufficient": bool(
                getattr(item, "evidence_sufficient", True)
            ),
            "confidence": getattr(item, "confidence", None),
            "need_manual_review": True,
        },
        notes=notes,
    )
    if reasons:
        return reasons

    return [
        _entry(
            "reason_not_recorded",
            "该项在系统开始记录复核原因之前完成评分，具体原因未记录。",
        )
    ]


def to_display_text(reasons):
    """派生的展示文本。

    它是 ``review_reasons`` 的投影，不是另一个事实源——两者不一致时以结构化
    列表为准。
    """
    return " ".join(entry["message"] for entry in reasons or ())


__all__ = ["derive", "to_display_text", "LOW_CONFIDENCE_THRESHOLD"]
