"""评分项的修改建议：分清“评分模型针对内容写的建议”和“系统写的固定说明”（评分助手 Agent 化方案 §31.3）。

`ScoreItem.suggestion` 里两种都有。旧评分路径让评分模型逐项写修改建议；但有几条路径会写系统说明
（评分失败或降级、混合制、分块汇总、确定性发现、缺可重放 observation），那不是“这份论文怎么改”。
评分页与评分助手只把前一种当作修改建议。生产上的 Core 评分不写修改建议（2026-10-26 核对）。
"""

from __future__ import annotations

#: 系统写进 suggestion 的固定说明（与 scoring/engine.py、checkers/findings_checker.py 里的文案对应）。
SYSTEM_SUGGESTION_MARKERS = (
    "请人工重点复核，并检查 LLM API",  # 真实模型调用失败、降级为 Mock：前面那段也是 Mock 写的
    "请补齐可重放 observation",
    "混合制：确定性子项由规则核验",
    "已按分块证据评分汇总",
    "确定性发现已按评分项规则计入",
)
#: 只有评出了结果的评分项才有内容上的修改建议；无效、阻断的项没有。
_SCORED = {None, "", "calculated"}


def revision_suggestion(suggestion, auto_score_status=None) -> str | None:
    """评分模型针对内容写的修改建议；系统说明、没评出结果的项返回 None。"""

    text = str(suggestion or "").strip()
    if not text or auto_score_status not in _SCORED:
        return None
    if any(marker in text for marker in SYSTEM_SUGGESTION_MARKERS):
        return None
    return text


__all__ = ["SYSTEM_SUGGESTION_MARKERS", "revision_suggestion"]
