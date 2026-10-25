"""一次 Agent 回合（Agent 化方案 §10、§13）。

LangGraph 子图：模型节点 ↔ 工具节点，不用预制的 ReAct 智能体，也不用 LangChain 的模型封装——模型调用
一律走现有适配器（连接、限流、熔断、计量都在那里）。每回合无状态：本回合的工具调用与结果（⑥）只在
内存里，回合结束即丢弃；下一轮的历史只放用户原话、加载记录与回复文字。

结束方式：
- 调用 `reply`：校验通过后显示文字与卡片（不合格可修正一次，仍不合格给确定性兜底）；
- 调用一个提议工具：生成确认卡片后立即结束（A1 起才有提议工具）；
- 超过上限（8 次工具调用、其中 2 次搜索、60 秒）：确定性兜底回复与快捷按钮；
- 模型出错（超时、限流、熔断、无法解析）：本回合退回规则模式（降级）。

原生工具调用走 `complete_with_tools`；没有它的适配器退回 JSON 动作协议（每一步用 `complete_json`
返回 `{"calls": [...]}`）。Mock 提供方在运行器里就被当作“模型不可用”，不会走到这里。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Callable
from typing import TypedDict

from fastapi import HTTPException
from langgraph.graph import END
from langgraph.graph import START
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError
from sqlalchemy import select

from backend.app.db.models import AssistantMessage
from backend.app.services.assistant.agent import budget
from backend.app.services.assistant.agent import catalog
from backend.app.services.assistant.agent import compaction
from backend.app.services.assistant.agent.context import Prefix
from backend.app.services.assistant.agent.context import build_prefix
from backend.app.services.assistant.agent.context import state_message
from backend.app.services.assistant.agent.context import turn_groups
from backend.app.services.assistant.agent.refs import ToolError
from backend.app.services.assistant.agent.refs import TurnContext
from backend.app.services.assistant.agent.reply import ReplyInvalid
from backend.app.services.assistant.agent.reply import ValidReply
from backend.app.services.assistant.agent.reply import validate_reply
from backend.app.services.assistant.context import RunContext
from backend.app.services.llm.errors import ProviderJSONOutputError
from backend.app.services.llm.tool_calls import ToolCall
from backend.app.services.llm.tool_calls import ToolResponse
from backend.app.services.llm.tool_calls import ToolResponseError

logger = logging.getLogger(__name__)

FALLBACK_TEXT = "这个问题我一时没处理好。可以换个说法，或者点下面的按钮。"
RECURSION_LIMIT = 40

JSON_PROTOCOL = (
    "你所在的连接不支持原生工具调用，改用 JSON 动作协议：每一步只输出一个 JSON 对象，形如 "
    "{\"calls\": [{\"name\": \"工具名\", \"arguments\": {参数}}]}，不要输出其它内容。"
    "messages 里 role 为 tool 的条目是之前各步的工具结果。结束本回合时调用 reply。"
)

ModelStep = Callable[[list[dict], list[dict], float], ToolResponse]


@dataclass
class TurnOutcome:
    kind: str  # reply | proposal | fallback | degrade
    reply: ValidReply | None = None
    proposal: dict | None = None
    reason: str | None = None
    meta: dict = field(default_factory=dict)
    refs: Any = None


# --- 模型的一步 ---------------------------------------------------------------------

def model_step(scorer) -> tuple[ModelStep | None, str | None]:
    """按适配器的能力选协议：原生工具调用优先，否则 JSON 动作协议（§13）。"""

    if callable(getattr(scorer, "complete_with_tools", None)):
        def native(messages, tools, deadline):
            return scorer.complete_with_tools(
                messages, tools, tool_choice="required", max_tokens=budget.STEP_MAX_TOKENS,
                deadline=deadline, timeout_seconds=budget.STEP_SECONDS,
                rate_limit_retries=budget.RATE_LIMIT_RETRIES, rate_limit_delay_seconds=budget.RATE_LIMIT_RETRY_SECONDS,
                rate_limit_min_call_seconds=budget.RATE_LIMIT_MIN_CALL_SECONDS,
            )
        return native, "native"
    if callable(getattr(scorer, "complete_json", None)):
        return _json_protocol_step(scorer), "json"
    return None, None


def _json_protocol_step(scorer) -> ModelStep:
    def step(messages, tools, deadline):
        instructions = "\n\n".join([
            messages[0]["content"],
            "可用的工具：\n" + catalog.render([tool["function"] for tool in tools]),
            JSON_PROTOCOL,
        ])
        payload = {"messages": messages[1:]}
        try:
            raw = scorer.complete_json(instructions, payload, attempts_limit=1, deadline=deadline,
                                       default_timeout_seconds=budget.STEP_SECONDS)
        except TypeError:  # 只接受 (instructions, payload) 的适配器
            raw = scorer.complete_json(instructions, payload)
        items = raw.get("calls") if isinstance(raw, dict) else None
        if not isinstance(items, list) or not items:
            raise ToolResponseError("invalid_json_actions")
        calls = []
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise ToolResponseError("invalid_json_actions")
            arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            calls.append(ToolCall(id="json-%d" % index, name=item["name"], arguments=catalog.render(arguments)))
        message = {"role": "assistant", "content": catalog.render(raw)}
        return ToolResponse(content="", tool_calls=tuple(calls), message=message, finish_reason=None, usage={})
    return step


# --- 子图 -------------------------------------------------------------------------

class _State(TypedDict, total=False):
    steps: list
    pending: list
    synthetic: bool
    outcome: dict
    calls: int
    searches: int
    reply_retries: int


@dataclass
class _Runtime:
    turn: TurnContext
    step: ModelStep
    prefix: Prefix
    state_message: dict
    started: float
    loaded: list[str]
    first_loaded: list[str] = field(default_factory=list)
    selected: list[str] = field(default_factory=list)
    called: list[str] = field(default_factory=list)
    param_errors: dict[str, int] = field(default_factory=dict)
    model_calls: int = 0
    #: 评测用的观测：模型没调用工具、直接给文字的次数；工具调用上带思考签名的个数（§12.2）。
    text_replies: int = 0
    thought_signatures: int = 0
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0})


def _add_usage(target: dict, usage: dict) -> None:
    for key in target:
        value = (usage or {}).get(key)
        if isinstance(value, int):
            target[key] += value


def _model_node(state: _State, runtime: Runtime[_Runtime]):
    rt = runtime.context
    now = time.monotonic()
    if now - rt.started > budget.TURN_SECONDS:
        return {"outcome": {"kind": "fallback", "reason": "turn_timeout"}}
    deadline = min(rt.started + budget.TURN_SECONDS, now + budget.STEP_SECONDS)
    messages = [*rt.prefix.messages, rt.state_message, *state.get("steps", [])]
    try:
        response = rt.step(messages, rt.prefix.tools, deadline)
    except Exception as exc:  # 超时、限流、熔断、无法解析：本回合退回规则模式
        code = _error_code(exc)
        logger.warning("assistant_agent_model_failed error=%s code=%s", type(exc).__name__, code or "-")
        return {"outcome": {"kind": "degrade", "reason": type(exc).__name__, "error_code": code}}
    rt.model_calls += 1
    _add_usage(rt.usage, response.usage)
    rt.thought_signatures += sum(
        1 for item in response.message.get("tool_calls") or []
        if isinstance(item, dict) and ((item.get("extra_content") or {}).get("google") or {}).get("thought_signature")
    )
    calls = list(response.tool_calls)
    synthetic = False
    if not calls:
        # 模型没调用工具、直接给了文字：当作 reply 走同一套校验（§13）。
        calls = [ToolCall(id="text", name="reply", arguments=catalog.render({"text": response.content}))]
        synthetic = True
        rt.text_replies += 1
    return {"steps": [*state.get("steps", []), response.message], "pending": calls, "synthetic": synthetic}


def _error_code(exc) -> str | None:
    """模型调用失败的错误码（`rate_limited`、`PROVIDER_CIRCUIT_OPEN` 等），只记码不记正文。

    异常类名分不出“被限流”和“请求本身不合格”：A1 第一次评测 37 条都记成 ProviderCallError，
    要翻实例日志才知道是 Vertex 的 429。
    """

    error = getattr(exc, "error", None)
    code = getattr(error, "code", None) if error is not None else getattr(exc, "code", None)
    code = getattr(code, "value", code)
    if not code and isinstance(exc, ProviderJSONOutputError):
        # 模型答了但不能用（空响应、截断、工具调用格式不对）：A1 第二轮有一条只记成 ToolResponseError。
        code = "output_%s" % exc.reason
    return str(code) if isinstance(code, str) and code else None


def _arguments(raw) -> dict | None:
    """模型给的参数：必须是 JSON 对象。合法 JSON 但不是对象（null、数组、数字）也按不合格处理，
    不能让 `.get` 在图里抛异常、变成老师看到的 500。"""

    try:
        value = json.loads(raw or "{}") if isinstance(raw, str) else raw
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _tool_message(call: ToolCall, content) -> dict:
    return {"role": "tool", "tool_call_id": call.id, "content": catalog.render(content)}


def _tools_node(state: _State, runtime: Runtime[_Runtime]):
    rt = runtime.context
    steps = list(state.get("steps", []))
    calls = int(state.get("calls") or 0)
    searches = int(state.get("searches") or 0)
    retries = int(state.get("reply_retries") or 0)
    for call in state.get("pending") or []:
        calls += 1
        if calls > budget.MAX_TOOL_CALLS:
            return {"outcome": {"kind": "fallback", "reason": "too_many_tool_calls"}}
        if call.name == "reply":
            try:
                reply = validate_reply(call.arguments, rt.turn.refs)
            except ReplyInvalid as exc:
                if retries >= 1 or state.get("synthetic"):
                    return {"outcome": {"kind": "fallback", "reason": "reply_invalid"}}
                retries += 1
                steps.append(_tool_message(call, {"错误": exc.reason, "提示": "请修正后重新调用 reply"}))
                continue
            return {"outcome": {"kind": "reply", "reply": reply}}
        if call.name == "tool_search":
            searches += 1
            content = _search(call, rt) if searches <= budget.MAX_SEARCHES else {
                "错误": "本回合搜索次数已用完：请用已加载的工具回答，或调用 reply 告诉老师现在做不到。"}
            steps.append(_tool_message(call, content))
            continue
        content, proposal = _call(call, rt)
        if proposal is not None:
            return {"outcome": {"kind": "proposal", "proposal": proposal}}
        steps.append(_tool_message(call, content))
    return {"steps": steps, "pending": [], "calls": calls, "searches": searches, "reply_retries": retries}


def _search(call: ToolCall, rt: _Runtime) -> dict:
    arguments = _arguments(call.arguments)
    if arguments is None:
        return {"错误": "tool_search 的参数必须是 JSON 对象，例如 {\"query\": \"评分标准\"}"}
    query = str(arguments.get("query") or "").strip()
    limit = arguments.get("limit") if isinstance(arguments.get("limit"), int) else catalog.MAX_SEARCH_RESULTS
    result: dict[str, Any] = {}
    if query.lower().startswith("select:"):
        names = query.split(":", 1)[1].split(",")
        found, missing = catalog.select(names, rt.turn.role)
        resident = [name.strip() for name in missing if name.strip() in {tool.name for tool in catalog.resident_business()}]
        missing = [name.strip() for name in missing if name.strip() not in resident]
        if resident:
            result["常驻"] = resident
            result["提示"] = "常驻工具不用加载，直接调用。"
        if missing:
            result["找不到"] = missing
        rt.selected.extend(tool.name for tool in found if tool.name not in rt.selected)
    else:
        found = catalog.search(query, rt.turn.role, limit=limit)
    for tool in found:
        if tool.name not in rt.loaded:
            rt.loaded.append(tool.name)
            rt.first_loaded.append(tool.name)
    result["tools"] = [catalog.definition(tool) for tool in found]
    if not found:
        result["提示"] = result.get("提示") or "没有找到相关工具：可以换个说法再搜，或调用 reply 告诉老师现在做不到。"
        result["分组"] = {key: label for key, label in catalog.GROUPS.items()}
    return result


def _call(call: ToolCall, rt: _Runtime) -> tuple[dict, dict | None]:
    tools = {tool.name: tool for tool in catalog.visible(rt.turn.role)}
    if call.name == "call_tool":
        arguments = _arguments(call.arguments)
        if arguments is None:
            return {"错误": "call_tool 的参数必须是 JSON 对象：{\"name\": 工具名, \"arguments_json\": 参数}"}, None
        name = str(arguments.get("name") or "").strip()
        # 常驻工具经 call_tool 调用也照常执行：工具改为常驻之前的对话历史里，加载记录与调用方式还是延迟工具的。
        if name not in tools or not (tools[name].resident or name in rt.loaded):
            return {"错误": "工具“%s”还没有加载：先用 tool_search 搜索，或用 select:名称 加载。" % name}, None
        return _run(tools[name], arguments.get("arguments_json") or "{}", rt)
    tool = tools.get(call.name)
    if tool is None or not tool.resident:
        return {"错误": "没有工具“%s”：常驻工具直接调用，其它工具经 call_tool 调用。" % call.name}, None
    return _run(tool, call.arguments, rt)


def _run(tool, arguments_json, rt: _Runtime) -> tuple[dict, dict | None]:
    if rt.param_errors.get(tool.name, 0) >= 2:
        return {"错误": "这个工具的参数已经连续不合格，本回合不再调用它。"}, None
    try:
        raw = json.loads(arguments_json) if isinstance(arguments_json, str) else dict(arguments_json or {})
        if not isinstance(raw, dict):
            raise ValueError
        params = tool.params.model_validate(raw)
    except (ValueError, TypeError, ValidationError) as exc:
        rt.param_errors[tool.name] = rt.param_errors.get(tool.name, 0) + 1
        detail = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else "参数不是合法的 JSON 对象"
        return {"错误": "参数不合格：%s" % detail, "参数": catalog.parameters_schema(tool.params, tool.param_notes)}, None
    try:
        result = tool.run(rt.turn, params)
    except ToolError as exc:
        return {"错误": str(exc)}, None
    except HTTPException:
        # 守卫拒绝（不存在或无权访问）：不回显内部信息。
        return {"错误": "找不到这个对象，或当前账号无权访问。"}, None
    if tool.name not in rt.called:
        rt.called.append(tool.name)
    if tool.kind == "proposal" and isinstance(result, dict) and result.get("proposal"):
        card_type = (result["proposal"].get("card") or {}).get("type")
        if card_type:
            rt.turn.refs.proposed.add(card_type)
        return {}, result["proposal"]  # 确认卡片或进入流程：本回合到此结束
    # 回复里的状态与卡片说法原样出现在工具结果里，也算工具返回过（claims）。
    rt.turn.refs.seen_text.append(catalog.render(result))
    return result, None


def _after_model(state: _State):
    return END if state.get("outcome") else "tools"


def _after_tools(state: _State):
    return END if state.get("outcome") else "model"


def _build_graph():
    builder = StateGraph(_State, context_schema=_Runtime)
    builder.add_node("model", _model_node)
    builder.add_node("tools", _tools_node)
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", _after_model, ["tools", END])
    builder.add_conditional_edges("tools", _after_tools, ["model", END])
    return builder.compile()


_GRAPH = _build_graph()


# --- 入口 -------------------------------------------------------------------------

def _messages(ctx: RunContext) -> list[AssistantMessage]:
    return list(ctx.db.scalars(
        select(AssistantMessage)
        .where(AssistantMessage.conversation_id == ctx.conversation.id)
        .order_by(AssistantMessage.created_at, AssistantMessage.id)
    ).all())


def _last_identity(messages: list[AssistantMessage]) -> str | None:
    for message in reversed(messages):
        identity = (message.meta or {}).get("prefix_identity")
        if identity:
            return identity
    return None


def run_turn(ctx: RunContext, scorer, *, text: str, user_message_id: str) -> TurnOutcome:
    step, protocol = model_step(scorer)
    if step is None:
        return TurnOutcome(kind="degrade", reason="no_model_step")
    started = time.monotonic()
    model_name = getattr(scorer, "model_name", None)
    conversation = ctx.conversation
    messages = _messages(ctx)

    prefix = build_prefix(messages, compaction=conversation.compaction, exclude_id=user_message_id, model_name=model_name)
    groups = turn_groups(messages, exclude_id=user_message_id, start_id=(conversation.compaction or {}).get("start_id"))
    last_identity = _last_identity(messages)
    if compaction.maybe_compact(
        conversation, groups, prefix_messages=prefix.messages, history_tokens=prefix.history_tokens,
        loaded=len(prefix.loaded), identity_changed=bool(last_identity) and last_identity != prefix.identity,
        summarize=lambda request, deadline: step(request, prefix.tools, deadline), model_name=model_name,
    ):
        prefix = build_prefix(messages, compaction=conversation.compaction, exclude_id=user_message_id,
                              model_name=model_name)

    turn = TurnContext(run=ctx)
    focus = dict(conversation.focus or {})
    if focus.get("batch_id"):
        turn.refs.pin("t0", "t", focus["batch_id"])
    if focus.get("paper_id"):
        turn.refs.pin("p0", "p", focus["paper_id"])
    runtime = _Runtime(
        turn=turn, step=step, prefix=prefix, started=started, loaded=list(prefix.loaded),
        state_message=state_message(text, focus=focus, pending=conversation.pending, loaded=prefix.loaded),
    )
    final = _GRAPH.invoke({"steps": []}, context=runtime, config={"recursion_limit": RECURSION_LIMIT})
    raw = final.get("outcome") or {"kind": "fallback", "reason": "no_outcome"}
    tools_loaded = [name for name in runtime.first_loaded if name in runtime.called or name in runtime.selected]
    meta = {
        "mode": "agent", "protocol": protocol, "outcome": raw["kind"],
        "tools_loaded": tools_loaded, "tools_called": list(runtime.called),
        "prefix_identity": prefix.identity, "model_calls": runtime.model_calls,
        "searches": int(final.get("searches") or 0), "tokens": dict(runtime.usage),
        "text_replies": runtime.text_replies, "thought_signatures": runtime.thought_signatures,
        "param_errors": sum(runtime.param_errors.values()),
        "help_refs": list(raw["reply"].help_refs) if raw.get("reply") is not None else [],
    }
    if raw.get("reason"):
        meta["reason"] = raw["reason"]
    if raw.get("error_code"):
        meta["error_code"] = raw["error_code"]
    logger.info(
        "assistant_turn mode=agent protocol=%s outcome=%s steps=%d tools=%s searches=%d loaded=%s "
        "tokens_in=%d tokens_cache_read=%d tokens_out=%d latency_ms=%d",
        protocol, raw["kind"], runtime.model_calls, ",".join(runtime.called) or "-", meta["searches"],
        ",".join(tools_loaded) or "-", runtime.usage["prompt_tokens"], runtime.usage["cached_tokens"],
        runtime.usage["completion_tokens"], int((time.monotonic() - started) * 1000),
    )
    return TurnOutcome(kind=raw["kind"], reply=raw.get("reply"), proposal=raw.get("proposal"),
                       reason=raw.get("reason"), meta=meta, refs=turn.refs)


def cards_for(refs, reply: ValidReply) -> list[dict]:
    """回复里的引用编号 → 数据卡片（分数由卡片现取）。没有卡片的对象（评分标准、评分项）不显示。"""

    cards = []
    for ref in reply.cards:
        card = refs.card_of(ref)
        if card is None:
            found = refs.object_of(ref)
            card = {"type": "overview", "batch_id": found[1]} if found and found[0] == "t" else None
        if card and card not in cards:
            cards.append(card)
    return cards


def help_texts(reply: ValidReply) -> list[str]:
    """回复引用的帮助条目 → 固定文案（§14：帮助以条目原文为准，模型的 text 只做引导）。"""

    from backend.app.services.assistant.agent import help as help_entries

    return [help_entries.BY_KEY[key].text for key in reply.help_refs if key in help_entries.BY_KEY]


__all__ = ["FALLBACK_TEXT", "TurnOutcome", "cards_for", "help_texts", "model_step", "run_turn"]
