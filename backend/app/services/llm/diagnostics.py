"""LLM 连通自检：供前端"测试连接"按钮 / 上线前排查用。

mock 直接返回 ok（不发请求）；真实 provider 发一个极小请求测连通+延迟，失败时返回清晰错误（阶段/原因）。
不抛异常，永远返回结构化结果。
"""

from time import perf_counter

from backend.app.services.llm.factory import get_llm_scorer

PING_INSTRUCTIONS = "你是连通自检助手。只返回一个 JSON 对象，不要任何多余文字。"
PING_PAYLOAD = {"task": "ping", "instruction": '请返回 {"ok": true}'}


def check_connectivity():
    try:
        scorer = get_llm_scorer()
    except Exception as exc:
        return {"ok": False, "stage": "config", "error": _short(exc)}

    provider = getattr(scorer, "provider", "")
    if provider == "mock":
        return {
            "ok": True,
            "stage": "mock",
            "provider": "mock",
            "note": "当前为 Mock 评分器，未发起真实请求；配置真实 LLM（LLM_PROVIDER + API Key）后再测连通。",
        }

    started = perf_counter()
    try:
        result = scorer.complete_json(PING_INSTRUCTIONS, PING_PAYLOAD)
        return {
            "ok": True,
            "stage": "model",
            "provider": provider,
            "model": getattr(scorer, "model_name", ""),
            "latency_ms": round((perf_counter() - started) * 1000),
            "sample": result if isinstance(result, dict) else None,
        }
    except NotImplementedError:
        return {"ok": False, "stage": "capability", "provider": provider, "error": "该 adapter 未实现 complete_json"}
    except Exception as exc:
        return {
            "ok": False,
            "stage": "model",
            "provider": provider,
            "model": getattr(scorer, "model_name", ""),
            "latency_ms": round((perf_counter() - started) * 1000),
            "error": _short(exc),
        }


def _short(exc):
    return " ".join(str(exc).split())[:300] or exc.__class__.__name__
