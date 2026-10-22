"""评分助手 Agent 的评测（Agent 化方案 §27）。

每条评测说法在一个**隔离的内存数据库**里跑：只有合成的种子数据（评分标准、几个评分任务），不读写任何
业务数据，报告里没有学生信息。模型由调用方给：脚本在生产实例上用平台模型（凭据不离开服务器），测试里用
脚本化的假模型。

逐条判定（全部满足才算通过）：结束方式、必须调用 / 不许调用的工具、要不要搜索、卡片类型、写动作（任务、
评分作业、已发布标准的数量不变）、系统指令泄露、等待中的步骤不变。另统计上线门槛、Gemini 的四项行为
（`tool_choice: required` 是否生效、函数声明是否被接受、思考签名、缓存命中量）、搜索与 `call_tool` 的
指标、延迟与费用。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.deps import CurrentPrincipal
from backend.app.core.config import settings
from backend.app.db.models import AssistantConversation
from backend.app.db.models import AssistantMessage
from backend.app.db.models import Base
from backend.app.db.models import BatchScoringJob
from backend.app.db.models import GradingBatch
from backend.app.db.models import Rubric
from backend.app.services.assistant import model as assistant_model
from backend.app.services.assistant import runner
from backend.app.services.assistant.agent import catalog
from backend.app.services.assistant.agent.context import estimate_tokens
from backend.app.services.assistant.agent.context import system_prompt
from backend.app.services.assistant.agent.context import tools_array

EVAL_SET = Path(__file__).with_name("assistant_eval_set.jsonl")
#: 每百万 token 的美元价（Vertex 上的 Gemini 2.5 Flash；隐式缓存命中按输入价的一成）。
DEFAULT_PRICES = {"input": 0.30, "cached": 0.03, "output": 2.50}
#: 估算用：每条说法平均几次模型调用、每次输出多少 token、工具结果与状态多少 token。
ESTIMATE_CALLS_PER_ITEM = 3.0
ESTIMATE_OUTPUT_PER_CALL = 150
ESTIMATE_EXTRA_INPUT_PER_CALL = 600
GATES = {"accuracy": 0.95, "violations": 0, "p90_ms": 6000}
_LEAK_MARKERS = ("assistant-agent@", "底线：", "怎么用工具：", "可以搜索的工具分组")
EXTRA_BATCHES = ("2026 届毕业论文评分 · 计算机学院", "课程报告评分 · 软件工程导论", "实验报告评分 · 数据结构")


def load_items(path: Path = EVAL_SET) -> list[dict]:
    items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("评测集里有重复的 id")
    return items


def eval_set_version(path: Path = EVAL_SET) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def cost_usd(tokens: dict, prices: dict) -> float:
    prompt = int(tokens.get("prompt_tokens") or 0)
    cached = min(int(tokens.get("cached_tokens") or 0), prompt)
    output = int(tokens.get("completion_tokens") or 0)
    return ((prompt - cached) * prices["input"] + cached * prices["cached"] + output * prices["output"]) / 1_000_000


def estimate(items: list[dict], prices: dict) -> dict:
    """不调用模型的粗估：按前缀长度 × 平均调用次数，不计缓存折扣（偏高）。"""

    prefix = estimate_tokens(catalog.render(tools_array())) + estimate_tokens(system_prompt())
    calls = len(items) * ESTIMATE_CALLS_PER_ITEM
    input_tokens = int(calls * (prefix + ESTIMATE_EXTRA_INPUT_PER_CALL))
    output_tokens = int(calls * ESTIMATE_OUTPUT_PER_CALL)
    usd = cost_usd({"prompt_tokens": input_tokens, "completion_tokens": output_tokens}, prices)
    return {"items": len(items), "model_calls": calls, "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens, "usd": round(usd, 4)}


def check_budget(estimated_usd: float, *, round_budget: float, total_budget: float, spent: float) -> str | None:
    """返回拒绝的原因；None 表示可以跑。两道：本轮预算、累计总预算（§27）。"""

    if estimated_usd > round_budget:
        return "预计 %.4f 美元，超过本轮预算 %.2f 美元" % (estimated_usd, round_budget)
    if spent + estimated_usd > total_budget:
        return "已花 %.4f 美元，加上本轮预计 %.4f 美元，超过总预算 %.2f 美元" % (spent, estimated_usd, total_budget)
    return None


# --- 隔离环境 -----------------------------------------------------------------------

@contextlib.contextmanager
def isolated_environment(scorer):
    """内存数据库 + 合成种子数据；进程内打开 Agent 开关、关闭鉴权，把助手模型换成给定的模型。"""

    from backend.app.scripts.seed_dev import seed

    saved = {name: getattr(settings, name) for name in (
        "ASSISTANT_AGENT_ENABLED", "ASSISTANT_AGENT_DAILY_TURNS", "AUTH_ENABLED", "STORAGE_ROOT")}
    saved_build = assistant_model.build_scorer
    engine = create_engine("sqlite+pysqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with tempfile.TemporaryDirectory(prefix="assistant-eval-") as storage:
        try:
            settings.ASSISTANT_AGENT_ENABLED = True
            settings.ASSISTANT_AGENT_DAILY_TURNS = 100_000
            settings.AUTH_ENABLED = False
            settings.STORAGE_ROOT = Path(storage)  # 种子数据只建目录；指到临时目录，碰不到实例的存储卷
            assistant_model.build_scorer = lambda *args, **kwargs: scorer
            Base.metadata.create_all(engine)
            factory = sessionmaker(bind=engine, expire_on_commit=False)
            with factory() as session:
                seed(session)
                rubric = session.scalar(select(Rubric).where(Rubric.status == "published"))
                for name in EXTRA_BATCHES:
                    session.add(GradingBatch(name=name, rubric_id=rubric.id, created_by=rubric.created_by,
                                             organization_id=rubric.organization_id))
                session.commit()
            yield factory
        finally:
            assistant_model.build_scorer = saved_build
            for name, value in saved.items():
                setattr(settings, name, value)
            engine.dispose()


def _counts(db) -> dict:
    return {
        "batches": db.scalar(select(func.count(GradingBatch.id))),
        "jobs": db.scalar(select(func.count(BatchScoringJob.id))),
        "published": db.scalar(select(func.count(Rubric.id)).where(Rubric.status == "published")),
    }


def _principal() -> CurrentPrincipal:
    return CurrentPrincipal(settings.DEFAULT_DEV_USER_ID, None, None, "developer")


def _send(db, conversation, text: str) -> dict:
    return runner.run(db, _principal(), conversation,
                      {"type": "message", "text": text, "attachments": None, "quick_action": None})


# --- 逐条判定 -----------------------------------------------------------------------

def judge(item: dict, observed: dict) -> list[str]:
    """返回不通过的原因；空列表即通过。"""

    expect = item.get("expect") or {}
    reasons = []
    if observed["mode"] != "agent":
        reasons.append("没有走 Agent 回合（%s）" % observed["mode"])
    if observed["outcome"] != expect.get("outcome", "reply"):
        reasons.append("结束方式是 %s，期望 %s" % (observed["outcome"], expect.get("outcome", "reply")))
    called = set(observed["tools_called"])
    missing = [name for name in expect.get("tools_all", []) if name not in called]
    if missing:
        reasons.append("没有调用：%s" % "、".join(missing))
    forbidden = [name for name in expect.get("tools_none", []) if name in called]
    if forbidden:
        reasons.append("不该调用：%s" % "、".join(forbidden))
    if expect.get("search") == "required" and observed["searches"] == 0:
        reasons.append("需要先搜索延迟工具，却没有搜索")
    if expect.get("search") == "forbidden" and observed["searches"] > 0:
        reasons.append("常驻工具就够用，却搜索了 %d 次" % observed["searches"])
    if expect.get("cards_any") and not set(expect["cards_any"]) & set(observed["card_types"]):
        reasons.append("没有出现卡片：%s" % "、".join(expect["cards_any"]))
    if expect.get("pending_unchanged") and observed["pending_before"] != observed["pending_after"]:
        reasons.append("等待中的步骤被改变了")
    reasons.extend(observed["violations"])
    return reasons


def run_item(factory, item: dict, prices: dict) -> dict:
    with factory() as db:
        conversation = AssistantConversation(owner_id=settings.DEFAULT_DEV_USER_ID, organization_id=None,
                                             title="评测 %s" % item["id"], focus={})
        db.add(conversation)
        db.commit()
        for text in item.get("setup", []):
            _send(db, conversation, text)
        db.refresh(conversation)
        before, pending_before = _counts(db), (conversation.pending or {}).get("kind")
        started = time.monotonic()
        error = None
        try:
            result = _send(db, conversation, item["utterance"])
        except Exception as exc:  # 一条出错不影响其它条；记下类型，不记内容
            error, result = type(exc).__name__, {"messages": []}
        latency_ms = int((time.monotonic() - started) * 1000)
        db.refresh(conversation)
        after, pending_after = _counts(db), (conversation.pending or {}).get("kind")
        messages = list(result.get("messages") or [])
        user = next((message for message in messages if message.role == "user"), None)
        replies = [message for message in messages if message.role == "assistant"]
        final = next((message for message in reversed(replies) if (message.meta or {}).get("mode") == "agent"), None)
        meta = (final.meta if final is not None else None) or {}
        user_meta = (user.meta if user is not None else None) or {}
        text = "\n".join(message.text for message in replies)
        violations = []
        if after != before:
            violations.append("发生了写动作（任务、作业或已发布标准的数量变了）")
        if any(marker in text for marker in _LEAK_MARKERS):
            violations.append("回复泄露了系统指令")
        if error:
            violations.append("运行出错：%s" % error)
        tokens = meta.get("tokens") or {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
        observed = {
            "mode": meta.get("mode") or user_meta.get("mode") or "rules",
            "outcome": meta.get("outcome") or ("degrade" if user_meta.get("mode") == "degraded" else "none"),
            "reason": meta.get("reason") or user_meta.get("reason"),
            "tools_called": list(meta.get("tools_called") or []),
            "searches": int(meta.get("searches") or 0),
            "model_calls": int(meta.get("model_calls") or 0),
            "text_replies": int(meta.get("text_replies") or 0),
            "thought_signatures": int(meta.get("thought_signatures") or 0),
            "param_errors": int(meta.get("param_errors") or 0),
            "card_types": [card.get("type") for message in replies for card in message.cards or []],
            "pending_before": pending_before, "pending_after": pending_after,
            "violations": violations,
        }
        reasons = judge(item, observed)
        return {
            "id": item["id"], "category": item["category"], "utterance": item["utterance"],
            "expect": item.get("expect") or {}, "passed": not reasons, "reasons": reasons,
            "violations": violations, "latency_ms": latency_ms, "tokens": tokens,
            "cost_usd": round(cost_usd(tokens, prices), 6), "reply_text": text[:300],
            **{key: observed[key] for key in ("mode", "outcome", "reason", "tools_called", "searches", "model_calls",
                                              "text_replies", "thought_signatures", "param_errors", "card_types")},
        }


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))]


def summarize(results: list[dict], *, skipped: int) -> dict:
    ran = [item for item in results]
    passed = [item for item in ran if item["passed"]]
    latencies = [item["latency_ms"] for item in ran]
    by_category: dict[str, dict] = {}
    for item in ran:
        entry = by_category.setdefault(item["category"], {"passed": 0, "total": 0})
        entry["total"] += 1
        entry["passed"] += int(item["passed"])
    tokens = {key: sum(int(item["tokens"].get(key) or 0) for item in ran)
              for key in ("prompt_tokens", "cached_tokens", "completion_tokens")}
    accuracy = (len(passed) / len(ran)) if ran else 0.0
    violations = sum(len(item["violations"]) for item in ran)
    needed = [item for item in ran if (item["expect"].get("search") == "required")]
    found_right = sum(1 for item in needed if item["searches"] > 0 and set(item["expect"].get("tools_all", [])) <= set(item["tools_called"]))
    unneeded = sum(1 for item in ran if item["expect"].get("search") == "forbidden" and item["searches"] > 0)
    deferred_calls = sum(1 for item in ran for name in item["tools_called"] if name in _deferred_names())
    param_errors = sum(item["param_errors"] for item in ran)
    degraded = [item for item in ran if item["outcome"] == "degrade"]
    p90 = _percentile(latencies, 0.9)
    return {
        "ran": len(ran), "passed": len(passed), "failed": len(ran) - len(passed), "skipped": skipped,
        "accuracy": round(accuracy, 4), "violations": violations,
        "latency_ms": {"p50": _percentile(latencies, 0.5), "p90": p90, "max": max(latencies, default=0)},
        "by_category": by_category, "tokens": tokens,
        "cost_usd": round(sum(item["cost_usd"] for item in ran), 6),
        "gates": {
            "动作准确率 ≥ 95%": accuracy >= GATES["accuracy"],
            "违规 0 次": violations == GATES["violations"],
            "P90 延迟 ≤ 6 秒": p90 <= GATES["p90_ms"],
        },
        "search": {"needed": len(needed), "found_right": found_right, "unneeded": unneeded},
        "call_tool": {"deferred_calls": deferred_calls, "param_errors": param_errors},
        "provider_checks": {
            "tool_choice_required_honored": all(item["text_replies"] == 0 for item in ran),
            "text_replies": sum(item["text_replies"] for item in ran),
            "schema_accepted": not any(item.get("reason") == "HTTPStatusError" for item in degraded),
            "degraded": len(degraded),
            "degraded_reasons": sorted({str(item.get("reason")) for item in degraded}),
            "thought_signatures": sum(item["thought_signatures"] for item in ran),
            "cached_tokens_reported": tokens["cached_tokens"] > 0,
            "cache_read_ratio": round(tokens["cached_tokens"] / tokens["prompt_tokens"], 4) if tokens["prompt_tokens"] else 0.0,
        },
    }


def _deferred_names() -> set[str]:
    return {tool.name for tool in catalog.registry().values() if not tool.resident}


def run_eval(scorer, items: list[dict], *, prices: dict, round_budget: float) -> dict:
    """逐条跑；累计费用超过本轮预算就停，剩下的记为跳过。"""

    results, spent = [], 0.0
    with isolated_environment(scorer) as factory:
        for index, item in enumerate(items):
            if spent >= round_budget:
                return {"results": results, "skipped": len(items) - index, "stopped": "本轮费用已到预算"}
            result = run_item(factory, item, prices)
            spent += result["cost_usd"]
            results.append(result)
    return {"results": results, "skipped": 0, "stopped": None}


__all__ = ["DEFAULT_PRICES", "EVAL_SET", "check_budget", "cost_usd", "estimate", "eval_set_version", "judge",
           "load_items", "run_eval", "summarize"]
