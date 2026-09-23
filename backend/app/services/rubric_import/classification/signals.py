"""规则信号预筛（解析重构方案 §4.4 / §5.2）。

通用信号写在代码里，领域词汇按 business_profile_key 配置。预筛有意放宽，
宁可多送给人或 LLM 判断，也不漏掉疑似规则。
"""

from __future__ import annotations

import re

_SCORE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:分|%|％|points?\b|pts?\b)|(?:扣|减|得|加)\s*\d", re.IGNORECASE
)
_VERB_RE = re.compile(r"扣|减分|加分|不得分|得分|酌情")
_NORMATIVE_RE = re.compile(r"须|必须|应当|不得(?!分)|禁止|至少|不少于|不超过|不低于|不高于|需要|需")
_NOISE_RE = re.compile(r"^[\s\W_]*$|^[\s(（\[【-]*\d+[\s)）\]】.、-]*$|^第\s*\d+\s*页$|^-\s*\d+\s*-$")

PROFILE_SIGNAL_TERMS: dict[str, tuple[str, ...]] = {
    "thesis": ("题注", "参考文献", "查重", "引用", "摘要", "关键词", "图表", "字数"),
    "technical_proposal": ("技术路线", "可行性", "进度", "预算", "风险", "交付"),
}


def profile_signal_terms(business_profile_key: str | None) -> tuple[str, ...]:
    return PROFILE_SIGNAL_TERMS.get(str(business_profile_key or ""), ())


def rule_signals(text: str | None, *, profile_terms: tuple[str, ...] | list[str] = ()) -> tuple[str, ...]:
    """返回命中的信号类别（score / verb / normative / profile），按固定顺序。"""

    value = str(text or "")
    if not value.strip():
        return ()
    hits = []
    if _SCORE_RE.search(value):
        hits.append("score")
    if _VERB_RE.search(value):
        hits.append("verb")
    if _NORMATIVE_RE.search(value):
        hits.append("normative")
    if any(term and term in value for term in profile_terms):
        hits.append("profile")
    return tuple(hits)


def is_noise(text: str | None) -> bool:
    """空白、纯标点、纯序号、页码：可直接标为 structural。"""

    return bool(_NOISE_RE.match(str(text or "").strip()))
