"""评分助手：意图识别、会话与设置、LangGraph 流程与查询、状态存储、工具登记（全部离线）。"""

from datetime import timedelta

import pytest
from sqlalchemy import func
from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.db import models
from backend.app.services.assistant import intents
from backend.app.services.assistant import model as assistant_model
from backend.app.services.assistant import tools
from backend.app.services.assistant.checkpointer import SqlCheckpointSaver
from backend.app.tests import rubric_parse_fixtures as fx
from backend.app.tests.conftest import make_sample_docx

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _login(client, monkeypatch, username, *, role="org_admin"):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "bootstrap-admin-password")
    monkeypatch.setattr(settings, "AUTH_SECRET", "a" * 48)
    monkeypatch.setattr(settings, "REGISTRATION_MODE", "invite_only")
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "BYOK_MASTER_KEY", "test-only-master-key")
    email = "%s@example.test" % username
    password = "%s correct password" % username
    assert client.post("/api/auth/login", json={"username": "admin", "password": "bootstrap-admin-password"}).status_code == 204
    default_org_id = client.get("/api/organizations").json()[0]["id"]
    invitation = client.post(f"/api/organizations/{default_org_id}/members", json={"email": email, "role": "member"})
    assert invitation.status_code == 201, invitation.text
    assert client.post("/api/auth/register", json={
        "username": username, "email": email, "display_name": username, "password": password,
        "invitation_token": invitation.json()["invitation_token"],
    }).status_code == 201
    with client.session_factory() as session:
        user = session.scalar(select(models.User).where(models.User.username == username))
        organization = models.Organization(name="%s workspace" % username, created_by=user.id)
        session.add(organization)
        session.flush()
        session.add(models.OrganizationMember(organization_id=organization.id, user_id=user.id, role=role))
        session.commit()
        user_id, organization_id = user.id, organization.id
    assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 204
    assert client.post("/api/auth/organization-context", json={"organization_id": organization_id}).status_code == 200
    return user_id, organization_id


def _seed_published_rubric(client):
    from backend.app.scripts.seed_dev import seed

    with client.session_factory() as session:
        seed(session)
        rubric = session.scalar(select(models.Rubric).where(models.Rubric.status == "published"))
        return rubric.id, rubric.name


def _conversation(client):
    created = client.post("/api/assistant/conversations", json={})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _run(client, conversation_id, **body):
    response = client.post(f"/api/assistant/conversations/{conversation_id}/runs", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _say(client, conversation_id, text, **extra):
    return _run(client, conversation_id, type="message", text=text, **extra)


def _resume(client, conversation_id, pending, value):
    return _run(client, conversation_id, type="resume", card_id=pending["card_id"], value=value)


def _upload(client, batch_id, names):
    ids = []
    for name in names:
        response = client.post("/api/papers/upload", data={"batch_id": batch_id},
                               files={"file": (name, make_sample_docx(), DOCX)})
        assert response.status_code == 200, response.text
        ids.append(response.json()["id"])
    return ids


def _start_until_waiting_job(client, names=("张三.docx", "李四.docx")):
    """走到“等评分结束”：开始评分 → 选文件 → 确认 → 上传。"""

    conversation_id = _conversation(client)
    body = _say(client, conversation_id, "开始评分")
    assert body["pending"]["kind"] == "pick_files"
    body = _resume(client, conversation_id, body["pending"], {"count": len(names), "names": list(names)})
    assert body["pending"]["kind"] == "confirm_start"
    body = _resume(client, conversation_id, body["pending"], {"action": "confirm"})
    assert body["pending"]["kind"] == "upload_papers"
    batch_id = body["pending"]["client"]["batch_id"]
    paper_ids = _upload(client, batch_id, names)
    body = _resume(client, conversation_id, body["pending"], {"paper_ids": paper_ids})
    assert body["pending"]["kind"] == "wait_job", body
    return conversation_id, batch_id, body


def _run_job(client, job_id):
    """同步跑完作业。测试库是共享一个连接的内存 SQLite，多线程并发评分会互相踩，改成单线程。"""

    with client.session_factory() as session:
        session.get(models.BatchScoringJob, job_id).max_workers = 1
        session.commit()
    ran = client.post(f"/api/batch-scoring-jobs/{job_id}/run")
    assert ran.status_code == 200, ran.text
    assert {item["status"] for item in ran.json()["items"]} == {"succeeded"}, ran.json()["items"]


def _cards(body, card_type):
    return [card for message in body["messages"] for card in message["cards"] if card["type"] == card_type]


class _FakeScorer:
    provider = "fake"
    model_name = "fake-intent-model"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def complete_json(self, instructions, payload, **options):
        self.calls.append({"instructions": instructions, "payload": payload, "options": options})
        if self.error:
            raise self.error
        return self.result


# --- 意图识别（M1、T9） ------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "intent", "ordinal"),
    [
        ("第3篇为什么扣分", "query_paper", 3),
        ("第十二篇扣了哪些分", "query_paper", 12),
        ("第二十份呢", "query_paper", 20),
        ("第一百零五篇", "query_paper", 105),
        ("评到哪了", "query_progress", None),
        ("哪些需要复核", "query_review", None),
        ("这批的平均分是多少", "query_overview", None),
        ("把失败的重试一下", "retry_failed", None),
        ("取消评分", "cancel_job", None),
        ("我要上传自己的评分规则和模板", "import_rubric", None),
        ("帮我评一下这些论文", "start_grading", None),
        ("你能做什么", "help", None),
    ],
)
def test_rules_cover_the_common_phrasings_without_a_model(text, intent, ordinal):
    result = intents.interpret(text, scorer=None)
    assert result.intent == intent
    assert result.paper_ordinal == ordinal
    assert result.source == "rules"


