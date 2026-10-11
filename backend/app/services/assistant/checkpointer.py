"""评分助手流程图的状态存储：LangGraph `BaseCheckpointSaver` 的 SQLAlchemy 实现（方案 T3）。

为什么自写：官方 Postgres 存储在运行时 `setup()` 自己建表，生产运行角色
`pgs_app` 没有 DDL 权限；测试又是 SQLite，要换另一种实现。这里的表由迁移
0035 创建并授权、建 RLS，测试与生产走同一份代码。

语义照 LangGraph 自带的 `InMemorySaver`：最新快照按快照编号取最大（编号按时间
单调递增）；中间写入对非负下标去重。快照里的 `channel_values` 与快照本体一起
序列化，不拆成单独的 blob——助手的状态只有编号与标志位，体积很小。

**写入先进内存缓冲，运行结束后由调用方在主线程 `persist()`。** LangGraph 在后台
线程里保存快照与中间写入，同时节点在主线程里用同一个数据库会话写消息；
SQLAlchemy 会话不能跨线程并发使用（会报 “Session is already flushing”）。缓冲
让后台线程只碰加锁的内存，数据库只在主线程里读写。读取时缓冲优先，
因为本次运行产生的快照编号一定比库里的新。
"""

from __future__ import annotations

import random
import threading
from collections.abc import Iterator
from collections.abc import Sequence
from typing import Any

from langgraph.checkpoint.base import WRITES_IDX_MAP
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.base import ChannelVersions
from langgraph.checkpoint.base import Checkpoint
from langgraph.checkpoint.base import CheckpointMetadata
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.base import get_checkpoint_id
from langgraph.checkpoint.base import get_checkpoint_metadata
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import AssistantCheckpoint
from backend.app.db.models import AssistantCheckpointWrite


