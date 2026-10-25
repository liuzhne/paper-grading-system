"""打开评分助手 Agent 开关前的门槛（docs/评分助手Agent化方案.md §12.5，维护者 2026-10-25 定）。

    python -m backend.app.scripts.assistant_eval_gate docs/评测报告/评分助手

报告目录里必须有一轮**在当前提示词指纹上**、**跑满当前评测集**（版本一致、条数齐全、没有跳过）、门槛全过、
没有标作废的正式评测；没有就返回 1。只跑了几条（`max_items`）的一轮不算：没测到的条目不能当作通过。
部署流水线在仓库变量 `ASSISTANT_AGENT_ENABLED=true` 时用刚构建的镜像跑它（指纹按镜像里的代码算），
不过就不部署。不调用模型，不读数据库。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def passing_reports(directory: Path, fingerprint: str, *, eval_version: str, eval_count: int) -> list[str]:
    """门槛全过、指纹一致、跑满当前评测集、没有作废的正式评测报告文件名。"""

    found = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("schema") != 1 or data.get("mode") != "run" or data.get("obsolete"):
            continue
        summary = data.get("summary") or {}
        gates = summary.get("gates") or {}
        complete = ((data.get("eval_set") or {}).get("version") == eval_version and summary.get("ran") == eval_count
                    and not summary.get("skipped") and len(data.get("items") or []) == eval_count)
        if (data.get("prompt") or {}).get("fingerprint") == fingerprint and complete and gates and all(gates.values()):
            found.append(path.name)
    return found


def main(argv=None) -> int:
    from backend.app.eval import assistant_eval
    from backend.app.services.assistant.agent.context import SYSTEM_VERSION
    from backend.app.services.assistant.agent.context import prompt_fingerprint

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    fingerprint = prompt_fingerprint()
    eval_version, eval_count = assistant_eval.eval_set_version(), len(assistant_eval.load_items())
    found = passing_reports(args.directory, fingerprint, eval_version=eval_version, eval_count=eval_count)
    if found:
        print("评分助手提示词 %s（指纹 %s）有达标的评测：%s" % (SYSTEM_VERSION, fingerprint, "、".join(found)))
        return 0
    print("评分助手提示词 %s（指纹 %s）还没有达标的评测（要跑满评测集 %s 的 %d 条）：先在这个版本上跑一轮完整的评测"
          "并提交报告，或者把仓库变量 ASSISTANT_AGENT_ENABLED 改回 false。"
          % (SYSTEM_VERSION, fingerprint, eval_version, eval_count), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
