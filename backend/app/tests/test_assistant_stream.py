"""评分助手的过程流式（docs/评分助手过程流式方案.md）：全部离线，脚本化的假模型代替真实模型。"""

from __future__ import annotations

import json
import threading
from datetime import timedelta

import pytest

from backend.app.core.config import settings
from backend.app.db import models
from backend.app.db.models import utcnow
from backend.app.services.assistant import runner
from backend.app.services.assistant import stream
from backend.app.tests.test_assistant import _conversation
from backend.app.tests.test_assistant import _say
from backend.app.tests.test_assistant import _seed_published_rubric
from backend.app.tests.test_assistant_agent import ScriptedModel
from backend.app.tests.test_assistant_agent import agent  # noqa: F401  (pytest fixture)
from backend.app.tests.test_assistant_agent import reply
from backend.app.tests.test_assistant_agent import step


@pytest.fixture
def stream_on(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_STREAM_ENABLED", True)
    yield
    stream.wait_idle()


def _parse(lines) -> list[tuple[str, object]]:
    events, kind, data = [], None, []
    for line in lines:
        if line.startswith(":"):
            continue
        if line.startswith("event: "):
            kind = line[len("event: "):]
        elif line.startswith("data: "):
            data.append(line[len("data: "):])
        elif line == "" and kind:
            events.append((kind, json.loads("\n".join(data)) if data else None))
            kind, data = None, []
    return events


def _stream(client, conversation_id, **body):
    with client.stream("POST", f"/api/assistant/conversations/{conversation_id}/runs/stream", json=body) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "private, no-store"
        events = _parse(response.iter_lines())
    stream.wait_idle()
    return events


def _labels(events):
    return [data["label"] for kind, data in events if kind == "progress"]


def _list_rubrics_then(*steps):
    return (step(("tool_search", {"query": "select:rubric_list"})),
            step(("call_tool", {"name": "rubric_list", "arguments_json": "{}"})), *steps)


def test_stream_is_off_by_default(client):
    conversation_id = _conversation(client)
    response = client.post(f"/api/assistant/conversations/{conversation_id}/runs/stream",
                           json={"type": "message", "text": "你好"})
    assert response.status_code == 404
    assert client.get("/api/assistant/settings").json()["stream_enabled"] is False


def test_settings_tell_the_page_to_use_the_stream(client, stream_on):
    assert client.get("/api/assistant/settings").json()["stream_enabled"] is True


def test_agent_turn_pushes_progress_then_the_same_body_as_runs(client, agent, stream_on):  # noqa: F811
    _seed_published_rubric(client)
    agent(*_list_rubrics_then(reply("目前已发布的评分标准有：", cards=["r1"])))
    conversation_id = _conversation(client)

    events = _stream(client, conversation_id, type="message", text="有哪些评分标准？")

    assert [kind for kind, _data in events] == ["accepted", "progress", "progress", "done"]
    assert _labels(events) == ["正在理解你的问题…", "正在查：可用的评分标准…"]
    assert [data["step"] for kind, data in events if kind == "progress"] == [1, 2]
    done = events[-1][1]
    assert set(done) == {"messages", "pending", "conversation"}
    assert [message["role"] for message in done["messages"]] == ["user", "assistant"]
    assert done["messages"][0]["text"] == "有哪些评分标准？"
    assert done["messages"][1]["text"].startswith("目前已发布的评分标准有：\n· 《")
    assert done["conversation"]["running"] is False
    # 进度文字只来自代码的表：模型写的字不会出现在进度里。
    assert not any("目前已发布" in label for label in _labels(events))
    # 和重新读会话看到的一样：已经落库，锁已释放。
    detail = client.get(f"/api/assistant/conversations/{conversation_id}").json()
    assert [message["id"] for message in detail["messages"]] == [message["id"] for message in done["messages"]]
    assert detail["running"] is False


def test_rules_mode_and_quick_buttons_have_no_progress(client, stream_on):
    _seed_published_rubric(client)
    conversation_id = _conversation(client)
    events = _stream(client, conversation_id, type="message", text="开始评分")
    assert [kind for kind, _data in events] == ["accepted", "done"]
    assert events[-1][1]["pending"]["kind"] == "pick_files"


def test_a_rejected_reply_shows_checking(client, agent, stream_on):  # noqa: F811
    agent(reply("看这里。", cards=["t9"]), reply("好的，我没找到相关的任务。"))
    events = _stream(client, _conversation(client), type="message", text="给我看看")
    assert _labels(events) == ["正在理解你的问题…", "正在核对回复…"]
    assert events[-1][1]["messages"][-1]["text"] == "好的，我没找到相关的任务。"


def test_refusals_come_before_the_stream_starts(client, stream_on):
    conversation_id = _conversation(client)
    with client.session_factory() as session:
        conversation = session.get(models.AssistantConversation, conversation_id)
        conversation.run_locked_until = utcnow() + timedelta(seconds=60)
        session.commit()
    # 被别的运行占着：直接 409，不开始推送。读会话时能看到它正在运行。
    response = client.post(f"/api/assistant/conversations/{conversation_id}/runs/stream",
                           json={"type": "message", "text": "你好"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ASSISTANT_RUN_IN_PROGRESS"
    assert client.get(f"/api/assistant/conversations/{conversation_id}").json()["running"] is True

    response = client.post("/api/assistant/conversations/not-mine/runs/stream", json={"type": "message", "text": "你好"})
    assert response.status_code == 404


def test_errors_inside_the_run_are_pushed_and_the_lock_is_released(client, stream_on, monkeypatch):
    conversation_id = _conversation(client)
    # 卡片已失效：运行里的 409 变成 error 事件（推送已经开始，状态码是 200）。
    events = _stream(client, conversation_id, type="resume", card_id="gone", value={})
    assert events[-1] == ("error", {"status": 409, "detail": {"code": "ASSISTANT_CARD_STALE",
                                                               "message": "这张卡片已经失效，请看最新的消息。"}})

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "_handle_message", boom)
    events = _stream(client, conversation_id, type="message", text="你好")
    assert events[-1] == ("error", {"status": 500, "detail": stream.FAILED_DETAIL})
    assert client.get(f"/api/assistant/conversations/{conversation_id}").json()["running"] is False


def test_the_run_finishes_even_when_the_page_goes_away(client, monkeypatch, stream_on):
    """推出第一个事件响应就结束（老师关了页面）：后台线程照样跑完、提交、释放锁，重新打开会话能看到回复。

    TestClient 要等整个响应生成完才交出内容，测不了“读一半断开”；这里把响应生成器换成推出第一条就结束，
    验证的是同一件事：回合跑不跑完，和有没有人在读响应无关。
    """

    gate = threading.Event()

    async def hang_up(queue, conversation_id):
        yield stream.sse("accepted", {})

    monkeypatch.setattr(stream, "_events", hang_up)

    class SlowModel(ScriptedModel):
        def complete_with_tools(self, messages, tools, **options):
            assert gate.wait(10)
            return super().complete_with_tools(messages, tools, **options)

    from backend.app.services.assistant import model as assistant_model

    monkeypatch.setattr(settings, "ASSISTANT_AGENT_ENABLED", True)
    slow = SlowModel(reply("好的，这就帮你看看。"))
    monkeypatch.setattr(assistant_model, "build_scorer", lambda *args, **kwargs: slow)
    conversation_id = _conversation(client)

    response = client.post(f"/api/assistant/conversations/{conversation_id}/runs/stream",
                           json={"type": "message", "text": "这批写得怎么样"})
    assert response.status_code == 200
    assert _parse(response.text.split("\n")) == [("accepted", {})]
    # 回合进行中不在这里读会话：测试库是单连接的内存库，另一个请求结束时的回滚会落到后台线程的事务上
    # （生产是连接池，各用各的连接）。“锁被占时 running 为 true”见上一个测试。
    gate.set()  # 响应已经结束，模型这时才返回
    stream.wait_idle()

    detail = client.get(f"/api/assistant/conversations/{conversation_id}").json()
    assert [message["text"] for message in detail["messages"]] == ["这批写得怎么样", "好的，这就帮你看看。"]
    assert detail["running"] is False
    # 锁已释放：接着说话不会 409（_say 断言 200）。
    assert _say(client, conversation_id, "开始评分")["messages"]
