"""评分助手的发送去重（docs/评分助手发送去重方案.md）：同一会话里同一个请求编号只处理一次。全部离线。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy import text

from backend.app.core.config import settings
from backend.app.db import models
from backend.app.services.assistant import runner
from backend.app.services.assistant import stream
from backend.app.tests.test_assistant import _conversation
from backend.app.tests.test_assistant import _run
from backend.app.tests.test_assistant import _seed_published_rubric
from backend.app.tests.test_assistant_agent import agent  # noqa: F401  (pytest fixture)
from backend.app.tests.test_assistant_agent import reply

ROOT = Path(__file__).resolve().parents[3]
CLIENT_ID = "6f1d2c3b-0a9e-4c7d-8b6a-5e4f3d2c1b0a"


def _send(client, conversation_id, text_, client_id=CLIENT_ID):
    body = {"type": "message", "text": text_}
    if client_id is not None:
        body["client_id"] = client_id
    return _run(client, conversation_id, **body)


def _user_messages(client, conversation_id):
    with client.session_factory() as session:
        return session.scalars(
            select(models.AssistantMessage)
            .where(models.AssistantMessage.conversation_id == conversation_id, models.AssistantMessage.role == "user")
        ).all()


def test_the_same_client_id_is_handled_once_and_replays_the_first_round(client):
    _seed_published_rubric(client)
    conversation_id = _conversation(client)

    first = _send(client, conversation_id, "开始评分")
    second = _send(client, conversation_id, "开始评分")

    assert [message["id"] for message in second["messages"]] == [message["id"] for message in first["messages"]]
    assert second["messages"][0]["client_id"] == CLIENT_ID and second["messages"][0]["role"] == "user"
    assert second["pending"]["kind"] == "pick_files"
    assert len(_user_messages(client, conversation_id)) == 1
    # 读会话时老师消息带着编号，前端按它对应。
    detail = client.get(f"/api/assistant/conversations/{conversation_id}").json()
    assert [message["client_id"] for message in detail["messages"] if message["role"] == "user"] == [CLIENT_ID]


def test_a_replay_never_runs_the_agent_turn_again(client, agent):  # noqa: F811
    model = agent(reply("你好，有什么要帮忙的？"))
    conversation_id = _conversation(client)

    first = _send(client, conversation_id, "你好")
    second = _send(client, conversation_id, "你好")  # 脚本里只有一步：再调用模型会抛 AssertionError

    assert len(model.requests) == 1
    assert [message["text"] for message in second["messages"]] == ["你好", "你好，有什么要帮忙的？"]
    assert [message["id"] for message in second["messages"]] == [message["id"] for message in first["messages"]]


def test_a_replay_stops_at_the_next_teacher_message(client):
    conversation_id = _conversation(client)
    _send(client, conversation_id, "你好")
    _send(client, conversation_id, "今天天气怎么样", client_id="0d9c8b7a-6f5e-4d3c-2b1a-0f9e8d7c6b5a")

    replay = _send(client, conversation_id, "你好")

    assert [message["role"] for message in replay["messages"]] == ["user", "assistant"]
    assert replay["messages"][0]["text"] == "你好"


def test_client_ids_are_per_conversation_and_old_pages_are_not_deduplicated(client):
    first, second = _conversation(client), _conversation(client)
    _send(client, first, "你好")
    _send(client, second, "你好")
    assert len(_user_messages(client, first)) == 1 and len(_user_messages(client, second)) == 1

    # 旧页面不带编号：照常处理，每次都新建。
    _send(client, first, "你好", client_id=None)
    _send(client, first, "你好", client_id=None)
    assert len(_user_messages(client, first)) == 3


def test_the_same_id_with_other_text_replays_the_first_and_logs_a_warning(client, caplog):
    conversation_id = _conversation(client)
    _send(client, conversation_id, "你好")
    with caplog.at_level(logging.WARNING, logger="backend.app.services.assistant.runner"):
        replay = _send(client, conversation_id, "换一句话")
    assert replay["messages"][0]["text"] == "你好"
    assert "assistant_run_replay_text_mismatch" in caplog.text
    assert len(_user_messages(client, conversation_id)) == 1


def test_a_unique_index_conflict_falls_back_to_the_replay(client, monkeypatch):
    """两次同编号请求都越过了运行锁：后插入的被唯一索引拒绝，回放先存下的那一次。"""

    conversation_id = _conversation(client)
    first = _send(client, conversation_id, "你好")
    real = runner._replay
    calls = {"n": 0}

    def miss_once(ctx, client_id, text_):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(ctx, client_id, text_)

    monkeypatch.setattr(runner, "_replay", miss_once)
    second = _send(client, conversation_id, "你好")

    assert calls["n"] == 2
    assert [message["id"] for message in second["messages"]] == [message["id"] for message in first["messages"]]
    assert len(_user_messages(client, conversation_id)) == 1
    assert client.get(f"/api/assistant/conversations/{conversation_id}").json()["running"] is False


def test_the_stream_replays_with_only_accepted_and_done(client, monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_STREAM_ENABLED", True)
    conversation_id = _conversation(client)
    first = _send(client, conversation_id, "你好")

    with client.stream("POST", f"/api/assistant/conversations/{conversation_id}/runs/stream",
                       json={"type": "message", "text": "你好", "client_id": CLIENT_ID}) as response:
        lines = list(response.iter_lines())
    stream.wait_idle()

    events = [line[len("event: "):] for line in lines if line.startswith("event: ")]
    assert events == ["accepted", "done"]
    done = json.loads(next(line for line in reversed(lines) if line.startswith("data: "))[len("data: "):])
    assert [message["id"] for message in done["messages"]] == [message["id"] for message in first["messages"]]


@pytest.mark.parametrize("client_id", ["short", "has space-1234", "a" * 65, "中文编号中文编号"])
def test_malformed_client_ids_are_refused(client, client_id):
    conversation_id = _conversation(client)
    response = client.post(f"/api/assistant/conversations/{conversation_id}/runs",
                           json={"type": "message", "text": "你好", "client_id": client_id})
    assert response.status_code == 422


def test_0042_refuses_a_lossy_downgrade_and_the_index_rejects_duplicates(monkeypatch, tmp_path):
    url = "sqlite+pysqlite:///%s" % (tmp_path / "client-id.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "0042_assistant_message_client_id")
    engine = create_engine(url)
    insert = text(
        "INSERT INTO assistant_messages (id, conversation_id, role, text, cards, client_id, created_at) "
        "VALUES (:id, 'c1', 'user', 'hi', '[]', :client_id, CURRENT_TIMESTAMP)"
    )
    with engine.begin() as connection:
        # SQLite 默认不检查外键：会话行不需要真的存在。没有编号的行可以有多条。
        connection.execute(insert, {"id": "m1", "client_id": CLIENT_ID})
        connection.execute(insert, {"id": "m2", "client_id": None})
        connection.execute(insert, {"id": "m3", "client_id": None})
    with pytest.raises(Exception, match="UNIQUE"):
        with engine.begin() as connection:
            connection.execute(insert, {"id": "m4", "client_id": CLIENT_ID})
    with pytest.raises(RuntimeError, match="0042 downgrade"):
        command.downgrade(config, "0041_assistant_agent_state")
    with engine.begin() as connection:
        connection.execute(text("UPDATE assistant_messages SET client_id = NULL"))
    command.downgrade(config, "0041_assistant_agent_state")
    engine.dispose()
