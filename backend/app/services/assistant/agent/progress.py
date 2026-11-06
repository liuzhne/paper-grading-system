"""评分助手回合进行时的进度文字（docs/评分助手过程流式方案.md §3、§4.1）。

只来自这张固定的表：唯一的参数是工具目录里写死的标题，不含模型输出、学生文字或分数——进度在校验之前
就推给老师，不能让模型的文字从这里绕过最终回复的校验（Agent 化方案 §14）。
"""

from __future__ import annotations

UNDERSTANDING = "正在理解你的问题…"
COMPACTING = "正在整理之前的对话…"
PROPOSING = "正在准备确认卡片…"
CHECKING = "正在核对回复…"


def for_tool(tool) -> str:
    """调用业务工具之前的进度：提议工具一律“准备确认卡片”，查询工具“正在查：标题”。"""

    if tool.kind == "proposal":
        return PROPOSING
    return "正在查：%s…" % tool.title


__all__ = ["CHECKING", "COMPACTING", "PROPOSING", "UNDERSTANDING", "for_tool"]
