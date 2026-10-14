"""进程级日志配置：web（uvicorn）与 worker 用同一种格式与级别。

uvicorn 只配置它自己的 ``uvicorn.*`` 日志器。根日志器没人配置时，应用里的
``logger.info`` 全部被丢弃，``logger.warning`` 只经 Python 的兜底处理器输出一行没有
时间、级别和来源的文字——2026-10-14 平台模型测试连接失败时，日志里因此看不出原因。

级别由 ``LOG_LEVEL`` 决定（默认 INFO）。调到 DEBUG 也不会记录模型请求原文：原文日志
由 ``LLM_DEBUG_LOG_ENABLED`` 单独控制，受保护部署禁止开启。
"""

from __future__ import annotations

import logging
import sys


FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
# 第三方 HTTP 与存储客户端在 INFO/DEBUG 会逐条记录请求地址（含存储对象路径），
# 不随 LOG_LEVEL 调低，固定只记 WARNING 以上。
QUIET_LOGGERS = (
    "httpx",
    "httpcore",
    "hpack",
    "urllib3",
    "botocore",
    "supabase",
    "postgrest",
    "storage3",
    "gotrue",
)


def configure_logging(level: str) -> bool:
    """给根日志器挂一个标准输出处理器；返回是否做了配置。

    根日志器已经有处理器时什么都不做（与 ``logging.basicConfig`` 相同）：pytest 和嵌入本应用
    的程序有自己的日志配置，不该被覆盖。
    """

    root = logging.getLogger()
    if root.handlers:
        return False
    numeric = logging.getLevelName(str(level).upper())
    if not isinstance(numeric, int):
        numeric = logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(handler)
    root.setLevel(numeric)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(numeric, logging.WARNING))
    return True


__all__ = ["FORMAT", "QUIET_LOGGERS", "configure_logging"]
