"""最终回复的输出契约与校验（Agent 化方案 §14）。

模型以调用 `reply` 结束一个回合。代码校验之后才显示，三种处理：

- **整体作废**（重试一次，仍不合格就给确定性兜底）：参数不合 schema；出现本回合工具结果里没有的
  引用编号或不在清单里的下一步（视为编造）；`cards` 里的编号界面上显示不出任何东西；命中注入检测；
  说自己做了写动作、说出了确认卡片却没有、说出工具没返回过的任务状态（`claims`，§14.1）；
  清理后的 `text` 以冒号结尾、后面却没有卡片、代码列表或帮助条目（§14.2）；
- **去掉记号**：Markdown 的加粗、反引号、标题井号去掉，行首的 `*`、`+` 换成“·”，文字保留；
- **单句丢弃**：含分数样式的数字、改分建议、链接、邮箱或 HTML 标签的句子去掉；
- **超长截断**：`text` 最多 300 字。

前端一律按纯文本显示。分数不经模型转述：要展示数据就把引用编号放进 `cards`，卡片由界面现取；
没有卡片的对象（评分标准、评分项）由代码按工具结果列出名称（`turn.attachments`）。
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
_LINK = re.compile(r"https?://|www\.|[\w.+-]+@[\w-]+\.[\w.]+|<[a-zA-Z/][^>]*>")
_SENTENCE = SENTENCE_PATTERN
#: Markdown 记号只去掉记号、保留文字（§14.2）。原来整句丢弃：模型把列表写成“**名称**”时，列表整行没了，
#: 只剩“……有：”（11-05 生产）。行内的单个 `*`、`_` 不动：“a_b.docx”这类文件名里会出现。
_MARKUP = (
    (re.compile(r"\*\*|`"), ""),
    (re.compile(r"__(?=\S)(.+?)(?<=\S)__"), r"\1"),
    (re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE), ""),
    (re.compile(r"^([ \t]*)[*+][ \t]+", re.MULTILINE), r"\1· "),
)
#: 以冒号结尾：后面本该有内容（“目前已发布的评分标准有：”）。
_TRAILING_COLON = ("：", ":")


def _strip_markup(text: str) -> str:
    for pattern, replacement in _MARKUP:
        text = pattern.sub(replacement, text)
    return text


def _clean_text(text: str) -> tuple[str, int]:
    """整句丢分数句、改分建议、链接；传入的是已经去掉 Markdown 记号的文字（validate_reply 里先做）。"""

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
    # 注入检测与说法核对看去掉记号之后的文字：老师看到的是它。只看原文时，“评分已经**完成**”
    # 对不上“已经完成”，去掉记号后却原样显示（#78 复审）。
    visible = _strip_markup(params.text)
    if detect_injection(params.text) or detect_injection(visible):
        raise ReplyInvalid("回复里出现了疑似提示注入的内容")
    if check_claims:
        problem = claims.problem(visible, refs)
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
        if not refs.displayable(ref):
            # 以前静默丢掉：回复的引导句后面什么都没有（§14.2）。
            raise ReplyInvalid("cards 里的“%s”在界面上显示不出来：先用工具查到它，或把名称直接写进 text" % ref)
        if ref not in cards:
            cards.append(ref)
    unknown = [step for step in params.next_steps if step not in NEXT_STEPS]
    if unknown:
        raise ReplyInvalid("next_steps 只能从这些里选：%s" % "、".join(NEXT_STEPS))
    text, dropped = _clean_text(visible)
    if not text:
        raise ReplyInvalid("回复去掉分数、改分建议与链接后没有剩下内容；分数请用 cards 展示")
    if allow_cards and text.endswith(_TRAILING_COLON) and not cards and not params.help_refs:
        # 快捷按钮不算：“目前已发布的评分标准有：”后面跟两个按钮，老师看到的仍是空列表（11-05 生产）。
        raise ReplyInvalid("text 以冒号结尾，后面却没有内容：要列出的名称直接写进 text，或把工具结果里的编号放进 cards")
    # 卡片数上限在显示时处理（turn.attachments）：超出的用名称补一行，不再静默截掉。
    return ValidReply(text=text, cards=tuple(cards),
                      next_steps=tuple(dict.fromkeys(params.next_steps)), dropped_sentences=dropped,
                      help_refs=tuple(dict.fromkeys(params.help_refs)))


__all__ = ["MAX_CARDS", "MAX_TEXT_CHARS", "NEXT_STEPS", "ReplyInvalid", "ReplyParams", "ValidReply", "parse_arguments", "validate_reply"]
