"""把后端测试文件分成 N 片，供提交阶段流水线的矩阵任务并行运行（RUNBOOK 3.0）。

- 按文件分：同一个文件里的用例仍在同一个进程里、按原来的顺序跑。
- 每片内部按路径排序，与整套运行时 pytest 的收集顺序一致，片内的相对顺序不变。
- 权重是每个文件的耗时（秒），存在 ``scripts/ci_test_weights.json``；没有记录的新文件按中位数计。
  权重过时只影响各片是否一样快，不影响跑哪些文件：每个测试文件恰好落在一片里
  （``backend/app/tests/test_ci_pipeline.py`` 守着这一点）。
- 分法：按权重从大到小，每个文件放进当前最轻的一片；同样的输入总是同样的结果。

用法::

    python scripts/ci_test_shard.py --shard 1 --total 4         # 打印第 1 片的测试文件，一行一个
    python scripts/ci_test_shard.py --record report-*.xml       # 用 pytest 的 JUnit 报告重算权重
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path("backend/app/tests")
WEIGHTS = ROOT / "scripts" / "ci_test_weights.json"


def test_files(root: Path = ROOT) -> list[str]:
    """与 pyproject 的 ``testpaths`` 和 pytest 默认的 ``test_*.py`` 规则一致。"""
    found = (root / TEST_ROOT).rglob("test_*.py")
    return sorted(
        path.relative_to(root).as_posix()
        for path in found
        if "__pycache__" not in path.parts and "node_modules" not in path.parts
    )


def load_weights(path: Path = WEIGHTS) -> dict[str, float]:
    if not path.exists():
        return {}
    return {str(name): float(seconds) for name, seconds in json.loads(path.read_text(encoding="utf-8")).items()}


def partition(files: list[str], weights: dict[str, float], total: int) -> list[list[str]]:
    if total < 1:
        raise ValueError("分片数至少为 1")
    known = [weights[name] for name in files if name in weights]
    fallback = statistics.median(known) if known else 1.0
    cost = {name: weights.get(name, fallback) for name in files}
    shards: list[list[str]] = [[] for _ in range(total)]
    loads = [0.0] * total
    for name in sorted(files, key=lambda item: (-cost[item], item)):
        lightest = min(range(total), key=lambda index: (loads[index], index))
        shards[lightest].append(name)
        loads[lightest] += cost[name]
    return [sorted(shard) for shard in shards]


def _module_to_file(classname: str, files: set[str]) -> str | None:
    """JUnit 的 classname 是 ``backend.app.tests.test_x`` 或再接一个类名，取能对上文件的最长前缀。"""
    parts = classname.split(".")
    for end in range(len(parts), 0, -1):
        candidate = "/".join(parts[:end]) + ".py"
        if candidate in files:
            return candidate
    return None


def record(reports: list[Path], files: list[str]) -> dict[str, float]:
    known = set(files)
    seconds: dict[str, float] = {}
    for report in reports:
        for case in ET.parse(report).getroot().iter("testcase"):
            name = _module_to_file(case.get("classname", ""), known)
            if name is not None:
                seconds[name] = seconds.get(name, 0.0) + float(case.get("time") or 0.0)
    return {name: round(seconds[name], 1) for name in sorted(seconds)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--shard", type=int, help="第几片，从 1 开始")
    parser.add_argument("--total", type=int, help="一共几片")
    parser.add_argument("--record", nargs="+", type=Path, metavar="JUNIT_XML", help="用 JUnit 报告重写权重文件")
    args = parser.parse_args(argv)

    files = test_files()
    if not files:
        print(f"没有找到测试文件：{TEST_ROOT}", file=sys.stderr)
        return 2

    if args.record:
        weights = record(args.record, files)
        if not weights:
            print("报告里没有能对上测试文件的用例", file=sys.stderr)
            return 2
        WEIGHTS.write_text(json.dumps(weights, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"已写入 {len(weights)} 个文件的耗时：{WEIGHTS.relative_to(ROOT)}")
        return 0

    if args.shard is None or args.total is None:
        parser.error("需要 --shard 与 --total，或者 --record")
    if not 1 <= args.shard <= args.total:
        parser.error("--shard 必须在 1 到 --total 之间")

    shards = partition(files, load_weights(), args.total)
    if any(not shard for shard in shards):
        # 空的一片交给 pytest 会变成“不带参数”，按 testpaths 把整套都跑一遍。
        print(f"测试文件只有 {len(files)} 个，分不出 {args.total} 片", file=sys.stderr)
        return 2
    print("\n".join(shards[args.shard - 1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
