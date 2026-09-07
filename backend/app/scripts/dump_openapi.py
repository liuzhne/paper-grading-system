"""导出 OpenAPI 快照，供前端生成类型并做差异比对（前端 v2 计划 §12.2）。

快照本身**不入库**：它是从代码派生的，提交一份就多一处会过期的副本。CI 每次
现导出、现生成类型，再与仓库里已提交的 `schema.d.ts` 比对——后端合同变了而
前端类型没跟上，会在 PR 上直接显示成一个 diff。

输出按 key 排序且不带尾随空格，两次导出必须逐字节一致；否则差异比对每次都会
报"有变化"，很快就没人看了。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys


DEFAULT_OUTPUT = (
    pathlib.Path(__file__).resolve().parents[3] / "frontend" / "workbench" / "openapi.json"
)


def dump(output):
    """把当前应用的 OpenAPI 文档写到 ``output``，返回写入的路径。"""
    # 延迟导入：settings 在导入期固化，让调用方有机会先设置环境变量。
    from backend.app.main import app

    output = pathlib.Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    document = app.openapi()
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        nargs="?",
        default=DEFAULT_OUTPUT,
        help="输出路径，默认 frontend/workbench/openapi.json",
    )
    args = parser.parse_args(argv)
    written = dump(args.output)
    print("wrote %s" % written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
