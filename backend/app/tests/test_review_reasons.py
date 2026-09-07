"""结构化「需要确认的原因」（前端 v2 计划 §5-B）。

本轮**只做确定性来源**：validator 强制覆盖 `need_manual_review` 的两条分支，
以及低置信度阈值。模型补充原因需要 bump `PROMPT_VERSION` 并按 §15 用真实留出集
重锚 QWK，而可门禁基线尚未建立（§11 未决事项），因此拆为后续增量。

结构化在先、展示文本在后：`review_reasons[]` 带 source/code/message，
`review_reason` 只是派生的展示投影。这样将来接入模型原因时不必改数据形状。
"""

from backend.app.services.scoring import review_reasons


def _criterion():
    class C:
        max_score = 25
        code = "C01"
        name = "研究方法"

    return C()


def test_low_confidence_produces_a_deterministic_reason():
    reasons = review_reasons.derive(
        {"confidence": 0.42, "need_manual_review": True, "evidence_sufficient": True},
        criterion=_criterion(),
        notes=[],
    )

    codes = [r["code"] for r in reasons]
    assert "low_confidence" in codes
    entry = next(r for r in reasons if r["code"] == "low_confidence")
    assert entry["source"] == "deterministic"
    assert "0.42" in entry["message"] or "42" in entry["message"]


def test_confidence_above_threshold_does_not_produce_that_reason():
    reasons = review_reasons.derive(
        {"confidence": 0.93, "need_manual_review": False, "evidence_sufficient": True},
        criterion=_criterion(),
        notes=[],
    )

    assert [r["code"] for r in reasons] == []


def test_missing_confidence_is_reported_as_unknown_not_as_zero(client):
    """Core 持久化时 confidence 为 None。它不等于「置信度为 0」。"""
    reasons = review_reasons.derive(
        {"confidence": None, "need_manual_review": True, "evidence_sufficient": True},
        criterion=_criterion(),
        notes=[],
    )

    codes = [r["code"] for r in reasons]
    assert "confidence_unavailable" in codes
    assert "low_confidence" not in codes


def test_insufficient_evidence_produces_its_own_reason():
    reasons = review_reasons.derive(
        {"confidence": 0.9, "need_manual_review": True, "evidence_sufficient": False},
        criterion=_criterion(),
        notes=[],
    )

    assert "evidence_insufficient" in [r["code"] for r in reasons]


def test_validator_notes_are_carried_through_verbatim():
    """证据校验失败与提示注入发生在模型返回之后，模型无从知晓。

    这两条确定性文案必须进入原因列表，否则原因栏会显示模型的「我很确定」，
    与实际的 need_manual_review 标记矛盾。
    """
    notes = [
        "部分证据引用未通过原文校验。",
        "检测到证据文本疑似提示注入，已标记人工复核；未采纳其中任何指令。",
    ]

    reasons = review_reasons.derive(
        {"confidence": 0.95, "need_manual_review": True, "evidence_sufficient": True},
        criterion=_criterion(),
        notes=notes,
    )

    messages = [r["message"] for r in reasons]
    for note in notes:
        assert note in messages
    assert all(r["source"] == "deterministic" for r in reasons)


def test_blocking_reasons_sort_before_advisory_ones():
    """阻断性原因排在前面，复核者先看到最要紧的那条。"""
    reasons = review_reasons.derive(
        {"confidence": 0.4, "need_manual_review": True, "evidence_sufficient": False},
        criterion=_criterion(),
        notes=["检测到证据文本疑似提示注入，已标记人工复核；未采纳其中任何指令。"],
    )

    codes = [r["code"] for r in reasons]
    assert codes.index("prompt_injection_suspected") < codes.index("low_confidence")


def test_display_text_is_derived_not_a_separate_source_of_truth():
    reasons = review_reasons.derive(
        {"confidence": 0.4, "need_manual_review": True, "evidence_sufficient": False},
        criterion=_criterion(),
        notes=[],
    )

    text = review_reasons.to_display_text(reasons)

    for entry in reasons:
        assert entry["message"] in text


def test_no_reasons_yields_empty_display_text():
    assert review_reasons.to_display_text([]) == ""


def test_reason_entries_have_a_closed_shape():
    """形状固定，将来接入模型原因时不必改数据结构。"""
    reasons = review_reasons.derive(
        {"confidence": 0.4, "need_manual_review": True, "evidence_sufficient": True},
        criterion=_criterion(),
        notes=[],
    )

    for entry in reasons:
        assert set(entry) == {"source", "code", "message", "rule_code"}
        assert entry["source"] in ("deterministic", "model", "core_issue", "provider")