def test_model_sees_only_this_sentence_recent_user_messages_and_names():
    scorer = _FakeScorer(result={"intent": "query_paper", "paper_ordinal": None,
                                 "paper_name": "张三", "clarify": None})
    ruled = intents.interpret("第3篇为什么扣分", scorer=scorer)
    assert ruled.source == "rules" and scorer.calls == []

    recent = ["开始评分", "第一篇呢", "第二篇呢", "整体情况"]
    result = intents.interpret("张三那篇怎么样", focus={"batch_name": "十月批次"}, scorer=scorer, recent=recent)
    assert (result.intent, result.paper_name, result.source) == ("query_paper", "张三", "model")
    payload = scorer.calls[0]["payload"]
    assert set(payload) == {"intents", "text", "recent_user_messages", "context"}
    assert payload["recent_user_messages"] == recent[-3:]
    assert payload["context"] == {"current_task": "十月批次", "current_rubric": None}
    assert scorer.calls[0]["options"]["attempts_limit"] == 1


@pytest.mark.parametrize(
    "raw",
    [
        {"intent": "delete_everything", "paper_ordinal": None, "paper_name": None, "clarify": None},
        {"intent": "query_paper", "paper_ordinal": "3", "paper_name": None, "clarify": None},
        "not an object",
    ],
)
def test_invalid_model_output_is_treated_as_not_understood(raw):
    result = intents.interpret("张三那篇怎么样", scorer=_FakeScorer(result=raw))
    assert result.intent == "unknown"
    assert result.error_code == "ASSISTANT_MODEL_OUTPUT_INVALID"


def test_model_failure_never_raises_and_mock_is_not_used():
    assert intents.interpret("张三那篇怎么样", scorer=_FakeScorer(error=RuntimeError("boom"))).error_code == "ASSISTANT_MODEL_FAILED"

    class Mock(_FakeScorer):
        provider = "mock"

    scorer = Mock(result={"intent": "help"})
    assert intents.interpret("随便聊聊", scorer=scorer).intent == "unknown"
    assert scorer.calls == []


# --- 工具登记（T14） ------------------------------------------------------------------

def test_tools_are_registered_with_schema_and_write_tools_need_explicit_permission():
    registry = tools.registry()
    assert {"batch_summary", "latest_job", "create_batch", "start_scoring", "retry_scoring"} <= set(registry)
    assert registry["batch_summary"].read_only is True
    assert registry["create_batch"].read_only is False
    assert registry["create_batch"].schema()["properties"]["rubric_id"]["type"] == "string"
    with pytest.raises(tools.ToolNotAllowed):
        tools.call(object(), "create_batch", name="x", rubric_id="r")


def test_route_guards_are_the_shared_guard_functions():
    from backend.app.api import guards
    from backend.app.api.routes import batch_jobs, batches, papers, rubrics, scoring

    assert batches._visible_batch is guards.visible_batch
    assert papers._visible_batch is guards.visible_batch
    assert papers._visible_paper is guards.visible_paper
    assert scoring._visible_run is guards.visible_run
    assert batch_jobs._job_or_404 is guards.visible_job
    assert rubrics._visible_import_session is guards.visible_import_session


