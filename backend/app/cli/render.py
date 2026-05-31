"""CLI 输出：Rich 彩色表格/摘要（设计 §12「CLI 输出彩色摘要」）+ --json 机器输出。

`dump_json` 用标准库 print（不带 ANSI），保证管道里是干净 JSON。
"""

import json

from rich.console import Console
from rich.table import Table

console = Console()


def dump_json(obj):
    print(json.dumps(obj, ensure_ascii=False, default=str, indent=2))


def render_table(title, columns, rows, style_fn=None):
    """columns: 列名列表；rows: 每行为值的序列；style_fn(row)->样式(可空，整行)。"""
    table = Table(title=title, header_style="bold cyan", title_style="bold")
    for column in columns:
        table.add_column(column, overflow="fold")
    for row in rows:
        cells = ["" if value is None else str(value) for value in row]
        table.add_row(*cells, style=style_fn(row) if style_fn else None)
    console.print(table)


def info(message):
    console.print(message, style="green")


def warn(message):
    console.print(message, style="yellow")


def error(message):
    console.print(message, style="bold red")


def hint(message):
    console.print(message, style="dim")
