"""评分项的修改建议：分清“评分模型针对内容写的建议”和“系统写的固定说明”（评分助手 Agent 化方案 §31.3）。

`ScoreItem.suggestion` 里两种都有。旧评分路径让评分模型逐项写修改建议；但有几条路径会写系统说明
（评分失败或降级、混合制、分块汇总、确定性发现、缺可重放 observation），那不是“这份论文怎么改”。
评分页与评分助手只把前一种当作修改建议。生产上的 Core 评分不写修改建议（2026-10-26 核对）。
"""

from __future__ import annotations

import re

from backend.app.services.scoring.core.evidence import detect_injection

#: 分数样式：“2 分”“3.5 points”“满分”“总分 78”“得了 80”“得分 6”“得分为 8”“分数是 7”。“2 分钟”不算。
#: 评分助手的回复校验也用这一份（agent/reply.py），两处对“带分数”的判断一致。
SCORE_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:分(?!钟)|points?\b|pts?\b)|满分|(?:总分|得分|分数)(?:是|为)?\s*\d|得了\s*\d",
    re.IGNORECASE,
)
_NUM = r"\s*\d+(?:\.\d+)?"
#: 改分的说法（数字后面不一定带“分”）：“改为 8 分”“建议给 3 分”“加 2 分”“得分调到 7”“将总分改为 8”。
#: “建议把引用数量改为 8 条”是修改建议，不算。评分助手的回复校验也用这一份（#60 复审）。
SCORE_CHANGE_PATTERN = re.compile(
    r"(?:建议给|应得|应该得|应该给|改为|改成|调高到|调低到|调到|提高到|降到)" + _NUM + r"\s*分(?!钟)"
    + r"|(?:分数|得分|总分|成绩|分值)[^。！？!?；;\n]{0,8}?(?:改为|改成|调高到|调低到|调到|提高到|降到|给)" + _NUM
    + r"|(?:加|减|扣|多给|少给)" + _NUM + r"\s*分(?!钟)|可以加分|调高分数|调低分数"
)
SENTENCE_PATTERN = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?|\n")
INJECTION_PLACEHOLDER = "（该条疑似包含指令，未显示）"

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


def without_scores(text) -> str:
    """去掉含分数样式或改分说法的句子。"""

    return "".join(piece for piece in SENTENCE_PATTERN.findall(str(text or ""))
                   if piece == "\n" or not (SCORE_PATTERN.search(piece) or SCORE_CHANGE_PATTERN.search(piece))).strip()


def revision_suggestion(suggestion, auto_score_status=None) -> str | None:
    """评分模型针对内容写的修改建议；系统说明、没评出结果的项返回 None。

    评分页、运行导出与评分助手看到的是同一份：带分数的句子去掉（修改建议不谈分数），疑似注入的整条
    换成占位说明（源自学生文档）。长度不在这里截：评分页显示全文，助手自己限长（§31.3）。
    """

    raw = str(suggestion or "").strip()
    if not raw or auto_score_status not in _SCORED:
        return None
    if any(marker in raw for marker in SYSTEM_SUGGESTION_MARKERS):
        return None
    if detect_injection(raw):
        return INJECTION_PLACEHOLDER
    return without_scores(raw) or None


__all__ = ["INJECTION_PLACEHOLDER", "SCORE_CHANGE_PATTERN", "SCORE_PATTERN", "SENTENCE_PATTERN", "SYSTEM_SUGGESTION_MARKERS",
           "revision_suggestion", "without_scores"]
