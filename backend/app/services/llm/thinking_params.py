"""OpenAI 兼容请求里“思考”设置的写法，按主机区分。

连接的 ``thinking_type`` 沿用智谱的写法 ``thinking: {"type": "disabled"}``，多数兼容服务认它。
Google（Vertex AI 与 Gemini API 的 OpenAI 兼容端点）不认这个字段，会静默忽略：Gemini 2.5 Flash
于是按默认开着思考，思考 token 计入输出上限。2026-10-16 生产上一份论文 48 次评分调用里思考
约 3.7 万 token、可见输出只有 3 千多，12 次撞上 1200 的输出上限被截断，整份失败。

关闭思考的写法两家不同，只有 2.5 Flash 系列能关（2.5 Pro、Gemini 3 关不掉）：

- Gemini API（``generativelanguage.googleapis.com``）：``reasoning_effort: "none"``；
- Vertex AI：``reasoning_effort`` 只收 ``minimal / low / medium / high / max``，发 ``"none"`` 会 400
  （2026-10-20 生产 48 次请求全被拒）。改用 Vertex 文档给的 Gemini 专属参数
  ``extra_body.google.thinking_config.thinking_budget = 0``——原始 HTTP 请求体里就是顶层的
  ``"extra_body"`` 键。两种写法不混用（Google 文档：二者只能指定其一）。

关不掉的模型不发参数，运行身份记为 ``provider-default``（思考开启），如实反映实际行为。

连接**没配置**思考时，Google 主机上同样按「关闭」处理（2026-10-20）。BYOK 连接没配时工厂传空字符串，
本意是不给只协议兼容的服务发非标准的 ``thinking`` 字段；Google 主机上发的是它自己文档里的参数，
这层顾虑不成立。之前没配置按模型默认（开启）处理，2026-10-19 生产重试那份论文时 48 次调用全在思考
（Vertex 报告推理 46,463 token），5 次撞上 2048 的输出上限，整份又失败。

运行身份（决策账本、L0 缓存都按它区分）记的是这里换算后的值，所以改了写法，旧判定不会被
当作同一请求复用。
"""

from __future__ import annotations

from urllib.parse import urlsplit

from backend.app.services.llm.auth_headers import is_vertex_host


_GEMINI_API_HOST = "generativelanguage.googleapis.com"
_DISABLED = frozenset({"disabled", "none", "false", "off"})
# Google 主机上关不掉思考、或没配置思考时的身份值：思考按模型默认开启。
GOOGLE_PROVIDER_DEFAULT = "provider-default"


def is_google_openai_host(base_url) -> bool:
    """Vertex AI（全局与区域端点）或 Gemini API 的 OpenAI 兼容端点。"""

    if is_vertex_host(base_url):
        return True
    host = (urlsplit(str(base_url or "")).hostname or "").rstrip(".").casefold()
    return host == _GEMINI_API_HOST


def _model_id(model_name) -> str:
    name = str(model_name or "").strip().casefold()
    return name.rsplit("/", 1)[-1]  # google/gemini-2.5-flash、models/gemini-2.5-flash


def google_can_disable_thinking(model_name) -> bool:
    """只有 Gemini 2.5 Flash 系列（含 Flash-Lite 与预览版）能用 ``reasoning_effort: none`` 关闭思考。"""

    return _model_id(model_name).startswith("gemini-2.5-flash")


def effective_thinking_type(base_url, model_name, thinking_type):
    """连接配置的思考设置 → 实际发出的写法对应的身份值；非 Google 主机原样返回。

    Google 主机上没配置（空值）按关闭处理。
    """

    if not is_google_openai_host(base_url):
        return thinking_type
    value = str(thinking_type or "").strip()
    if not value or value.casefold() in _DISABLED:
        # 没配置与显式关闭一样：能关的发关闭参数，关不掉的如实记按模型默认。
        return "none" if google_can_disable_thinking(model_name) else GOOGLE_PROVIDER_DEFAULT
    return value


def thinking_request_fields(base_url, model_name, thinking_type) -> dict:
    """要合并进请求体的思考字段（可能为空）。"""

    value = str(thinking_type or "").strip()
    if not value:
        return {}
    if is_google_openai_host(base_url):
        if value.casefold() in _DISABLED and google_can_disable_thinking(model_name):
            if is_vertex_host(base_url):
                return {"extra_body": {"google": {"thinking_config": {"thinking_budget": 0}}}}
            return {"reasoning_effort": "none"}
        return {}
    return {"thinking": {"type": value}}


__all__ = [
    "GOOGLE_PROVIDER_DEFAULT",
    "effective_thinking_type",
    "google_can_disable_thinking",
    "is_google_openai_host",
    "thinking_request_fields",
]
