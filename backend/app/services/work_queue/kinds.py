"""条目种类登记：每种条目（批量评分的一篇、AI 任务的一批）各自提供执行与收尾。

通用的领取、心跳、巡检只认这里的接口；种类之间的差别（父任务的状态机、执行内容、
怎样算“有进展”）留在各自的模块里。
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from importlib import import_module
from typing import Any
from typing import Callable


@dataclass(frozen=True)
class ClaimedItem:
    """一次领取的结果；``attempt`` 是这次执行的围栏号（条目的 attempt_count）。

    心跳与写结果都带着它做条件更新：巡检把条目重置、又被另一次执行领走之后，
    旧执行迟到的结果不会覆盖新执行。
    """

    kind: str
    item_id: str
    parent_id: str
    source_key: str
    attempt: int
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WorkKind:
    name: str
    # 数字小的先领：AI 条目（用户在页面上等）优先于批量评分条目。
    priority: int
    model: Any
    # 父任务表（批量评分任务 / AI 任务），带 ``last_swept_at``；条目上指向它的列名。
    parent_model: Any
    parent_column: str
    # (session, item, now) -> ClaimedItem | None：锁父任务、校验、把条目置为 running。
    begin: Callable
    # (session_factory, claimed, **options) -> None：执行并带围栏落库。
    execute: Callable
    # (session, item, now, *, progressed, exhausted) -> None：处理已死的执行。
    abandon: Callable
    # (session, item) -> bool：这次执行有没有写入新的检查点或结果。
    made_progress: Callable
    # (session_factory, *, parent_ids, now, limit) -> int：收敛父任务状态。
    converge: Callable
    # (session, parent_id) -> str | None：父任务的来源键（页面巡检用）。
    parent_source: Callable


_KINDS: dict[str, WorkKind] = {}
# 种类模块在首次使用时导入，避免 work_queue ↔ 业务模块的循环导入。
_KIND_MODULES = (
    "backend.app.services.batch_scoring.jobs",
)


def register(kind: WorkKind) -> WorkKind:
    _KINDS[kind.name] = kind
    return kind


def _ensure_registered() -> None:
    for module in _KIND_MODULES:
        import_module(module)


def all_kinds() -> list[WorkKind]:
    _ensure_registered()
    return sorted(_KINDS.values(), key=lambda kind: (kind.priority, kind.name))


def get_kind(name: str) -> WorkKind:
    _ensure_registered()
    return _KINDS[name]


__all__ = ["ClaimedItem", "WorkKind", "all_kinds", "get_kind", "register"]
