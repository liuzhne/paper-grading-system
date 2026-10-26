"""打开评分助手 Agent 开关前的门槛（docs/评分助手Agent化方案.md §12.5，维护者 2026-10-25 定）。

    python -m backend.app.scripts.assistant_eval_gate docs/评测报告/评分助手

报告目录里必须有一轮**在当前提示词指纹上**、**跑满当前评测集**（版本一致、条数齐全、没有跳过）、门槛全过、
没有标作废的正式评测；没有就返回 1。只跑了几条（`max_items`）的一轮不算：没测到的条目不能当作通过。
线上只对评测过的模型开 Agent（`agent/evaluated.py`）：名单里的**每个模型**都要有这样一轮，缺一个也返回 1。
部署流水线在仓库变量 `ASSISTANT_AGENT_ENABLED=true` 时用刚构建的镜像跑它（指纹按镜像里的代码算），
不过就不部署。不调用模型，不读数据库。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def passing_reports(directory: Path, fingerprint: str, *, eval_version: str, eval_count: int,
                    model: tuple[str, str] | None = None) -> list[str]:
    """门槛全过、指纹一致、跑满当前评测集、没有作废的正式评测报告文件名；给了 ``model`` 只认用它跑的。"""

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
        if model is not None and _model_of(data) != model:
            continue
        if (data.get("prompt") or {}).get("fingerprint") == fingerprint and complete and gates and all(gates.values()):
            found.append(path.name)
    return found


def _model_of(data: dict) -> tuple[str, str]:
    from backend.app.services.assistant.agent import evaluated

    model = data.get("model") or {}
    return evaluated.family(host=model.get("host")), evaluated.model_id(model.get("name"))


def main(argv=None) -> int:
    from backend.app.eval import assistant_eval
    from backend.app.services.assistant.agent.context import SYSTEM_VERSION
    from backend.app.services.assistant.agent.context import prompt_fingerprint
    from backend.app.services.assistant.agent.evaluated import EVALUATED_MODELS

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    fingerprint = prompt_fingerprint()
    if not EVALUATED_MODELS:
        print("agent/evaluated.py 里没有登记任何模型：开关打开了也不会有 Agent 回合。", file=sys.stderr)
        return 1
    eval_version, eval_count = assistant_eval.eval_set_version(), len(assistant_eval.load_items())
    found = {model: passing_reports(args.directory, fingerprint, eval_version=eval_version, eval_count=eval_count,
                                    model=model) for model in sorted(EVALUATED_MODELS)}
    missing = ["%s（%s）" % (name, family) for (family, name), reports in found.items() if not reports]
    if not missing:
        print("评分助手提示词 %s（指纹 %s）有达标的评测：%s" % (
            SYSTEM_VERSION, fingerprint, "、".join(sorted({report for reports in found.values() for report in reports}))))
        return 0
    if any(found.values()):
        print("评分助手提示词 %s（指纹 %s）：这些登记过的模型还没有达标的评测：%s。先用它们各跑一轮完整的评测并提交报告，"
              "或者从 agent/evaluated.py 的名单里去掉。" % (SYSTEM_VERSION, fingerprint, "、".join(missing)), file=sys.stderr)
        return 1
    print("评分助手提示词 %s（指纹 %s）还没有达标的评测（要跑满评测集 %s 的 %d 条）：先在这个版本上跑一轮完整的评测"
          "并提交报告，或者把仓库变量 ASSISTANT_AGENT_ENABLED 改回 false。"
          % (SYSTEM_VERSION, fingerprint, eval_version, eval_count), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