# --- 状态存储（T3、M2） ----------------------------------------------------------------

def test_sql_checkpointer_round_trips_and_prunes(client):
    from langgraph.checkpoint.base import empty_checkpoint

    with client.session_factory() as session:
        saver = SqlCheckpointSaver(session)
        config = {"configurable": {"thread_id": "c1:1", "checkpoint_ns": ""}}
        for step in range(13):
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"] = {"batch_id": "b-%d" % step}
            config = saver.put(config, checkpoint, {"step": step}, {})
        saver.put_writes(config, [("batch_id", "b-x")], "task-1")
        saver.put_writes(config, [("batch_id", "b-y")], "task-1")  # 同下标不覆盖
        latest = saver.get_tuple({"configurable": {"thread_id": "c1:1"}})
        assert latest.checkpoint["channel_values"] == {"batch_id": "b-12"}
        assert latest.metadata["step"] == 12
        assert latest.pending_writes == [("task-1", "batch_id", "b-x")]
        assert len(list(saver.list({"configurable": {"thread_id": "c1:1"}}, limit=5))) == 5
        assert saver.prune_thread("c1:1", keep=10) == 3
        assert session.scalar(select(func.count()).select_from(models.AssistantCheckpoint)) == 10
        saver.delete_threads_with_prefix("c1:")
        assert saver.get_tuple({"configurable": {"thread_id": "c1:1"}}) is None
        assert session.scalar(select(func.count()).select_from(models.AssistantCheckpointWrite)) == 0


# --- 场景 A：完整流程（开发模式，Mock 评分） ------------------------------------------------

def test_full_grading_flow_from_files_to_report(client):
    rubric_id, rubric_name = _seed_published_rubric(client)
    conversation_id = _conversation(client)

    body = _say(client, conversation_id, "开始评分")
    assert body["pending"]["kind"] == "pick_files"
    assert body["pending"]["client"] == {"type": "pick_files", "max": 30}

    body = _resume(client, conversation_id, body["pending"], {"count": 2, "names": ["张三.docx", "李四.docx"]})
    confirm = _cards(body, "confirm_start")[0]
    assert confirm["rubric_id"] == rubric_id and confirm["file_count"] == 2
    assert rubric_name in body["messages"][-1]["text"]
    assert body["pending"] == {**body["pending"], "kind": "confirm_start", "card_id": confirm["id"]}

    body = _resume(client, conversation_id, body["pending"], {"action": "confirm"})
    batch_id = body["pending"]["client"]["batch_id"]
    assert body["conversation"]["focus"]["workspace"] == "/tasks/new?batch=%s" % batch_id

    paper_ids = _upload(client, batch_id, ["张三.docx", "李四.docx"])
    body = _resume(client, conversation_id, body["pending"], {"paper_ids": paper_ids})
    job_id = body["pending"]["client"]["job_id"]
    assert body["pending"]["kind"] == "wait_job"
    assert body["conversation"]["focus"]["workspace"] == "/tasks/%s/run" % batch_id

    _run_job(client, job_id)
    body = _resume(client, conversation_id, body["pending"], {"event": "job_finished"})
    assert body["pending"] is None
    report = body["messages"][-1]
    assert report["text"].startswith("评分完成。共 2 份，已出分 2 份")
    assert [card["type"] for card in report["cards"]] == ["overview", "quick_actions"]
    assert body["conversation"]["focus"]["workspace"] == "/batches/%s/grade" % batch_id

    with client.session_factory() as session:
        conversation = session.get(models.AssistantConversation, conversation_id)
        assert conversation.thread_id is None
        assert session.scalar(select(func.count()).select_from(models.AssistantCheckpoint)) <= 10
        assert session.scalar(select(func.count()).select_from(models.GradingBatch).where(
            models.GradingBatch.id == batch_id)) == 1


def test_attachments_go_straight_to_the_key_confirmation(client):
    _seed_published_rubric(client)
    conversation_id = _conversation(client)
    body = _run(client, conversation_id, type="message", attachments={"count": 3, "names": ["a.docx", "b.docx", "c.docx"]})
    assert body["pending"]["kind"] == "confirm_start"
    # 前端靠线程编号把内存里的文件归到这次流程，响应里不能丢。
    assert body["pending"]["thread_id"] == "%s:1" % conversation_id
    assert _cards(body, "confirm_start")[0]["file_count"] == 3
    assert body["messages"][0]["text"] == "评分这 3 份文件"


