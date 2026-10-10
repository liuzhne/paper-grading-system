"""AI 任务的两类错误：建任务时的请求问题，与条目执行时的模型/输出问题。"""

from __future__ import annotations


class AITaskProblem(Exception):
    """建任务、取消、重试时的可操作错误，路由映射为 HTTP problem。"""

    def __init__(self, status, code, message, user_action, *, retryable=False, context=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.user_action = user_action
        self.retryable = retryable
        self.context = context or {}


# 条目失败后的处理方式（方案第 6.4 节）：
# fail   立即判失败（额度耗尽、鉴权失败、请求被拒、输出被截断）
# retry  超时、5xx、网络中断：最多再执行 1 次
# defer  429 限流、熔断：带 Retry-After 回到待处理，最多延后 5 次
# repair 输出不合格：带修正提示再执行 1 次
DISPOSITIONS = ("fail", "retry", "defer", "repair")


class AITaskItemError(Exception):
    def __init__(self, code, message, *, disposition="fail", retry_after_seconds=None):
        if disposition not in DISPOSITIONS:
            raise ValueError("unknown disposition %r" % disposition)
        super().__init__(message)
        self.code = code
        self.message = message
        self.disposition = disposition
        self.retry_after_seconds = retry_after_seconds


class AITaskReuse(Exception):
    """建任务时发现要处理的内容全部已在进行中的任务里：直接返回那个任务。"""

    def __init__(self, task_id):
        super().__init__(task_id)
        self.task_id = task_id


__all__ = ["AITaskItemError", "AITaskProblem", "AITaskReuse", "DISPOSITIONS"]
