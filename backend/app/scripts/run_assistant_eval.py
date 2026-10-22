"""评分助手 Agent 的真实模型评测（Agent 化方案 §27、Q8）。在生产实例的一次性容器里跑。

用法（由 `.github/workflows/assistant-eval.yml` 经 SSH 调用，不在笔记本上跑）：

    python -m backend.app.scripts.run_assistant_eval --mode estimate --phase A0 --spent-usd 0
    python -m backend.app.scripts.run_assistant_eval --mode run --phase A0 --spent-usd 0 --confirm-usd 0.12

- 模型用平台模型（管理员配置的那一条）：配置在一个**只读事务**里读出，读完即回滚，凭据不离开服务器；
- 评测在隔离的内存数据库里跑，只有合成的种子数据，不读写业务数据；
- 两道预算：本轮预计不超过 `--round-budget-usd`；历次已花（`--spent-usd`，由工作流按仓库里的报告合计）加本轮
  预计不超过 `--total-budget-usd`。`run` 还要求 `--confirm-usd`（维护者看过估算后确认的金额）不小于本轮预计；
- 跑的过程中累计费用到了本轮预算就停，剩下的记为跳过；
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


def model_summary(runtime) -> dict:
    host = (urlsplit(str(runtime.base_url or "")).hostname or "") if runtime else ""
    return {
        "source": "platform",
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
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", args.phase):
        parser.error("phase 只能是字母、数字、点、下划线、短横线")

    items = assistant_eval.load_items()
    if args.max_items > 0:
        items = items[: args.max_items]
    prices = dict(assistant_eval.DEFAULT_PRICES)
    estimated = assistant_eval.estimate(items, prices)
    payload = {
        "schema": 1, "mode": args.mode, "phase": args.phase, "started_at": _now(), "commit": args.commit,
        "eval_set": {"version": assistant_eval.eval_set_version(), "count": len(items)},
        "prices_usd_per_million": prices, "estimate": estimated,
        "budget": {"round_usd": args.round_budget_usd, "total_usd": args.total_budget_usd,
                   "spent_before_usd": args.spent_usd, "confirmed_usd": args.confirm_usd},
    }

    runtime = platform_runtime()
    payload["model"] = model_summary(runtime)
    if runtime is None:
        payload["refused"] = "生产上没有生效的平台模型"
        emit(payload)
        return EXIT_NO_MODEL
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

    scorer = get_llm_scorer(runtime)
    try:
        outcome = assistant_eval.run_eval(scorer, items, prices=prices, round_budget=args.round_budget_usd)
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