def test_job_finished_is_verified_on_the_server(client):
    _seed_published_rubric(client)
    conversation_id, _batch_id, body = _start_until_waiting_job(client)
    # 浏览器说“评完了”，但作业还没跑：继续等，不汇报。
    again = _resume(client, conversation_id, body["pending"], {"event": "job_finished"})
    assert again["pending"]["kind"] == "wait_job"
    assert not any("评分完成" in message["text"] for message in again["messages"])


def test_uploaded_papers_must_belong_to_the_batch(client):
    rubric_id, _ = _seed_published_rubric(client)
    other_batch = client.post("/api/batches", json={"name": "别的任务", "rubric_id": rubric_id}).json()["id"]
    foreign = _upload(client, other_batch, ["外人.docx"])
    conversation_id = _conversation(client)
    body = _say(client, conversation_id, "开始评分")
    body = _resume(client, conversation_id, body["pending"], {"count": 1, "names": ["外人.docx"]})
    body = _resume(client, conversation_id, body["pending"], {"action": "confirm"})
    body = _resume(client, conversation_id, body["pending"], {"paper_ids": foreign})
    assert body["pending"] is None
    assert "校验失败" in body["messages"][-1]["text"]


def test_stale_cards_and_malformed_values_are_rejected(client):
    _seed_published_rubric(client)
    conversation_id = _conversation(client)
    body = _say(client, conversation_id, "开始评分")
    stale = client.post(f"/api/assistant/conversations/{conversation_id}/runs",
                        json={"type": "resume", "card_id": "not-the-card", "value": {"count": 1}})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "ASSISTANT_CARD_STALE"
    too_many = client.post(f"/api/assistant/conversations/{conversation_id}/runs",
                           json={"type": "resume", "card_id": body["pending"]["card_id"], "value": {"count": 31}})
    assert too_many.status_code == 422


