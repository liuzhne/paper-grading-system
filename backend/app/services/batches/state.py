"""批次业务阶段状态机（前端 v2 计划 §5-C）。

**阶段不是任务状态。** 这里的七种阶段描述的是「这批材料现在处于评审流程的
哪一步」，由材料与当前结果推导；`BatchScoringJob.status` 描述的是「某次执行
在做什么」。一次 job 失败不代表批次阶段就是失败——阶段要看还剩多少材料没有
有效结果。两者必须并行展示，不能用阶段字段表达全部任务故障。

所有阶段变更都必须经过 :func:`apply_event`：客户端不能直接写阶段，尤其不能
直接写终态。`state_version` 提供乐观并发，`archived` 是写守卫。
"""

from __future__ import annotations

BATCH_STAGES = (
    "draft",
    "parsing",
    "scoring",
    "scored",
    "scored_with_errors",
    "reviewed",
    "archived",
)

#: 评分结束后允许落到的两个终态。
SCORING_OUTCOMES = ("scored", "scored_with_errors")

#: 已产出结果、可以进入复核的阶段。
RESULT_STAGES = ("scored", "scored_with_errors")


class BatchStateError(Exception):
    """非法的阶段转移或未知事件。"""


class BatchStateConflict(BatchStateError):
    """乐观并发失败：期望版本与当前版本不一致，整次不写入。"""


class BatchArchived(BatchStateError):
    """已归档批次拒绝写动作，直到显式 reopen。"""


# 事件 -> 允许的起始阶段。目标阶段由 _resolve_target 决定，因为取消与
# 重开的落点取决于当前结果，不能写死在表里。
_ALLOWED_FROM = {
    "start_parsing": ("draft",),
    "finish_parsing": ("parsing",),
    # 重评从任何已出结果的阶段（含 reviewed）重新开始，并撤销既有复核结论。
    "start_scoring": ("draft", "scored", "scored_with_errors", "reviewed"),
    "finish_scoring": ("scoring",),
    "cancel": ("parsing", "scoring"),
    "complete_review": RESULT_STAGES,
    "archive": ("reviewed",),
    "reopen": ("archived",),
}

#: reopen 之外的所有事件在 archived 下都被拒绝。
_ARCHIVE_EXEMPT = ("reopen",)


def stable_stage(batch) -> str:
    """由当前材料与结果推导出的稳定阶段。

    用于取消与重开的落点：这两种情况都不能由调用方指定目标阶段，否则会把
    「执行被中断」误记成「已完成」。
    """
    papers = list(getattr(batch, "papers", None) or [])
    if not papers:
        return "draft"

    scored = 0
    failed = 0
    for paper in papers:
        runs = list(getattr(paper, "scoring_runs", None) or [])
        if not runs:
            continue
        latest = max(runs, key=lambda run: (run.created_at, run.id))
        if latest.status in ("scored", "reviewed"):
            scored += 1
        elif latest.status == "failed":
            failed += 1

    if scored == 0:
        return "draft"
    if failed or scored < len(papers):
        return "scored_with_errors"
    return "scored"


def _resolve_target(batch, event, outcome):
    if event == "finish_scoring":
        if outcome not in SCORING_OUTCOMES:
            raise BatchStateError(
                "finish_scoring 的结果必须是 %s 之一，收到 %r"
                % (" / ".join(SCORING_OUTCOMES), outcome)
            )
        return outcome
    if event in ("cancel", "reopen"):
        # 中断与重开都回落到结果推导出的阶段，不宣称完成。
        return stable_stage(batch)
    return {
        "start_parsing": "parsing",
        "finish_parsing": "draft",
        "start_scoring": "scoring",
        "complete_review": "reviewed",
        "archive": "archived",
    }[event]


def guard_writable(batch) -> None:
    """已归档批次拒绝上传、解析、评分、改分、取消与重试等写动作。"""
    if batch.status == "archived":
        raise BatchArchived("批次已归档，需先显式重新打开才能修改")


def apply_event(session, batch, event, *, outcome=None, expected_version=None):
    """在同一服务内校验并应用一次阶段转移。

    :param outcome: 仅 ``finish_scoring`` 使用，取 :data:`SCORING_OUTCOMES` 之一。
    :param expected_version: 提供时执行乐观并发检查；不匹配抛
        :class:`BatchStateConflict` 且不做任何写入。
    """
    if event not in _ALLOWED_FROM:
        raise BatchStateError("未知的批次事件：%r" % (event,))

    if batch.status == "archived" and event not in _ARCHIVE_EXEMPT:
        raise BatchArchived("批次已归档，拒绝事件 %r" % (event,))

    if expected_version is not None and batch.state_version != expected_version:
        raise BatchStateConflict(
            "批次状态已被其他操作更新（期望版本 %s，当前 %s）"
            % (expected_version, batch.state_version)
        )

    allowed_from = _ALLOWED_FROM[event]
    if batch.status not in allowed_from:
        raise BatchStateError(
            "非法的批次阶段转移：%s 阶段不能执行 %r（允许的起始阶段：%s）"
            % (batch.status, event, " / ".join(allowed_from))
        )

    target = _resolve_target(batch, event, outcome)
    if target not in BATCH_STAGES:
        raise BatchStateError("非法的目标阶段：%r" % (target,))

    batch.status = target
    batch.state_version = (batch.state_version or 0) + 1
    session.add(batch)
    return batch


__all__ = [
    "BATCH_STAGES",
    "SCORING_OUTCOMES",
    "RESULT_STAGES",
    "BatchStateError",
    "BatchStateConflict",
    "BatchArchived",
    "apply_event",
    "guard_writable",
    "stable_stage",
    "available_events",
]


def available_events(batch):
    """当前阶段允许的事件，供前端渲染可执行动作。

    前端不自行推断转移合法性——判据只有一份，就在这里；否则界面会出现
    「按钮可点但服务端拒绝」或反过来的错位。
    """
    if batch.status == "archived":
        return ["reopen"]
    return [
        event
        for event, allowed_from in _ALLOWED_FROM.items()
        if event not in _ARCHIVE_EXEMPT and batch.status in allowed_from
    ]
