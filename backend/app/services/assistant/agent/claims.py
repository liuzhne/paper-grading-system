"""回复里关于“做了什么”“现在是什么状态”的说法（Agent 化方案 §14.1，维护者 2026-10-25 定）。

A1 第二轮评测里，模型没有调用取消工具，却回复“课程报告评分任务已经暂停”——老师会以为停了。
Agent 永远不自己执行写动作，所以回复校验按三类处理（不对照操作记录）：

- **“我已经做了 X”**（暂停、取消、开始评分、重试、导入、发布、删除、改分……）：一律不成立，整体作废；
- **“已经为你准备好确认卡片”“在卡片上点确认”**：只有本回合确实出了确认卡片才放行；
- **“任务已取消 / 已评完 / 正在评分”这类状态**：只有本回合某个工具返回过这个状态才放行，否则当作编造。

工具结果里的状态由业务工具按状态码登记到具体对象上（`TurnRefs.saw_status`，不在正文里猜）。状态说法要对得上
它说的那个对象：一句话里点了名（编号或工具结果里的任务名、材料名），就看那个对象（材料还可以看它所属的任务）；
没点名就当作说的是任务，只认任务的状态——查过某一份“已评分”，不能据此说“这批已经评完了”。
"""

from __future__ import annotations

import re

#: 一个分句之内（不跨逗号、句号）。
_IN_CLAUSE = r"[^，,。！？!?；;\n]"
_WRITE = r"(?:暂停|停止|停掉|中止|取消|开始评分|开始评|开评|重试|重新评分|重评|导入|发布|删除|归档|修改|改分|改成|改为|提交|上传|创建|新建)"
#: “帮你 / 为您 / 替你 / 给您”：敬称“您”也要认（“已为您取消了评分”）。
_FOR_YOU = r"(?:帮|为|替|给)(?:你|您)"
_BY_AGENT = re.compile(
    # “我已经帮你取消了”“我刚把它重试了”
    r"我" + _IN_CLAUSE + r"{0,6}?(?:已经?|刚刚?|刚才)" + _IN_CLAUSE + r"{0,12}?" + _WRITE
    # “已为你重试失败的材料”“已为您取消了评分”
    + r"|(?:已经?|刚刚?)" + _FOR_YOU + _IN_CLAUSE + r"{0,12}?" + _WRITE
    # “我帮你取消了评分”（“我可以帮你重试”不算：没有“了”）
    + r"|我" + _FOR_YOU + _IN_CLAUSE + r"{0,12}?" + _WRITE + _IN_CLAUSE + r"{0,6}?了"
)
_CARD = re.compile(
    # “已经为你准备好确认卡片”“已生成卡片”
    r"(?:已经?)(?:" + _FOR_YOU + r")?(?:准备|生成|弹出|打开|出|发)(?:好)?了?" + _IN_CLAUSE + r"{0,6}?卡片"
    # “请在下面的卡片上确认”“见下方卡片”
    + r"|(?:下面|下方|上面|这张)的?" + _IN_CLAUSE + r"{0,4}?卡片"
    # “请点确认”“请您点击“确认””（“请确认是哪一份”不算：那是请老师选）
    + r"|请您?(?:在卡片上|在下面|点击?|点一下)\s*[“\"「]?确认"
)
#: 一句话（判断状态说的是谁，按整句找点名）。
_SENTENCE = re.compile(r"[^。！？!?；;\n]+")
_REF_TOKEN = re.compile(r"(?<![A-Za-z0-9])[tp]\d+(?![A-Za-z0-9])")
#: 状态类别 → 回复里的说法。
_STATUS = {
    "canceled": re.compile(r"(?:已经?)(?:被)?(?:取消|暂停|停止|停掉|中止)|被(?:取消|暂停|停止|中止)"),
    "finished": re.compile(r"(?:已经?|都|全部)(?:评完|评分完成|完成评分|评好)|评分(?:已经?)?(?:完成|结束)|评完了"),
    "running": re.compile(r"正在评分|评分中|(?:已经?)开始评(?:分)?"),
    "retried": re.compile(r"(?:已经?)(?:重试|重新评分|重评)"),
}
STATUS_LABELS = {"canceled": "已取消", "canceling": "正在取消", "finished": "已评完", "running": "评分中",
                 "retried": "已重试", "failed": "评分失败"}
