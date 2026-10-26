"""最终回复的输出契约与校验（Agent 化方案 §14）。

模型以调用 `reply` 结束一个回合。代码校验之后才显示，三种处理：

- **整体作废**（重试一次，仍不合格就给确定性兜底）：参数不合 schema；出现本回合工具结果里没有的
  引用编号或不在清单里的下一步（视为编造）；命中注入检测；说自己做了写动作、说出了确认卡片却没有、
  说出工具没返回过的任务状态（`claims`，§14.1）；
- **单句丢弃**：含分数样式的数字、改分建议、链接、邮箱或标记语言的句子去掉；
- **超长截断**：`text` 最多 300 字。

前端一律按纯文本显示。分数不经模型转述：要展示数据就把引用编号放进 `cards`，卡片由界面现取。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError

from backend.app.services.assistant.agent import claims
from backend.app.services.assistant.agent.refs import TurnRefs
from backend.app.services.assistant.views import QUICK_ACTIONS
from backend.app.services.scoring.core.evidence import detect_injection
from backend.app.services.scoring.suggestions import SCORE_CHANGE_PATTERN
from backend.app.services.scoring.suggestions import SCORE_PATTERN
from backend.app.services.scoring.suggestions import SENTENCE_PATTERN

MAX_TEXT_CHARS = 300
MAX_CARDS = 3

#: 回复里能建议的下一步：渲染成快捷按钮，点了按键直接分派（§16）。
NEXT_STEPS = tuple(QUICK_ACTIONS)


class ReplyParams(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    cards: list[str] = Field(default_factory=list, max_length=10)
    next_steps: list[str] = Field(default_factory=list, max_length=6)
    help_refs: list[str] = Field(default_factory=list, max_length=3)


class ReplyInvalid(Exception):
    """整体作废；`reason` 是给模型看的修正提示。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ValidReply:
    text: str
    cards: tuple[str, ...]
    next_steps: tuple[str, ...]
    dropped_sentences: int
    help_refs: tuple[str, ...] = ()


# 分数样式与分句：与评分侧的修改建议过滤同一份（scoring/suggestions.py）。
_SCORE = SCORE_PATTERN
# 与分数有关的建议才拦：“改为 8 分”“建议给 3 分”“加 2 分”“应得 6 分”“得分调到 7”“将总分改为 8”。A1.1 起论文的
# 修改建议（“建议把研究方法一节改为分步骤说明”“把引用数量改为 8 条”）要能说出来（§31.3）。与评分侧共用一份。
_SUGGESTION = SCORE_CHANGE_PATTERN
_LINK = re.compile(r"https?://|www\.|[\w.+-]+@[\w-]+\.[\w.]+|<[a-zA-Z/][^>]*>|\*\*|__|`|^#+\s", re.MULTILINE)
_SENTENCE = SENTENCE_PATTERN


def _clean_text(text: str) -> tuple[str, int]:
    kept, dropped = [], 0
    for piece in _SENTENCE.findall(text):
        if piece == "\n":
            kept.append(piece)
            continue
        if _SCORE.search(piece) or _SUGGESTION.search(piece) or _LINK.search(piece):
            dropped += 1
            continue
        kept.append(piece)
    cleaned = re.sub(r"\n{3,}", "\n\n", "".join(kept)).strip()
    if len(cleaned) > MAX_TEXT_CHARS:
        cleaned = cleaned[: MAX_TEXT_CHARS - 1].rstrip() + "…"
    return cleaned, dropped


def parse_arguments(arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    try:
        value = json.loads(arguments or "{}")
    except (TypeError, ValueError) as exc:
        raise ReplyInvalid("参数不是合法的 JSON 对象") from exc
    if not isinstance(value, dict):
        raise ReplyInvalid("参数不是合法的 JSON 对象")
    return value


def validate_reply(arguments, refs: TurnRefs, *, allow_cards: bool = True, check_claims: bool = True) -> ValidReply:
    """校验一次 `reply` 调用。整体作废时抛 `ReplyInvalid`。

    ``check_claims=False`` 只给压缩摘要用：摘要转述的是过去的对话（“老师取消了任务”），不是本回合的说法。
    """

    try:
        params = ReplyParams.model_validate(parse_arguments(arguments))
    except ValidationError as exc:
        raise ReplyInvalid("参数不合格：%s" % exc.errors()[0]["msg"]) from exc
    if detect_injection(params.text):
        raise ReplyInvalid("回复里出现了疑似提示注入的内容")
    if check_claims:
        problem = claims.problem(params.text, refs)
        if problem:
            raise ReplyInvalid(problem)
    if not allow_cards and (params.cards or params.next_steps or params.help_refs):
        raise ReplyInvalid("这里只要文字，cards、next_steps 与 help_refs 必须为空")
    unseen = [ref for ref in params.help_refs if ref not in refs.help_seen]
    if unseen:
        raise ReplyInvalid("help_refs 里的“%s”不是本回合 help_lookup 返回的条目" % "、".join(unseen))
    cards = []
    for ref in params.cards:
        if refs.object_of(ref) is None:
            raise ReplyInvalid("cards 里的编号“%s”不在本回合的工具结果里" % ref)
        if ref not in cards:
            cards.append(ref)
    unknown = [step for step in params.next_steps if step not in NEXT_STEPS]
    if unknown:
        raise ReplyInvalid("next_steps 只能从这些里选：%s" % "、".join(NEXT_STEPS))
    text, dropped = _clean_text(params.text)
    if not text:
        raise ReplyInvalid("回复去掉分数、改分建议与链接后没有剩下内容；分数请用 cards 展示")
    return ValidReply(text=text, cards=tuple(cards[:MAX_CARDS]),
                      next_steps=tuple(dict.fromkeys(params.next_steps)), dropped_sentences=dropped,
                      help_refs=tuple(dict.fromkeys(params.help_refs)))


__all__ = ["MAX_TEXT_CHARS", "NEXT_STEPS", "ReplyInvalid", "ReplyParams", "ValidReply", "parse_arguments", "validate_reply"]