class SqlCheckpointSaver(BaseCheckpointSaver[str]):
    def __init__(self, session: Session, *, serde=None) -> None:
        super().__init__(serde=serde)
        self.session = session
        self._lock = threading.RLock()
        # (thread_id, ns, checkpoint_id) -> 行字段
        self._checkpoints: dict[tuple[str, str, str], dict] = {}
        # (thread_id, ns, checkpoint_id, task_id, idx) -> 行字段
        self._writes: dict[tuple[str, str, str, str, int], dict] = {}

    # --- 读 -------------------------------------------------------------------------

    def _db_writes(self, thread_id: str, ns: str, checkpoint_id: str) -> dict:
        rows = self.session.scalars(
            select(AssistantCheckpointWrite).where(
                AssistantCheckpointWrite.thread_id == thread_id,
                AssistantCheckpointWrite.checkpoint_ns == ns,
                AssistantCheckpointWrite.checkpoint_id == checkpoint_id,
            )
        ).all()
        return {
            (row.thread_id, row.checkpoint_ns, row.checkpoint_id, row.task_id, row.idx): {
                "channel": row.channel, "value_type": row.value_type,
                "value_data": row.value_data, "task_path": row.task_path,
            }
            for row in rows
        }

    def _tuple(self, key: tuple[str, str, str], fields: dict) -> CheckpointTuple:
        thread_id, ns, checkpoint_id = key
        writes = self._db_writes(thread_id, ns, checkpoint_id)
        writes.update({k: v for k, v in self._writes.items() if k[:3] == key})
        pending = [
            (k[3], v["channel"], self.serde.loads_typed((v["value_type"], v["value_data"])))
            for k, v in sorted(writes.items(), key=lambda item: (item[0][3], item[0][4]))
        ]
        parent_id = fields.get("parent_checkpoint_id")
        return CheckpointTuple(
            config={"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint_id}},
            checkpoint=self.serde.loads_typed((fields["checkpoint_type"], fields["checkpoint_data"])),
            metadata=self.serde.loads_typed((fields["metadata_type"], fields["metadata_data"])),
            parent_config=(
                {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": parent_id}}
                if parent_id else None
            ),
            pending_writes=pending,
        )

    @staticmethod
    def _row_fields(row: AssistantCheckpoint) -> dict:
        return {
            "parent_checkpoint_id": row.parent_checkpoint_id,
            "checkpoint_type": row.checkpoint_type, "checkpoint_data": row.checkpoint_data,
            "metadata_type": row.metadata_type, "metadata_data": row.metadata_data,
        }

    def get_tuple(self, config) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)
        with self._lock:
            buffered = {k: v for k, v in self._checkpoints.items() if k[0] == thread_id and k[1] == ns}
            if checkpoint_id:
                key = (thread_id, ns, checkpoint_id)
                if key in buffered:
                    return self._tuple(key, buffered[key])
                row = self.session.get(AssistantCheckpoint, key)
                return self._tuple(key, self._row_fields(row)) if row is not None else None
            if buffered:
                key = max(buffered)
                return self._tuple(key, buffered[key])
            row = self.session.scalar(
                select(AssistantCheckpoint)
                .where(AssistantCheckpoint.thread_id == thread_id, AssistantCheckpoint.checkpoint_ns == ns)
                .order_by(AssistantCheckpoint.checkpoint_id.desc())
                .limit(1)
            )
            if row is None:
                return None
            return self._tuple((row.thread_id, row.checkpoint_ns, row.checkpoint_id), self._row_fields(row))

    def list(
        self,
        config,
        *,
        filter: dict[str, Any] | None = None,
        before=None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        with self._lock:
            query = select(AssistantCheckpoint)
            thread_id = ns = checkpoint_id = None
            if config:
                thread_id = config["configurable"]["thread_id"]
                ns = config["configurable"].get("checkpoint_ns")
                checkpoint_id = get_checkpoint_id(config)
                query = query.where(AssistantCheckpoint.thread_id == thread_id)
                if ns is not None:
                    query = query.where(AssistantCheckpoint.checkpoint_ns == ns)
                if checkpoint_id:
                    query = query.where(AssistantCheckpoint.checkpoint_id == checkpoint_id)
            candidates = {
                (row.thread_id, row.checkpoint_ns, row.checkpoint_id): self._row_fields(row)
                for row in self.session.scalars(query)
            }
            for key, fields in self._checkpoints.items():
                if thread_id is not None and key[0] != thread_id:
                    continue
                if ns is not None and key[1] != ns:
                    continue
                if checkpoint_id and key[2] != checkpoint_id:
                    continue
                candidates[key] = fields
            before_id = get_checkpoint_id(before) if before is not None else None
            results = []
            for key in sorted(candidates, key=lambda item: item[2], reverse=True):
                if before_id and key[2] >= before_id:
                    continue
                fields = candidates[key]
                if filter:
                    metadata = self.serde.loads_typed((fields["metadata_type"], fields["metadata_data"]))
                    if not all(metadata.get(k) == v for k, v in filter.items()):
                        continue
                results.append(self._tuple(key, fields))
                if limit is not None and len(results) >= limit:
                    break
        yield from results

    # --- 写（只进缓冲，见模块说明） ------------------------------------------------------

    def put(
        self,
        config,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ):
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_type, checkpoint_data = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_data = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        with self._lock:
            self._checkpoints[(thread_id, ns, checkpoint["id"])] = {
                "parent_checkpoint_id": config["configurable"].get("checkpoint_id"),
                "checkpoint_type": checkpoint_type, "checkpoint_data": checkpoint_data,
                "metadata_type": metadata_type, "metadata_data": metadata_data,
            }
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint["id"]}}

    def put_writes(
        self,
        config,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        with self._lock:
            for position, (channel, value) in enumerate(writes):
                idx = WRITES_IDX_MAP.get(channel, position)
                key = (thread_id, ns, checkpoint_id, task_id, idx)
                # 与内存版一致：普通写入（非负下标）已存在就不覆盖；特殊通道（负下标）以最新为准。
                if idx >= 0 and key in self._writes:
                    continue
                value_type, value_data = self.serde.dumps_typed(value)
                self._writes[key] = {"channel": channel, "value_type": value_type,
                                     "value_data": value_data, "task_path": task_path}

    def persist(self) -> None:
        """把本次运行缓冲的快照与中间写入落库（在主线程、运行结束后调用）。"""

        with self._lock:
            for (thread_id, ns, checkpoint_id), fields in self._checkpoints.items():
                row = self.session.get(AssistantCheckpoint, (thread_id, ns, checkpoint_id))
                if row is None:
                    row = AssistantCheckpoint(thread_id=thread_id, checkpoint_ns=ns, checkpoint_id=checkpoint_id)
                    self.session.add(row)
                for name, value in fields.items():
                    setattr(row, name, value)
            for (thread_id, ns, checkpoint_id, task_id, idx), fields in self._writes.items():
                key = (thread_id, ns, checkpoint_id, task_id, idx)
                row = self.session.get(AssistantCheckpointWrite, key)
                if row is not None and idx >= 0:
                    continue
                if row is None:
                    row = AssistantCheckpointWrite(thread_id=thread_id, checkpoint_ns=ns, checkpoint_id=checkpoint_id,
                                                   task_id=task_id, idx=idx)
                    self.session.add(row)
                for name, value in fields.items():
                    setattr(row, name, value)
            self._checkpoints.clear()
            self._writes.clear()
            self.session.flush()

    # --- 清理（主线程调用，缓冲与库一起处理） ---------------------------------------------------

    def delete_thread(self, thread_id: str) -> None:
        with self._lock:
            self._checkpoints = {k: v for k, v in self._checkpoints.items() if k[0] != thread_id}
            self._writes = {k: v for k, v in self._writes.items() if k[0] != thread_id}
            self.session.execute(delete(AssistantCheckpointWrite).where(AssistantCheckpointWrite.thread_id == thread_id))
            self.session.execute(delete(AssistantCheckpoint).where(AssistantCheckpoint.thread_id == thread_id))
            self.session.flush()

    def delete_threads_with_prefix(self, prefix: str) -> None:
        """删除会话下的全部流程线程（线程编号形如 `<会话编号>:<流程序号>`）。"""

        pattern = prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        with self._lock:
            self._checkpoints = {k: v for k, v in self._checkpoints.items() if not k[0].startswith(prefix)}
            self._writes = {k: v for k, v in self._writes.items() if not k[0].startswith(prefix)}
            self.session.execute(
                delete(AssistantCheckpointWrite).where(AssistantCheckpointWrite.thread_id.like(pattern, escape="\\"))
            )
            self.session.execute(
                delete(AssistantCheckpoint).where(AssistantCheckpoint.thread_id.like(pattern, escape="\\"))
            )
            self.session.flush()

    def prune_thread(self, thread_id: str, *, keep: int) -> int:
        """只保留最近 `keep` 份快照（方案 M2），返回删除的份数。先落库再修剪。"""

        self.persist()
        ids = self.session.scalars(
            select(AssistantCheckpoint.checkpoint_id)
            .where(AssistantCheckpoint.thread_id == thread_id)
            .order_by(AssistantCheckpoint.checkpoint_id.desc())
        ).all()
        stale = list(ids[keep:])
        if not stale:
            return 0
        self.session.execute(
            delete(AssistantCheckpointWrite).where(
                AssistantCheckpointWrite.thread_id == thread_id,
                AssistantCheckpointWrite.checkpoint_id.in_(stale),
            )
        )
        self.session.execute(
            delete(AssistantCheckpoint).where(
                AssistantCheckpoint.thread_id == thread_id,
                AssistantCheckpoint.checkpoint_id.in_(stale),
            )
        )
        self.session.flush()
        return len(stale)

    def get_next_version(self, current: str | None, channel: None) -> str:
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(current.split(".")[0])
        return f"{current_v + 1:032}.{random.random():016}"
