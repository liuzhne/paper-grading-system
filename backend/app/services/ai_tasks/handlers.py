"""AI 任务种类登记：每种操作怎样拆条目、执行一个条目、合并结果。"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from importlib import import_module
from typing import Any
from typing import Callable


@dataclass(frozen=True)
class PreparedTask:
    """建任务时冻结的输入与条目拆分。``items`` 为空时任务直接以 ``result`` 成功。"""

    scope: dict
    input_snapshot: dict
    items: list[dict]
    result: dict | None = None
    # 只用于去重指纹的额外材料（例如归类的单元原文哈希）。
    fingerprint_material: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TaskView:
    """执行条目时传给处理函数的任务快照，避免把会话里的 ORM 对象带进模型调用。"""

    id: str
    kind: str
    rubric_id: str
    owner_id: str | None
    organization_id: str | None
    scope: dict
    input_snapshot: dict
    model_name: str | None


@dataclass(frozen=True)
class TaskHandler:
    kind: str
    prompt_version: str
    # (session, rubric_id, params, principal) -> PreparedTask
    prepare: Callable
    # (view, item_input, scorer) -> dict；失败抛 AITaskItemError
    run_item: Callable
    # (view, outputs_in_ordinal_order) -> dict
    merge: Callable
    # (session, view, item_input, output) -> None：条目成功时增量落库（例如归类建议）。
    # 抛 AITaskItemError 时这次执行按失败处理（例如归类期间原文已变化）。
    on_item_success: Callable | None = None
    # 页面上的操作名，用于错误提示
    label: str = "AI 操作"
    extra: dict[str, Any] = field(default_factory=dict)


_HANDLERS: dict[str, TaskHandler] = {}
_HANDLER_MODULES = (
    "backend.app.services.ai_tasks.rule_draft",
    "backend.app.services.ai_tasks.unit_classification",
)


def register_handler(handler: TaskHandler) -> TaskHandler:
    _HANDLERS[handler.kind] = handler
    return handler


def get_handler(kind: str) -> TaskHandler:
    for module in _HANDLER_MODULES:
        import_module(module)
    try:
        return _HANDLERS[kind]
    except KeyError as exc:
        raise KeyError("unsupported AI task kind: %s" % kind) from exc


def supported_kinds() -> list[str]:
    for module in _HANDLER_MODULES:
        import_module(module)
    return sorted(_HANDLERS)


__all__ = [
    "PreparedTask",
    "TaskHandler",
    "TaskView",
    "get_handler",
    "register_handler",
    "supported_kinds",
]