def test_second_run_while_locked_is_refused(client):
    conversation_id = _conversation(client)
    with client.session_factory() as session:
        conversation = session.get(models.AssistantConversation, conversation_id)
        conversation.run_locked_until = models.utcnow() + timedelta(seconds=60)
        session.commit()
    response = client.post(f"/api/assistant/conversations/{conversation_id}/runs", json={"type": "message", "text": "评分进度"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ASSISTANT_RUN_IN_PROGRESS"


def test_queries_while_waiting_do_not_disturb_the_flow(client):
    _seed_published_rubric(client)
    conversation_id, batch_id, body = _start_until_waiting_job(client)
    pending = body["pending"]

    progress = _say(client, conversation_id, "评分进度")
    assert progress["pending"] == pending
    assert _cards(progress, "job_progress")[0]["status"] == "info"

    another = _say(client, conversation_id, "开始评分")
    assert another["pending"] == pending
    assert "新开一个会话" in another["messages"][-1]["text"]


def test_starting_again_abandons_an_unconfirmed_flow(client):
    _seed_published_rubric(client)
    conversation_id = _conversation(client)
    first = _run(client, conversation_id, type="message", attachments={"count": 1, "names": ["a.docx"]})
    old_card = first["pending"]["card_id"]
    second = _say(client, conversation_id, "开始评分")
    assert second["pending"]["kind"] == "pick_files"
    dismissed = [card for message in second["messages"] for card in message["cards"] if card["id"] == old_card]
    assert dismissed and dismissed[0]["status"] == "dismissed"
    with client.session_factory() as session:
        assert session.get(models.AssistantConversation, conversation_id).flow_seq == 2


def test_waiting_for_the_user_expires_after_seven_days(client):
    _seed_published_rubric(client)
    conversation_id = _conversation(client)
    body = _say(client, conversation_id, "开始评分")
    card_id = body["pending"]["card_id"]
    with client.session_factory() as session:
        conversation = session.get(models.AssistantConversation, conversation_id)
        conversation.pending = {**conversation.pending,
                                "created_at": (models.utcnow() - timedelta(days=8)).isoformat()}
        session.commit()

    detail = client.get(f"/api/assistant/conversations/{conversation_id}").json()
    assert detail["pending"] is None
    assert "超过 7 天" in detail["messages"][-1]["text"]
    expired = [card for message in detail["messages"] for card in message["cards"] if card["id"] == card_id]
    assert expired[0]["status"] == "expired"


def test_transient_failures_are_retried_once_then_diagnosed(client):
    _seed_published_rubric(client)
    conversation_id, batch_id, body = _start_until_waiting_job(client)
    job_id = body["pending"]["client"]["job_id"]

    def fail_all():
        with client.session_factory() as session:
            job = session.get(models.BatchScoringJob, job_id)
            for item in job.items:
                item.status = "failed"
                item.error_code = "PROVIDER_RATE_LIMITED"
            job.status = "completed_with_errors"
            job.pending_count = job.running_count = job.succeeded_count = 0
            job.failed_count = len(job.items)
            session.commit()

    fail_all()
    retried = _resume(client, conversation_id, body["pending"], {"event": "job_finished"})
    assert retried["pending"]["kind"] == "wait_job"
    assert "已自动重试一次" in retried["messages"][-1]["text"]

    fail_all()
    final = _resume(client, conversation_id, retried["pending"], {"event": "job_finished"})
    assert final["pending"] is None
    diagnosis = _cards(final, "diagnosis")[0]
    assert diagnosis["status"] == "proposed"
    assert diagnosis["groups"][0]["code"] == "RATE_LIMITED"

    # 用户点“重试失败项”：确认后执行，并由观察流程接着等结果。
    watched = _run(client, conversation_id, type="select", card_id=diagnosis["id"], value={"action": "retry"})
    assert watched["pending"]["kind"] == "wait_job"


def test_query_paper_by_ordinal_and_choice(client):
    _seed_published_rubric(client)
    conversation_id, batch_id, body = _start_until_waiting_job(client, names=("张三.docx", "李四.docx"))
    job_id = body["pending"]["client"]["job_id"]
    _run_job(client, job_id)

    second = _say(client, conversation_id, "第2篇为什么扣分")
    result = _cards(second, "paper_result")[0]
    assert result["batch_id"] == batch_id
    assert "第 2 份" in second["messages"][-1]["text"]
    assert second["conversation"]["focus"]["workspace"] == "/batches/%s/grade?paper=%s" % (batch_id, result["paper_id"])

    choose = _say(client, conversation_id, "第9篇呢")
    card = _cards(choose, "choose_paper")[0]
    picked = _run(client, conversation_id, type="select", card_id=card["id"],
                  value={"paper_id": card["papers"][0]["paper_id"]})
    assert _cards(picked, "paper_result")
    again = client.post(f"/api/assistant/conversations/{conversation_id}/runs",
                        json={"type": "select", "card_id": card["id"], "value": {"paper_id": card["papers"][0]["paper_id"]}})
    assert again.status_code == 409


def test_import_flow_waits_for_publication_then_asks_for_files(client):
    rubric_id, _ = _seed_published_rubric(client)
    conversation_id = _conversation(client)
    body = _say(client, conversation_id, "我要上传自己的评分规则和模板")
    assert body["pending"]["kind"] == "create_import_session"
    session = client.post("/api/rubrics/import-sessions", data={"name": "我的标准", "version": "v1"},
                          files={"rules_file": ("rules.xlsx", fx.simple_rules_xlsx(), XLSX)})
    assert session.status_code == 201, session.text
    session_id = session.json()["id"]
    body = _resume(client, conversation_id, body["pending"], {"import_session_id": session_id})
    assert body["pending"]["kind"] == "wait_publish"
    assert body["conversation"]["focus"]["workspace"] == "/rubrics?import_session=%s" % session_id

    still = _resume(client, conversation_id, body["pending"], {"action": "check"})
    assert still["pending"]["kind"] == "wait_publish"

    # 模拟用户在工作区确认并发布（发布全流程由评分标准页的测试覆盖）。
    with client.session_factory() as db:
        row = db.get(models.RubricImportSession, session_id)
        row.status = "confirmed"
        row.rubric_id = rubric_id
        db.commit()
    published = _resume(client, conversation_id, still["pending"], {"action": "check"})
    assert published["pending"]["kind"] == "pick_files"
    assert "已发布" in published["messages"][-2]["text"]


def test_delete_conversation_removes_flow_state(client):
    _seed_published_rubric(client)
    conversation_id = _conversation(client)
    _say(client, conversation_id, "开始评分")
    with client.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(models.AssistantCheckpoint)) > 0
    assert client.delete(f"/api/assistant/conversations/{conversation_id}").status_code == 204
    with client.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(models.AssistantCheckpoint)) == 0
        assert session.scalar(select(func.count()).select_from(models.AssistantMessage)) == 0


