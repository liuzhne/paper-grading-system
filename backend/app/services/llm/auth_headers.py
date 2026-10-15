"""OpenAI 协议（Chat Completions / Responses）请求的认证头。

多数 OpenAI 兼容服务按惯例用 ``Authorization: Bearer <key>``。Google Vertex AI 的 OpenAI 兼容
端点（``https://aiplatform.googleapis.com/v1/projects/…/endpoints/openapi``）不同：Bearer 位置
只接受 OAuth 访问令牌，API Key（express 模式，``AQ.`` 开头）必须放在 ``x-goog-api-key`` 头里，
用 Bearer 发会被拒（401）。2026-10-15 平台模型改用 Vertex 时遇到。

只按主机判断，不看 Key 的样子：同一个主机上，Key 的格式变了也还是要走 ``x-goog-api-key``。
Claude（Anthropic Messages）的 ``x-api-key`` 不经过这里。
"""

from __future__ import annotations

from urllib.parse import urlsplit


_VERTEX_HOST = "aiplatform.googleapis.com"


def is_vertex_host(base_url) -> bool:
    """全局端点 ``aiplatform.googleapis.com`` 与区域端点 ``<区域>-aiplatform.googleapis.com``。"""

    host = (urlsplit(str(base_url or "")).hostname or "").rstrip(".").casefold()
    return host == _VERTEX_HOST or host.endswith("-" + _VERTEX_HOST)


def openai_auth_headers(base_url, api_key) -> dict[str, str]:
    if is_vertex_host(base_url):
        return {"x-goog-api-key": api_key}
    return {"Authorization": "Bearer %s" % api_key}


__all__ = ["is_vertex_host", "openai_auth_headers"]
