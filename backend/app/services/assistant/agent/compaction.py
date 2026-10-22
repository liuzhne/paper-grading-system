"""历史压缩（Agent 化方案 §12.4，取代截断）。

触发：攒到 12 轮，或历史超过 8,000 token；或缓存已冷（距上一轮超过 30 分钟、部署或换模型后的第一轮）
且历史超过 6 轮；或两次压缩之间加载记录里的工具超过 20 个。保留最近 6 轮（不超过 4,000 token，至少 2 轮），
被移出的轮次变成 ③ 的压缩内容，三部分：

- 事实要点：代码从消息与卡片里整理，不调用模型，总是有；
- 老师的要求与未完成的问题：模型摘要，过输出校验，不超过 300 字；
- 仍在使用的工具：被移出的轮次里加载、在保留的轮次里又调用过的工具（最多 12 个）。

摘要请求与主对话**完全一样**：同样的 tools、tool_choice、思考设置、系统指令与完整历史，只在末尾追加一条
摘要指令，所以前缀命中上一轮的缓存。失败（超时、限流、输出不合格）时本轮先不压缩，下一轮再试；
连续两轮失败就只用事实要点压缩。压缩内容只作背景，不能作为重评意见或改分分数的来源（§11.3）。
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Callable

from sqlalchemy.orm.attributes import flag_modified

from backend.app.db.models import utcnow
from backend.app.services.assistant.agent import catalog
from backend.app.services.assistant.agent.context import TurnGroup
from backend.app.services.assistant.agent.context import estimate_tokens
from backend.app.services.assistant.agent.context import render_group
from backend.app.services.assistant.agent.refs import TurnRefs
from backend.app.services.assistant.agent.reply import ReplyInvalid
from backend.app.services.assistant.agent.reply import validate_reply

logger = logging.getLogger(__name__)

TRIGGER_TURNS = 12
TRIGGER_TOKENS = 8000
TRIGGER_LOADED = 20
KEEP_TURNS = 6
KEEP_TOKENS = 4000
MIN_KEEP = 2
COLD_AFTER = timedelta(minutes=30)
MAX_FACTS = 20
MAX_STILL_USED = 12
MAX_FAILURES = 2
SUMMARY_SECONDS = 10

SUMMARY_INSTRUCTION = (
    "【压缩指令】对话较长，较早的轮次要压缩成一段概要（这一条不是老师的提问）。请只调用 reply，把概要写在 text 里，"
    "不超过 300 字，cards 与 next_steps 留空。只写两样：老师提出的偏好与约束（例如只关心哪个评分项、回答要简短）、"
    "还没解决的问题。不写分数、不写改分建议、不复述学生原文；“之前对话的概要”里仍然有效的要求要保留；"
    "两样都没有就写“无”。"
)

_INTENT_FACTS = {
    "query_progress": "问过评分进度", "query_paper": "查过材料的得分与扣分", "query_review": "看过需要复核的材料",
    "query_overview": "看过整体分数情况", "retry_failed": "要求重试失败项", "cancel_job": "要求取消评分",
    "start_grading": "要求开始评分", "import_rubric": "要求导入评分标准",
}
_CARD_FACTS = {
    ("confirm_start", "done"): "确认开始了一次评分", ("confirm_start", "dismissed"): "放弃了一次开始评分",
    ("confirm_retry", "done"): "确认了重试失败项", ("confirm_retry", "dismissed"): "没有重试失败项",
    ("diagnosis", "done"): "确认了重试失败项",
    ("confirm_cancel", "done"): "确认取消了剩余评分", ("confirm_cancel", "dismissed"): "没有取消评分",
    ("confirm_switch_flow", "done"): "放弃了上一步、开始了新的操作",
    ("confirm_switch_flow", "dismissed"): "选择继续上一步",
    ("pick_rubric_files", "done"): "提交了评分规则与模板",
}


def _card_facts(card: dict) -> list[str]:
    facts = []
    key = (card.get("type"), card.get("status"))
    if key in _CARD_FACTS:
        facts.append(_CARD_FACTS[key])
    result = card.get("result") or {}
    if card.get("type") == "choose_batch" and result.get("batch_id"):
        name = next((item.get("name") for item in card.get("batches") or [] if item.get("id") == result["batch_id"]), None)
        if name:
            facts.append("谈过任务《%s》" % name)
    if card.get("type") == "choose_paper" and result.get("paper_id"):
        label = next((item.get("label") for item in card.get("papers") or [] if item.get("paper_id") == result["paper_id"]), None)
        if label:
            facts.append("谈过材料 %s" % label)
    if card.get("type") == "job_progress" and result.get("status"):
        facts.append("一次评分作业已结束（%s）" % result["status"])
    if card.get("type") == "diagnosis" and card.get("failed_count"):
        facts.append("有 %d 份没有评完" % card["failed_count"])
    return facts


def facts_from(groups: list[TurnGroup]) -> list[str]:
    facts: list[str] = []
    for group in groups:
        fact = _INTENT_FACTS.get(group.user.intent or "")
        if fact:
            facts.append(fact)
        for message in group.assistants:
            for card in message.cards or []:
                facts.extend(_card_facts(card))
        tools = catalog.registry()
        for name in group.called:
            if name in tools:
                facts.append("用过工具：%s" % tools[name].title)
    return facts


def merge_facts(previous: list[str], new: list[str]) -> list[str]:
    merged: list[str] = []
    for fact in [*previous, *new]:
        if fact.startswith("另有 ") and fact.endswith("条较早的记录"):
            continue
        if fact in merged:
            merged.remove(fact)  # 同一事实只留最近一次的位置
        merged.append(fact)
    if len(merged) > MAX_FACTS:
        omitted = len(merged) - (MAX_FACTS - 1)
        merged = ["另有 %d 条较早的记录" % omitted, *merged[-(MAX_FACTS - 1):]]
    return merged


def _group_tokens(group: TurnGroup) -> int:
    return sum(estimate_tokens(catalog.render(item)) for item in render_group(group))


def keep_from(groups: list[TurnGroup]) -> list[TurnGroup]:
    keep = groups[-KEEP_TURNS:]
    while len(keep) > MIN_KEEP and sum(_group_tokens(group) for group in keep) > KEEP_TOKENS:
        keep = keep[1:]
    return keep


def still_used(dropped: list[TurnGroup], kept: list[TurnGroup], previous: list[str]) -> list[str]:
    candidates = [*previous, *(name for group in dropped for name in group.loaded)]
    kept_loaded = {name for group in kept for name in group.loaded}
    last_call: dict[str, int] = {}
    for position, group in enumerate(kept):
        for name in group.called:
            last_call[name] = position
    tools = catalog.registry()
    used = [
        name for name in dict.fromkeys(candidates)
        if name in last_call and name not in kept_loaded and name in tools and not tools[name].resident
    ]
    used.sort(key=lambda name: -last_call[name])
    return sorted(used[:MAX_STILL_USED], key=candidates.index)


def trigger_reason(groups: list[TurnGroup], *, history_tokens: int, loaded: int, idle, identity_changed: bool) -> str | None:
    count = len(groups)
    if count >= TRIGGER_TURNS:
        return "turns"
    if count > MIN_KEEP and history_tokens > TRIGGER_TOKENS:
        return "tokens"
    if count > MIN_KEEP and loaded > TRIGGER_LOADED:
        return "loaded"
    if count > KEEP_TURNS and ((idle is not None and idle > COLD_AFTER) or identity_changed):
        return "cold"
    return None


def maybe_compact(conversation, groups: list[TurnGroup], *, prefix_messages: list[dict], history_tokens: int,
                  loaded: int, identity_changed: bool, summarize: Callable[[list[dict], float], object] | None,
                  model_name: str | None) -> bool:
    """需要时压缩并写回 `conversation.compaction`；压缩了返回 True（调用方重新组装前缀）。

    `summarize(messages, deadline)` 发一次与主对话参数相同的请求，返回模型响应；为 None 时只生成事实要点。
    """

    last = max((message.created_at for group in groups for message in [group.user, *group.assistants]), default=None)
    idle = (utcnow() - last) if last is not None else None
    reason = trigger_reason(groups, history_tokens=history_tokens, loaded=loaded, idle=idle,
                            identity_changed=identity_changed)
    if reason is None:
        return False
    previous = dict(conversation.compaction or {})
    kept = keep_from(groups)
    dropped = groups[: len(groups) - len(kept)]
    if not dropped:
        return False
    started = time.monotonic()
    requirements, mode = previous.get("requirements") or "", "facts_only"
    failures = int(previous.get("failures") or 0)
    if summarize is not None:
        summary = _summarize(summarize, prefix_messages)
        if summary is None:
            failures += 1
            if failures < MAX_FAILURES:
                conversation.compaction = {**previous, "failures": failures}
                flag_modified(conversation, "compaction")
                _log(mode="deferred", reason=reason, dropped=len(dropped), started=started)
                return False
        else:
            requirements, mode = summary, "summary"
    conversation.compaction = {
        "version": 1,
        "start_id": kept[0].user.id,
        "facts": merge_facts(previous.get("facts") or [], facts_from(dropped)),
        "requirements": requirements,
        "tools": still_used(dropped, kept, list(previous.get("tools") or [])),
        "facts_only": mode == "facts_only",
        "model": model_name if mode == "summary" else previous.get("model"),
        "reason": reason,
        "created_at": utcnow().isoformat(),
        "failures": 0,
    }
    flag_modified(conversation, "compaction")
    _log(mode=mode, reason=reason, dropped=len(dropped), started=started)
    return True


def _summarize(summarize, prefix_messages: list[dict]) -> str | None:
    messages = [*prefix_messages, {"role": "user", "content": SUMMARY_INSTRUCTION}]
    try:
        response = summarize(messages, time.monotonic() + SUMMARY_SECONDS)
    except Exception as exc:  # 摘要失败只是这轮不压缩，不能让老师这句话报错。
        logger.warning("assistant_compaction_summary_failed error=%s", type(exc).__name__)
        return None
    calls = list(getattr(response, "tool_calls", ()) or ())
    if len(calls) != 1 or calls[0].name != "reply":
        return None
    try:
        reply = validate_reply(calls[0].arguments, TurnRefs(), allow_cards=False)
    except ReplyInvalid:
        return None
    text = reply.text.strip()
    return "" if text in {"无", "无。"} else text


def _log(*, mode: str, reason: str, dropped: int, started: float) -> None:
    logger.info("assistant_compaction mode=%s reason=%s dropped_turns=%d latency_ms=%d",
                mode, reason, dropped, int((time.monotonic() - started) * 1000))


__all__ = [
    "KEEP_TURNS", "SUMMARY_INSTRUCTION", "TRIGGER_TURNS", "facts_from", "keep_from", "maybe_compact",
    "merge_facts", "still_used", "trigger_reason",
]
