import json
import sys

import httpx

from backend.app.core.config import settings
from backend.app.services.llm.auth_headers import openai_auth_headers
from backend.app.services.llm.thinking_params import effective_thinking_type
from backend.app.services.llm.thinking_params import thinking_request_fields


def main():
    if not settings.OPENAI_COMPATIBLE_API_KEY:
        print_json({"ok": False, "stage": "config", "error": "OPENAI_COMPATIBLE_API_KEY is not configured"})
        return 1

    url = "%s/chat/completions" % settings.OPENAI_COMPATIBLE_BASE_URL.rstrip("/")
    payload = {
        "model": settings.OPENAI_COMPATIBLE_MODEL,
        "messages": [
            {"role": "system", "content": "只返回一个 JSON 对象，不要返回 Markdown。"},
            {"role": "user", "content": '请严格返回 {"ok": true, "message": "pong"}'},
        ],
        "temperature": 0.2,
        "max_tokens": 200,
    }
    # 与评分适配器同一套换算：Google 主机关闭思考发 reasoning_effort，其余发 thinking 字段。
    thinking_type = effective_thinking_type(
        url, settings.OPENAI_COMPATIBLE_MODEL, settings.OPENAI_COMPATIBLE_THINKING_TYPE,
    )
    payload.update(thinking_request_fields(url, settings.OPENAI_COMPATIBLE_MODEL, thinking_type))
    if settings.OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON:
        payload["response_format"] = {"type": "json_object"}

    try:
        response = httpx.post(
            url,
            headers={
                **openai_auth_headers(url, settings.OPENAI_COMPATIBLE_API_KEY),
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=settings.OPENAI_COMPATIBLE_TIMEOUT_SECONDS,
        )
    except httpx.TransportError as exc:
        print_json(
            {
                "ok": False,
                "stage": "transport",
                "url": url,
                "model": settings.OPENAI_COMPATIBLE_MODEL,
                "thinking": payload.get("thinking"),
                "reasoning_effort": payload.get("reasoning_effort"),
                "response_format": payload.get("response_format"),
                "error_type": exc.__class__.__name__,
                "error": str(exc),
                "hint": "请求未到达可解析的 HTTP 响应阶段，请优先检查代理、DNS、TLS 拦截或网络出口。",
            }
        )
        return 2

    body_preview = response.text[:1200]
    result = {
        "ok": response.is_success,
        "stage": "http",
        "url": url,
        "model": settings.OPENAI_COMPATIBLE_MODEL,
        "thinking": payload.get("thinking"),
        # Google 主机关闭思考发的是 reasoning_effort（thinking 为 null），两者都报出来便于核对。
        "reasoning_effort": payload.get("reasoning_effort"),
        "response_format": payload.get("response_format"),
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type"),
        "body_preview": body_preview,
    }
    try:
        data = response.json()
    except ValueError:
        print_json(result)
        return 3

    choices = data.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    content = message.get("content")
    reasoning = message.get("reasoning_content")
    result.update(
        {
            "choices_len": len(choices),
            "finish_reason": choice.get("finish_reason"),
            "choice_keys": sorted(choice.keys()),
            "message_keys": sorted(message.keys()),
            "content_chars": len(content or "") if isinstance(content, str) else None,
            "content_preview": str(content or "")[:500],
            "reasoning_content_chars": len(reasoning or "") if isinstance(reasoning, str) else None,
        }
    )
    print_json(result)
    return 0 if response.is_success and bool(content) else 4


def print_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
