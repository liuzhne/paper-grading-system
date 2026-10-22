"""原生工具调用的响应（评分助手 Agent 回合用，Agent 化方案 §13）。

适配器把厂商的响应换成这里的结构：模型这一步说了什么、要调用哪些工具，以及一条可以
原样放回下一次请求的助手消息。**放回的消息不改写、不重排**（§12.1 ⑥）：Gemini 在工具调用上
带的思考签名（``extra_content.google.thought_signature``）后续各步必须原样带回。

这里只放结构与解析，不含任何提示词，也不记录内容。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field

from backend.app.services.llm.errors import ProviderJSONOutputError


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    #: 模型写的参数（JSON 字符串，未解析）：解析与校验由调用方按工具的 schema 做。
    arguments: str


@dataclass(frozen=True)
class ToolResponse:
    content: str
    tool_calls: tuple[ToolCall, ...]
    #: 原样放回下一次请求的助手消息。
    message: dict
    finish_reason: str | None
    usage: dict = field(default_factory=dict)


class ToolResponseError(ProviderJSONOutputError):
    """Safe metadata only; never include provider content in this exception."""

    def __init__(self, reason):
        super().__init__(reason, "tool-call response: " + reason)


def _text(content) -> str:
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content or "")


def usage_from_chat(data: dict) -> dict:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    prompt_details = usage.get("prompt_tokens_details")
    cached = prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None
    # 缓存命中量的字段名各家不一（方案 §12.2）：Chat Completions 的标准位置之外，也认顶层的
    # cached_tokens / cached_content_token_count（OpenAI 兼容端点映射 Gemini 时可能用到）。
    if cached is None:
        cached = usage.get("cached_tokens", usage.get("cached_content_token_count"))
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cached_tokens": cached,
    }


def parse_chat_tool_response(data) -> ToolResponse:
    """解析 Chat Completions 的响应。没有工具调用也没有文字视为不合格。"""

    if not isinstance(data, dict):
        raise ToolResponseError("invalid_envelope")
    if data.get("error"):
        raise ToolResponseError("error_envelope")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ToolResponseError("missing_choices")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ToolResponseError("missing_message")
    calls = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        if not isinstance(raw, dict):
            raise ToolResponseError("invalid_tool_call")
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ToolResponseError("invalid_tool_call")
        arguments = function.get("arguments")
        if isinstance(arguments, dict):  # 个别兼容端点直接给对象
            arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        calls.append(ToolCall(id=str(raw.get("id") or "call_%d" % index), name=name, arguments=str(arguments or "{}")))
    content = _text(message.get("content"))
    if not calls and not content.strip():
        reason = "output_truncated" if choice.get("finish_reason") == "length" else "empty_content"
        raise ToolResponseError(reason)
    replay = {key: value for key, value in message.items() if value is not None}
    replay["role"] = "assistant"
    return ToolResponse(
        content=content,
        tool_calls=tuple(calls),
        message=replay,
        finish_reason=choice.get("finish_reason"),
        usage=usage_from_chat(data),
    )


__all__ = ["ToolCall", "ToolResponse", "ToolResponseError", "parse_chat_tool_response", "usage_from_chat"]