#: 工具结果里的状态码 → 类别。评分作业七态（batch_scoring/jobs.py）与材料状态。
STATUS_OF_CODE = {
    "queued": "running", "running": "running",
    # 提出了取消、还没生效：不能据此说“已取消”。
    "cancel_requested": "canceling", "canceled": "canceled",
    "completed": "finished", "completed_with_errors": "finished",
    "failed": "failed",
    # 材料：有评分结果就是这一份评完了。
    "已评分": "finished", "需要人工复核": "finished",
}


def done_by_agent(text: str) -> str | None:
    """“我已经做了 X”这类说法；没有返回 None。"""

    found = _BY_AGENT.search(text or "")
    return found.group(0) if found else None


def card_claim(text: str) -> str | None:
    found = _CARD.search(text or "")
    return found.group(0) if found else None


def status_claims(text: str) -> dict[str, str]:
    """回复里出现的状态说法：类别 → 原文片段。"""

    claims = {}
    for category, pattern in _STATUS.items():
        found = pattern.search(text or "")
        if found:
            claims[category] = found.group(0)
    return claims


def category_of(status: str | None) -> str | None:
    return STATUS_OF_CODE.get(str(status or ""))


def _subjects(sentence: str, refs) -> set[tuple[str, str]]:
    """这句话点名的对象：编号（t1、p2）或工具结果里出现过的任务名、材料名。"""

    found = {refs.object_of(token) for token in _REF_TOKEN.findall(sentence)}
    found |= {key for name, key in refs.names.items() if name and name in sentence}
    return {key for key in found if key is not None}


def _backed(category: str, sentence: str, refs) -> bool:
    subjects = _subjects(sentence, refs)
    if not subjects:  # 没点名：当作说的是任务
        return any(kind == "t" and seen == category for kind, _object_id, seen in refs.evidence)
    for subject in subjects:
        for kind, object_id in filter(None, (subject, refs.parents.get(subject))):
            if (kind, object_id, category) in refs.evidence:
                return True
    return False


def problem(text: str, refs) -> str | None:
    """不成立的说法给出修正提示（给模型看）；都成立返回 None。

    ``refs`` 是本回合的 `TurnRefs`：确认卡片（``proposed``）、状态依据（``evidence``）、名称与所属任务、
    工具结果原文（``seen_text``，说法原样出现在这里也算工具返回过）。
    """

    seen_text = "\n".join(refs.seen_text)
    claim = done_by_agent(text)
    if claim:
        return ("你不能自己执行改动，不要说“%s”。老师要做的事：先用 tool_search 加载对应的 action_* 工具"
                "（例如 select:action_cancel），再用 call_tool 调用，它会生成确认卡片；做不到的就直说。" % claim)
    claim = card_claim(text)
    if claim and not refs.proposed and claim not in seen_text:
        return ("本回合没有出确认卡片，不能说“%s”。要出卡片，先加载并调用对应的 action_* 工具。" % claim)
    for sentence in _SENTENCE.findall(text or ""):
        for category, claim in status_claims(sentence).items():
            if claim in seen_text or _backed(category, sentence, refs):
                continue
            seen = "、".join(sorted({STATUS_LABELS.get(item, item) for _kind, _object_id, item in refs.evidence})) or "无"
            return ("回复里说“%s”，但本回合没有工具返回它说的那个任务或材料是这个状态。只按工具结果描述状态"
                    "（本回合看到的状态：%s）；要知道任务状态先用 task_overview 查。" % (claim, seen))
    return None


__all__ = ["STATUS_OF_CODE", "card_claim", "category_of", "done_by_agent", "problem", "status_claims"]