def test_help_unknown_and_no_rubric_replies(client):
    conversation_id = _conversation(client)
    assert "我可以帮你完成整个评分" in _say(client, conversation_id, "你能做什么")["messages"][-1]["text"]
    unknown = _say(client, conversation_id, "今天天气怎么样")
    assert "没理解" in unknown["messages"][-1]["text"]
    no_rubric = _run(client, conversation_id, type="message", attachments={"count": 1, "names": ["a.docx"]})
    assert no_rubric["pending"] is None
    assert "还没有已发布的评分标准" in no_rubric["messages"][-1]["text"]


# --- 鉴权、归属与助手模型 -------------------------------------------------------------

def test_conversations_are_private_to_their_owner(client, monkeypatch):
    _login(client, monkeypatch, "assistant-owner")
    conversation_id = _conversation(client)
    _say(client, conversation_id, "你能做什么")

    client.post("/api/auth/logout")
    _login(client, monkeypatch, "assistant-other")
    assert client.get("/api/assistant/conversations").json() == []
    assert client.get(f"/api/assistant/conversations/{conversation_id}").status_code == 404
    assert client.post(f"/api/assistant/conversations/{conversation_id}/runs",
                       json={"type": "message", "text": "进度"}).status_code == 404
    assert client.delete(f"/api/assistant/conversations/{conversation_id}").status_code == 404


def test_members_without_teacher_role_cannot_use_the_assistant(client, monkeypatch):
    _login(client, monkeypatch, "assistant-member", role="member")
    assert client.get("/api/system/capabilities").json()["abilities"]["use_assistant"] is False
    assert client.get("/api/assistant/conversations").status_code == 403
    assert client.get("/api/assistant/settings").status_code == 403


def test_settings_fall_back_from_preference_to_own_connection_to_platform(client, monkeypatch):
    user_id, organization_id = _login(client, monkeypatch, "assistant-settings")

    initial = client.get("/api/assistant/settings").json()
    assert initial["configured"] is False
    assert initial["effective"]["source"] == "none"
    assert client.put("/api/assistant/settings", json={"model_source": "platform"}).status_code == 422

    monkeypatch.setattr(assistant_model, "platform_available", lambda db: True)
    platform = client.get("/api/assistant/settings").json()
    assert platform["effective"]["source"] == "platform"
    assert platform["effective"]["slow"] is True
    assert "非常慢" in platform["effective"]["notice"]

    created = client.post("/api/ai-connections", json={
        "name": "我的模型", "provider_type": "openai_responses",
        "base_url": "https://api.openai.com/v1", "model_name": "gpt-4.1-mini",
        "provider_options": {"timeout_seconds": 20}, "api_key": "sk-live-secret-key-7H2K",
    })
    assert created.status_code == 201, created.text
    connection_id = created.json()["id"]
    byok = client.get("/api/assistant/settings").json()
    assert byok["effective"] == {"source": "connection", "connection_id": connection_id,
                                 "label": "我的模型 · gpt-4.1-mini", "slow": False, "notice": None}

    assert client.put("/api/assistant/settings", json={"model_source": "platform"}).json()["configured"] is True
    assert client.put("/api/assistant/settings",
                      json={"model_source": "connection", "ai_connection_id": "not-mine"}).status_code == 422
    with client.session_factory() as session:
        rows = session.scalars(select(models.AssistantPreference)).all()
        assert [(row.user_id, row.organization_id) for row in rows] == [(user_id, organization_id)]


def test_unrecognized_text_uses_the_assistant_model(client, monkeypatch):
    _login(client, monkeypatch, "assistant-model")
    scorer = _FakeScorer(result={"intent": "help", "paper_ordinal": None, "paper_name": None, "clarify": None})
    monkeypatch.setattr(assistant_model, "build_scorer", lambda *args, **kwargs: scorer)
    conversation_id = _conversation(client)
    _say(client, conversation_id, "评分进度")
    body = _say(client, conversation_id, "这一批同学写得怎么样")
    user_message = body["messages"][0]
    assert user_message["intent"] == "help" and user_message["model_name"] == "fake-intent-model"
    assert scorer.calls[0]["payload"]["recent_user_messages"] == ["评分进度"]
    assert body["conversation"]["focus"] == {} or "workspace" not in body["conversation"]["focus"]
    assert client.get(f"/api/assistant/conversations/{conversation_id}").headers["cache-control"] == "private, no-store"
