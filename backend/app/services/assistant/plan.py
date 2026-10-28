"""计划卡片（docs/评分助手计划卡片方案.md）：流程开始时由代码列出步骤，每走一步在原卡片上更新。

步骤是固定的，不由模型规划。卡片和其它卡片一样存在消息里；流程状态只记它的编号
（`{"message_id", "card_id"}`），编号为空（没有计划的流程，例如重试后的观察）时这里的函数什么都不做。
计划卡片不进 Agent 的上下文（不在 `agent/context.CARD_LABELS` 里），提示词不因它改变。
"""

from __future__ import annotations

from backend.app.db.models import AssistantMessage
from backend.app.services.assistant.context import RunContext
from backend.app.services.assistant.context import new_card_id

_TAIL = (
    ("files", "选择待评分文件"),
    ("confirm", "确认开始"),
    ("upload", "上传与预检"),
    ("scoring", "评分"),
    ("report", "汇报结果"),
)
STEPS = {
    "grading": (("rubric", "评分标准"), ("model", "评分模型"), *_TAIL),
    "import": (("import", "上传评分规则和模板"), ("publish", "在右侧核对并发布"), ("model", "评分模型"), *_TAIL),
}
INTRO = "这次评分的步骤如下，每走完一步我会在这里更新。"

#: 卡片状态：进行中、完成、没完成、已放弃、已过期；`ended` 是兜底（结束时还在进行中，不猜成败）。
CARD_STATUSES = frozenset({"running", "done", "failed", "dismissed", "expired", "ended"})


def start(ctx: RunContext, flow: str, *, done: dict[str, str | None] | None = None,
          blocked: dict[str, str] | None = None) -> dict:
    """发出计划卡片：`done` 里的步骤已完成（值是说明），`blocked` 里的缺东西，第一步没完成的进行中。"""

    done = done or {}
    blocked = blocked or {}
    steps = []
    for key, label in STEPS[flow]:
        if key in done:
            steps.append({"key": key, "label": label, "status": "done", "detail": done[key]})
        elif key in blocked:
            steps.append({"key": key, "label": label, "status": "blocked", "detail": blocked[key]})
        else:
            steps.append({"key": key, "label": label, "status": "todo", "detail": None})
    _promote(steps)
    card_id = new_card_id("plan")
    message = ctx.say(INTRO, [{"id": card_id, "type": "plan", "status": "running", "flow": flow, "steps": steps}])
    return {"message_id": message.id, "card_id": card_id}


def update(ctx: RunContext, ref: dict | None, steps: dict[str, tuple[str, str | None]] | None = None, *,
           status: str | None = None) -> None:
    """改几步的状态与说明（说明为 None 时不改），可同时改卡片状态。不在这张卡片上的步骤忽略。"""

    found = _find(ctx, ref)
    if found is None:
        return
    _message, card = found
    current = [dict(step) for step in card.get("steps") or []]
    for step in current:
        change = (steps or {}).get(step["key"])
        if change is None:
            continue
        step["status"], detail = change
        if detail is not None:
            step["detail"] = detail
    fields = {"steps": current}
    if status is not None:
        if status not in CARD_STATUSES:
            raise ValueError("unsupported plan status: %s" % status)
        fields["status"] = status
    ctx.patch_card(ref["message_id"], ref["card_id"], fields)


def advance(ctx: RunContext, ref: dict | None, key: str, detail: str | None = None) -> None:
    """这一步完成，后面第一步没完成的变成进行中。"""

    found = _find(ctx, ref)
    if found is None:
        return
    _message, card = found
    steps = [dict(step) for step in card.get("steps") or []]
    for step in steps:
        if step["key"] == key:
            step["status"] = "done"
            if detail is not None:
                step["detail"] = detail
        elif step["status"] == "current":
            step["status"] = "todo"
    _promote(steps, after=key)
    ctx.patch_card(ref["message_id"], ref["card_id"], {"steps": steps})


def fail(ctx: RunContext, ref: dict | None, text: str) -> None:
    """进行中（或等你决定）的那一步没完成，说明写原因；卡片没完成。"""

    found = _find(ctx, ref)
    if found is None:
        return
    _message, card = found
    steps = [dict(step) for step in card.get("steps") or []]
    target = next((step for step in steps if step["status"] in {"current", "blocked"}), None)
    if target is not None:
        target["status"] = "failed"
        target["detail"] = text
    ctx.patch_card(ref["message_id"], ref["card_id"], {"steps": steps, "status": "failed"})


def close(ctx: RunContext, ref: dict | None, status: str) -> None:
    """流程被放弃或过期：卡片换状态；放弃时进行中的那一步记为跳过。卡片已经结束的不再改。"""

    found = _find(ctx, ref)
    if found is None or found[1].get("status") != "running":
        return
    steps = [dict(step) for step in found[1].get("steps") or []]
    if status == "dismissed":
        for step in steps:
            if step["status"] in {"current", "blocked"}:
                step["status"] = "skipped"
    update_status = status if status in CARD_STATUSES else "ended"
    ctx.patch_card(ref["message_id"], ref["card_id"], {"steps": steps, "status": update_status})


def is_running(ctx: RunContext, ref: dict | None) -> bool:
    found = _find(ctx, ref)
    return found is not None and found[1].get("status") == "running"


def _promote(steps: list[dict], after: str | None = None) -> None:
    keys = [step["key"] for step in steps]
    start_index = keys.index(after) + 1 if after in keys else 0
    for step in steps[start_index:]:
        if step["status"] in {"todo", "blocked"}:
            step["status"] = "current" if step["status"] == "todo" else "blocked"
            return
        if step["status"] != "done":
            return


def _find(ctx: RunContext, ref: dict | None):
    if not ref or not ref.get("message_id") or not ref.get("card_id"):
        return None
    message = ctx.db.get(AssistantMessage, ref["message_id"])
    if message is None or message.conversation_id != ctx.conversation.id:
        return None
    card = next((card for card in message.cards or [] if card.get("id") == ref["card_id"]), None)
    if card is None or card.get("type") != "plan":
        return None
    return message, card
