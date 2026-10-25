"""评分助手 Agent 的真实模型评测（Agent 化方案 §27、Q8）。在生产实例的一次性容器里跑。

用法（由 `.github/workflows/assistant-eval.yml` 经 SSH 调用，不在笔记本上跑）：

    python -m backend.app.scripts.run_assistant_eval --mode estimate --phase A0 --spent-usd 0
    python -m backend.app.scripts.run_assistant_eval --mode run --phase A0 --spent-usd 0 --confirm-usd 0.12
    python -m backend.app.scripts.run_assistant_eval --mode estimate --phase A1 --spent-usd 0 --connection-id <UUID>

- 模型默认用平台模型（管理员配置的那一条）；`--connection-id` 改用一条指定的 AI 连接（维护者自己的）。
  配置都在一个**只读事务**里读出，读完即回滚，凭据不离开服务器；输出里只有模型名、主机与来源，
  不打印连接名称与所属的人（仓库公开，运行日志谁都能看）；
- 价目按模型查（`assistant_eval.MODEL_PRICES`），只认 Google 端点上登记过的模型；查不到就拒绝，不估也不跑；
- 评测在隔离的内存数据库里跑，只有合成的种子数据，不读写业务数据；
- 两道预算：本轮预计不超过 `--round-budget-usd`；历次已花（`--spent-usd`，由工作流按仓库里的报告合计）加本轮
  预计不超过 `--total-budget-usd`。`run` 还要求 `--confirm-usd`（维护者看过估算后确认的金额）不小于本轮预计；
- 跑的过程中累计费用到了本轮预算就停，剩下的记为跳过；
- 条与条之间隔 `--pace-seconds`（默认 3 秒）；一条被限流或熔断挡住，等熔断冷却过去再重跑这一条
  （`--rate-limit-retries`，默认 2 次），限流的等待累计不超过 8 分钟。A1 第一次评测是连发，Vertex 在
  约 10 秒、11 次调用后返回 429，熔断之后 37 条都退回了规则模式，测到的是额度而不是 Agent；
- 结果只打印到标准输出（两行标记之间的 JSON），不落盘；报告由工作流在 GitHub 上生成。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from datetime import timezone
from urllib.parse import urlsplit

from sqlalchemy import text

MARK_BEGIN = "=====ASSISTANT_EVAL_JSON_BEGIN====="
MARK_END = "=====ASSISTANT_EVAL_JSON_END====="
EXIT_REFUSED = 2
EXIT_NO_MODEL = 3
#: 限流的等待累计上限：工作流 30 分钟超时，正常跑完约 5 分钟。
MAX_RATE_LIMIT_WAIT_SECONDS = 480
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def platform_runtime():
    """读平台模型配置：只读事务，读完回滚（事务池下只作用于这一个事务，不会漏到别的连接）。"""

    from backend.app.db.session import SessionLocal
    from backend.app.services import platform_llm

    with SessionLocal() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            config = platform_llm.get_active_config(session)
            return platform_llm.runtime_for(config) if config is not None else None
        finally:
            session.rollback()


def connection_runtime(connection_id: str):
    """读一条指定的 AI 连接：同样只读事务、读完回滚。返回 (运行时, 读不到的原因)。

    原因只说“找不到 / 没启用”，不带连接名称。
    """

    from backend.app.db.models import AIConnection
    from backend.app.db.session import SessionLocal
    from backend.app.services import ai_connections

    with SessionLocal() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            row = session.get(AIConnection, connection_id)
            if row is None:
                return None, "找不到这条 AI 连接"
            try:
                runtime = ai_connections.resolve_connection_runtime(
                    session, connection_id=row.id, owner_id=row.owner_id, organization_id=row.organization_id)
            except ValueError:
                return None, "这条 AI 连接没有启用"
            return runtime, None
        finally:
            session.rollback()


def model_summary(runtime, source: str) -> dict:
    host = (urlsplit(str(runtime.base_url or "")).hostname or "") if runtime else ""
    return {
        "source": source,
        "provider_type": getattr(runtime, "provider_type", None),
        "name": getattr(runtime, "model_name", None),
        "host": host,
    }


def emit(payload: dict) -> None:
    print(MARK_BEGIN)
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    print(MARK_END)
    sys.stdout.flush()


def main(argv=None) -> int:
    from backend.app.eval import assistant_eval
    from backend.app.services.llm.factory import get_llm_scorer

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("estimate", "run"), required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--round-budget-usd", type=float, default=0.5)
    parser.add_argument("--total-budget-usd", type=float, default=3.0)
    parser.add_argument("--spent-usd", type=float, required=True)
    parser.add_argument("--confirm-usd", type=float, default=None)
    parser.add_argument("--max-items", type=int, default=0)
    parser.add_argument("--commit", default="")
    parser.add_argument("--connection-id", default="")
    parser.add_argument("--pace-seconds", type=float, default=3.0)
    parser.add_argument("--rate-limit-retries", type=int, default=2)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", args.phase):
        parser.error("phase 只能是字母、数字、点、下划线、短横线")
    connection_id = args.connection_id.strip().lower()
    if connection_id and not _UUID.fullmatch(connection_id):
        parser.error("connection-id 必须是连接编号（UUID）")

    all_items = assistant_eval.load_items()
    items = all_items[: args.max_items] if args.max_items > 0 else all_items
    from backend.app.services.assistant.agent.context import SYSTEM_VERSION
    from backend.app.services.assistant.agent.context import prompt_fingerprint

    payload = {
        "schema": 1, "mode": args.mode, "phase": args.phase, "started_at": _now(), "commit": args.commit,
        # count 是这一轮跑的条数，total 是评测集的全部条数：只跑了一部分的一轮不能当上线依据（§12.5）。
        "eval_set": {"version": assistant_eval.eval_set_version(), "count": len(items), "total": len(all_items)},
        # 上线门槛按指纹认（§12.5）：这一轮测的是哪一版提示词。
        "prompt": {"version": SYSTEM_VERSION, "fingerprint": prompt_fingerprint()},
        "budget": {"round_usd": args.round_budget_usd, "total_usd": args.total_budget_usd,
                   "spent_before_usd": args.spent_usd, "confirmed_usd": args.confirm_usd},
    }

    if connection_id:
        runtime, missing = connection_runtime(connection_id)
        payload["model"] = model_summary(runtime, "connection")
    else:
        runtime = platform_runtime()
        missing = None if runtime is not None else "生产上没有生效的平台模型"
        payload["model"] = model_summary(runtime, "platform")
    if runtime is None:
        payload["refused"] = missing
        emit(payload)
        return EXIT_NO_MODEL
    prices = assistant_eval.prices_for(runtime.model_name, runtime.base_url)
    if prices is None:
        payload["refused"] = "没有登记这个模型的价目（只认 Google 端点上的 %s），不估也不跑" % "、".join(
            sorted(assistant_eval.MODEL_PRICES))
        payload["finished_at"] = _now()
        emit(payload)
        return EXIT_REFUSED
    estimated = assistant_eval.estimate(items, prices)
    payload["prices_usd_per_million"] = prices
    payload["estimate"] = estimated
    refused = assistant_eval.check_budget(estimated["usd"], round_budget=args.round_budget_usd,
                                          total_budget=args.total_budget_usd, spent=args.spent_usd)
    if refused is None and args.mode == "run" and (args.confirm_usd is None or args.confirm_usd < estimated["usd"]):
        refused = "正式运行需要 --confirm-usd 不小于本轮预计（%.4f 美元）" % estimated["usd"]
    if refused or args.mode == "estimate":
        if refused:
            payload["refused"] = refused
        payload["finished_at"] = _now()
        emit(payload)
        return EXIT_REFUSED if refused else 0

    from backend.app.core.config import settings

    # 比熔断冷却多等 5 秒：冷却过后熔断器放行一次试探，试探成功才恢复。
    pacing = {"pace_seconds": max(0.0, args.pace_seconds), "rate_limit_retries": max(0, args.rate_limit_retries),
              "rate_limit_wait_seconds": float(settings.PROVIDER_CIRCUIT_COOLDOWN_SECONDS + 5),
              "max_rate_limit_wait_seconds": float(MAX_RATE_LIMIT_WAIT_SECONDS)}
    payload["pacing"] = pacing
    scorer = get_llm_scorer(runtime)
    try:
        outcome = assistant_eval.run_eval(scorer, items, prices=prices, round_budget=args.round_budget_usd, **pacing)
    finally:
        close = getattr(scorer, "close", None)
        if callable(close):
            close()
    payload["items"] = outcome["results"]
    payload["stopped"] = outcome["stopped"]
    payload["summary"] = assistant_eval.summarize(outcome["results"], skipped=outcome["skipped"])
    payload["finished_at"] = _now()
    emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
