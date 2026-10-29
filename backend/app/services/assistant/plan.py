"""计划卡片（docs/评分助手计划卡片方案.md）：流程开始时由代码列出步骤，每走一步在原卡片上更新。

步骤是固定的，不由模型规划。卡片和其它卡片一样存在消息里；流程状态只记它的编号
（`{"message_id", "card_id"}`），编号为空（没有计划的流程，例如重试后的观察）时这里的函数什么都不做。
计划卡片不进 Agent 的上下文（不在 `agent/context.CARD_LABELS` 里），提示词不因它改变。
"""

from __future__ import annotations

from fastapi import HTTPException

from backend.app.db.models import AssistantMessage
from backend.app.db.models import utcnow
from backend.app.services.assistant import tools
from backend.app.services.assistant.context import RunContext
from backend.app.services.assistant.context import new_card_id
from backend.app.services.assistant.views import quick_actions_card

# 先上传预检、再确认开始（docs/评分助手v2界面改造方案.md §4.1）：确认时老师看得到哪几份能评。
_TAIL = (
    ("files", "选择待评分文件"),
    ("upload", "上传与预检"),
    ("confirm", "确认开始评分"),
    ("scoring", "评分"),
    ("report", "汇报结果"),
)
STEPS = {
    "grading": (("rubric", "评分标准"), ("model", "评分模型"), *_TAIL),
    "import": (("import", "导入评分标准"), ("publish", "核对并发布评分标准"), ("model", "评分模型"), *_TAIL),
}
INTRO = "这次评分分 %d 步完成，进度显示在右侧工作区顶部。"

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
    message = ctx.say(INTRO % len(steps), [{"id": card_id, "type": "plan", "status": "running", "flow": flow, "steps": steps}])
    ref = {"message_id": message.id, "card_id": card_id}
    _patch(ctx, ref, {"steps": steps, "status": "running"})
    return ref


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
    _patch(ctx, ref, fields, card)


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
    _patch(ctx, ref, {"steps": steps}, card)


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
    _patch(ctx, ref, {"steps": steps, "status": "failed"}, card)


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
    _patch(ctx, ref, {"steps": steps, "status": update_status}, found[1])


def is_running(ctx: RunContext, ref: dict | None) -> bool:
    found = _find(ctx, ref)
    return found is not None and found[1].get("status") == "running"


def latest(ctx: RunContext):
    """本会话最近的一张计划卡片，返回 (消息, 卡片)；没有时返回 (None, None)。"""

    for message in reversed(ctx.conversation.messages):
        card = next((card for card in message.cards or [] if card.get("type") == "plan"), None)
        if card is not None:
            return message, card
    return None, None


def progress(steps: list[dict], status: str) -> dict:
    """会话列表里的“步骤 2/8”：第一步没完成的是第几步；都完成了就是最后一步。"""

    total = len(steps)
    index = next((i for i, step in enumerate(steps) if step["status"] not in {"done", "skipped"}), total - 1)
    return {"status": status, "step": index + 1, "total": total}


# --- 按计划回答（不经模型）：进展快照与“接下来做什么”（docs/评分助手v2界面改造方案.md §4.5、§4.6） -------------

#: 用进展快照回答的计划状态：进行中或已完成。放弃、过期、没完成的计划是旧事，照旧查任务进度。
SNAPSHOT_STATUSES = frozenset({"running", "done"})


def _current(steps: list[dict]):
    return next(((index, step) for index, step in enumerate(steps) if step["status"] not in {"done", "skipped"}),
                (None, None))


def _job_counts(ctx: RunContext) -> dict | None:
    job_id = (ctx.conversation.focus or {}).get("job_id")
    if not job_id:
        return None
    try:
        job = tools.call(ctx, "job", job_id=job_id)
    except HTTPException:
        return None
    return {"status": job["status"], "total": job["total_items"], "failed": job["failed_count"],
            "done": job["succeeded_count"] + job["skipped_count"]}


def progress_text(card: dict, *, waiting: bool, job: dict | None) -> str:
    steps = card.get("steps") or []
    if card.get("status") == "done":
        return "%d 个步骤已全部完成。" % len(steps)
    index, step = _current(steps)
    if step is None:
        return "这次评分的步骤都走完了。"
    head = "现在在第 %d 步「%s」" % (index + 1, step["label"])
    if step["key"] == "scoring" and job:
        head += "，已评完 %d/%d 份" % (job["done"], job["total"])
    elif step["status"] == "blocked" or waiting:
        head += "，等你处理" + ("：%s" % step["detail"] if step.get("detail") else "")
    elif step.get("detail"):
        head += "：%s" % step["detail"]
    done = [item["label"] for item in steps if item["status"] == "done"]
    return head + "。" + ("已完成：%s。" % "、".join(done) if done else "")


def answer_progress(ctx: RunContext, *, waiting: bool) -> bool:
    """流程里问进展：回一张进展快照（提问时的计划副本，不随后续变化）。会话里没有可用的计划时返回 False。"""

    _message, card = latest(ctx)
    if card is None or card.get("status") not in SNAPSHOT_STATUSES:
        return False
    job = _job_counts(ctx) if card.get("status") == "running" else None
    ctx.say(progress_text(card, waiting=waiting, job=job), [{
        "type": "progress_snapshot", "as_of": utcnow().isoformat(), "flow": card.get("flow"),
        "plan_status": card.get("status"), "steps": [dict(step) for step in card.get("steps") or []],
        "job": job, "batch_id": (ctx.conversation.focus or {}).get("batch_id"),
    }], intent="query_progress")
    return True


def answer_next_steps(ctx: RunContext) -> None:
    """“接下来我要做什么？”：清单由前端按计划实时算，这里只给一句话和一张待办卡片的编号。"""

    _message, card = latest(ctx)
    if card is None or card.get("status") != "running":
        ctx.say("现在没有进行中的评分。可以开始一次评分，或者用你自己的评分规则和模板。",
                [quick_actions_card(["start_grading", "import_rubric"])], intent="next_steps")
        return
    index, step = _current(card.get("steps") or [])
    text = "清单在右侧，按顺序处理即可，完成一项会自动勾选。"
    if step is not None:
        text += "现在先处理第 %d 步「%s」。" % (index + 1, step["label"])
    ctx.say(text, [{"type": "todo", "plan_card_id": card.get("id")}], intent="next_steps")


def _patch(ctx: RunContext, ref: dict, fields: dict, card: dict | None = None) -> None:
    """改卡片，并把进度记到焦点的 `progress`（会话列表显示用，不进 Agent 的状态说明）。"""

    ctx.patch_card(ref["message_id"], ref["card_id"], fields)
    steps = fields.get("steps") or (card or {}).get("steps") or []
    status = fields.get("status") or (card or {}).get("status") or "running"
    if steps:
        ctx.set_progress(progress(steps, status))


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
