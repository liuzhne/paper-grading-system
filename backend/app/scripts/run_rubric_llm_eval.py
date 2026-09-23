"""评分规则解析 LLM 能力离线评估（解析重构方案阶段 5、6.5 验收）。

用法（需要真实 LLM；输出 JSON 指标）：
    LLM_PROVIDER=openai_compatible OPENAI_COMPATIBLE_API_KEY=... \\
    .venv/bin/python -m backend.app.scripts.run_rubric_llm_eval --suite all
"""

import argparse
import json
import sys

from backend.app.eval.rubric_llm_eval import evaluate_classifier
from backend.app.eval.rubric_llm_eval import evaluate_review


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["review", "classifier", "all"], default="all")
    args = parser.parse_args(argv)
    from backend.app.services.llm.factory import get_llm_scorer

    scorer = get_llm_scorer()
    report = {}
    try:
        if args.suite in ("review", "all"):
            report["review"] = evaluate_review(scorer)
        if args.suite in ("classifier", "all"):
            report["classifier"] = evaluate_classifier(scorer)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, default=list))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
