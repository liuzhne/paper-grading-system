"""评分助手的过程流式（docs/评分助手过程流式方案.md §4.2、§4.3）。

运行和连接分开：接口抢到运行锁之后，由这里起一个后台线程，用自己的数据库会话跑 `runner.run`；
进度事件经 `asyncio.Queue` 推给响应。老师刷新、关页面、切换组织时生成器被取消，后台线程照样跑完、
提交、释放锁——回复会存进会话，重新打开就能看到。进程退出（发版）时线程跟着结束，和 JSON 接口一样：
事务没有提交，锁按 `runner.LOCK_SECONDS` 过期。

只推三类数据：`progress`（代码表里的进度文字）、`done`（与 `/runs` 的返回体相同）、`error`。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any
from typing import AsyncIterator

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.app.api.deps import CurrentPrincipal
from backend.app.db.models import AssistantConversation
from backend.app.schemas.assistant import AssistantRunRead
from backend.app.services.assistant import runner

logger = logging.getLogger(__name__)

#: 这么久没有事件就发一行注释，防止代理把空闲连接断开。
HEARTBEAT_SECONDS = 15
FAILED_DETAIL = "助手处理失败，请稍后再试。"

#: 进行中的后台线程：测试用它等回合跑完。
ACTIVE: set[threading.Thread] = set()
_ACTIVE_LOCK = threading.Lock()


def sse(event: str, data: Any) -> str:
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def _work(bind, principal: CurrentPrincipal, conversation_id: str, request: dict, put) -> None:
    """后台线程：自己开会话跑一次运行。锁已经由接口抢到；`runner.run` 结束时释放。"""

    started = time.monotonic()
    db = Session(bind=bind, autoflush=False)
    entered = False
    outcome = "done"
    try:
        conversation = db.get(AssistantConversation, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        entered = True
        result = runner.run(db, principal, conversation, request, emit=put, locked=True)
        # 序列化在会话关闭之前做：消息是 ORM 对象。与 `/runs` 的 response_model 同一个结构。
        put("done", AssistantRunRead.model_validate(result, from_attributes=True).model_dump(mode="json"))
    except HTTPException as exc:
        outcome = "http_%d" % exc.status_code
        put("error", {"status": exc.status_code, "detail": exc.detail})
    except Exception:
        outcome = "failed"
        logger.exception("assistant_stream_failed conversation=%s", conversation_id)
        put("error", {"status": 500, "detail": FAILED_DETAIL})
    finally:
        if not entered:
            # 还没进到 runner.run：锁要在这里放掉，否则老师要等锁过期才能再说话。
            try:
                runner.release_lock(db, conversation_id)
            except Exception:
                logger.exception("assistant_stream_release_failed conversation=%s", conversation_id)
        db.close()
        logger.info("assistant_stream_run conversation=%s outcome=%s latency_ms=%d",
                    conversation_id, outcome, int((time.monotonic() - started) * 1000))
        put(None, None)
        with _ACTIVE_LOCK:
            ACTIVE.discard(threading.current_thread())


def start(bind, principal: CurrentPrincipal, conversation_id: str, request: dict) -> AsyncIterator[str]:
    """起后台线程并返回响应生成器。线程在这里就启动：即使连接立刻断开、生成器一次都没被读，回合也会跑。"""

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def put(kind, data) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, data))
        except RuntimeError:
            pass  # 事件循环已经关了：连接早就断了，进度丢掉无所谓

    thread = threading.Thread(target=_work, args=(bind, principal, conversation_id, request, put),
                              name="assistant-run", daemon=True)
    with _ACTIVE_LOCK:
        ACTIVE.add(thread)
    thread.start()
    return _events(queue, conversation_id)


async def _events(queue: asyncio.Queue, conversation_id: str) -> AsyncIterator[str]:
    yield sse("accepted", {})
    try:
        while True:
            try:
                kind, data = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if kind is None:
                return
            yield sse(kind, data)
    except asyncio.CancelledError:
        logger.info("assistant_stream_disconnected conversation=%s", conversation_id)
        raise


def wait_idle(timeout: float = 10.0) -> None:
    """测试用：等所有后台线程结束。"""

    deadline = time.monotonic() + timeout
    while True:
        with _ACTIVE_LOCK:
            threads = list(ACTIVE)
        if not threads:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("assistant stream threads still running")
        threads[0].join(remaining)


__all__ = ["ACTIVE", "HEARTBEAT_SECONDS", "sse", "start", "wait_idle"]
