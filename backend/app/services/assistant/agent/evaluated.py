"""评测达标过的助手模型（Agent 化方案 §12.5，维护者 2026-10-26 定）。

线上 Agent 用的是老师自己的模型：配了连接用连接，没配就回落到平台模型。A1 只在 Vertex 上的
Gemini 2.5 Flash 上评测达标；平台模型（当时配的是 Bedrock 上的 Claude Opus 5）从没调用过、很可能调不通。
所以 Agent 回合只对这里登记过的模型开放，其它模型照旧走规则模式，不去调一个没评测过的模型。

往这里加模型之前，先在当前提示词上用它跑一轮评测、提交达标的报告：部署门槛（`assistant_eval_gate`）
要求这里的每个模型都有一轮在当前提示词指纹上达标的评测，缺一个就不部署。
"""

from __future__ import annotations

from urllib.parse import urlsplit

from backend.app.services.llm.thinking_params import is_google_openai_host

#: (端点家族, 模型) 。端点家族：Google（Vertex 与 Gemini API）记 "google"，其它按主机名。
EVALUATED_MODELS = frozenset({("google", "gemini-2.5-flash")})


def model_id(model_name) -> str:
    """`google/gemini-2.5-flash`、`models/gemini-2.5-flash` → `gemini-2.5-flash`。"""

    return str(model_name or "").strip().casefold().rsplit("/", 1)[-1]


def family(base_url=None, *, host: str | None = None) -> str:
    url = base_url if base_url else ("https://%s/" % host if host else "")
    if is_google_openai_host(url):
        return "google"
    return (urlsplit(str(url or "")).hostname or "").casefold()


def is_evaluated(model_name, base_url) -> bool:
    return (family(base_url), model_id(model_name)) in EVALUATED_MODELS


__all__ = ["EVALUATED_MODELS", "family", "is_evaluated", "model_id"]
