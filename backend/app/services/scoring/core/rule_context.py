"""Deterministic context needs of a published rule (frozen into snapshot @3).

A semantic rule only receives the paper-wide context it plausibly needs:
the reference list, the coherence findings or the failed structure checks.
The decision is derived from the rule's own published wording so it is
reproducible and part of the rubric snapshot hash; the alternative of sending
every context to every rule cost ~28% of each request.
"""

from __future__ import annotations


_CONTEXT_MARKERS = {
    "references": ("文献", "参考", "引用", "检索", "信息来源", "资料来源", "reference", "citation"),
    "coherence": ("一致", "对应", "前后", "印证", "呼应", "矛盾", "consisten", "coheren"),
    "structure": ("章节", "结构", "缺失", "缺少", "完整", "目录", "摘要", "structure", "section"),
}


def derive_context_needs(*texts) -> list[str]:
    haystack = "\n".join(str(text) for text in texts if text).casefold()
    return sorted(
        need
        for need, markers in _CONTEXT_MARKERS.items()
        if any(marker.casefold() in haystack for marker in markers)
    )


__all__ = ["derive_context_needs"]
