from sqlalchemy import select
import pytest

from backend.app.core.config import settings
from backend.app.db import models


def _login(client, monkeypatch, username):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "bootstrap-admin-password")
    monkeypatch.setattr(settings, "AUTH_SECRET", "r" * 48)
    monkeypatch.setattr(settings, "REGISTRATION_MODE", "invite_only")
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "BYOK_MASTER_KEY", "test-only-master-key")
    password = "%s correct password" % username
    email = "%s@example.test" % username
    assert client.post("/api/auth/login", json={"username": "admin", "password": "bootstrap-admin-password"}).status_code == 204
    default_org_id = client.get("/api/organizations").json()[0]["id"]
    invitation = client.post(f"/api/organizations/{default_org_id}/members", json={"email": email, "role": "member"})
    assert invitation.status_code == 201, invitation.text
    assert client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": email,
            "display_name": username,
            "password": password,
            "invitation_token": invitation.json()["invitation_token"],
        },
    ).status_code == 201
    with client.session_factory() as session:
        user = session.scalar(select(models.User).where(models.User.username == username))
        organization = models.Organization(name="%s workspace" % username, created_by=user.id)
        session.add(organization)
        session.flush()
        session.add(models.OrganizationMember(organization_id=organization.id, user_id=user.id, role="org_admin"))
        session.commit()
        organization_id = organization.id
        session.refresh(user)
        session.expunge(user)
    assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 204
    assert client.post("/api/auth/organization-context", json={"organization_id": organization_id}).status_code == 200
    return user


def _connection_payload(name="my OpenAI"):
    return {
        "name": name,
        "provider_type": "openai_responses",
        "base_url": "https://api.openai.com/v1",
        "model_name": "gpt-4.1-mini",
        "provider_options": {"timeout_seconds": 20, "max_output_tokens": 600},
        "api_key": "sk-live-secret-key-7H2K",
    }


def test_private_connection_encrypts_key_and_api_never_returns_secret(client, monkeypatch):
    owner = _login(client, monkeypatch, "ai-owner")
    created = client.post("/api/ai-connections", json=_connection_payload())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["scope"] == "private"
    assert body["key_last4"] == "7H2K"
    assert body["key_masked"] == "sk-…7H2K"
    assert "api_key" not in body
    assert "ciphertext" not in body

    with client.session_factory() as session:
        connection = session.get(models.AIConnection, body["id"])
        assert connection.owner_id == owner.id
        assert connection.api_key_ciphertext != _connection_payload()["api_key"]
        assert _connection_payload()["api_key"] not in connection.api_key_ciphertext
        assert connection.api_key_nonce
        assert connection.api_key_tag
        assert connection.key_version == 1

    assert _connection_payload()["api_key"] not in client.get("/api/ai-connections").text


def test_environment_variables_no_longer_grant_a_platform_model(monkeypatch):
    """受保护部署的平台模型只来自管理员配置（D-028）。

    此前 `PLATFORM_MANAGED_LLM_ENABLED=True` 就能让环境变量里的模型生效。留着这条
    路等于把刚堵上的洞用一个 env 重新打开：谁设的、什么时候设的、设了什么，一概
    没有记录。现在无论这个开关是什么值，受保护部署都必须走配置表。
    """
    from backend.app.services.llm.factory import get_llm_scorer

    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "protected deployment password")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "platform-only-key")

    for granted in (False, True):
        monkeypatch.setattr(settings, "PLATFORM_MANAGED_LLM_ENABLED", granted)
        with pytest.raises(RuntimeError, match="平台模型"):
            get_llm_scorer()


def test_private_connections_are_not_visible_or_mutable_by_another_user(client, monkeypatch):
    _login(client, monkeypatch, "ai-owner-other")
    created = client.post("/api/ai-connections", json=_connection_payload("owner-only"))
    assert created.status_code == 201
    connection_id = created.json()["id"]
    assert client.post("/api/auth/logout").status_code == 204
    _login(client, monkeypatch, "ai-intruder")

    assert client.get("/api/ai-connections").json() == []
    assert client.patch(
        "/api/ai-connections/%s" % connection_id,
        json={"name": "stolen"},
    ).status_code == 404
    assert client.post("/api/ai-connections/%s/disable" % connection_id).status_code == 404
    assert client.post(
        "/api/ai-connections/%s/rotate-key" % connection_id,
        json={"api_key": "sk-other-secret-9X9X"},
    ).status_code == 404


def test_rotating_or_disabling_connection_advances_runtime_version_and_never_reveals_key(client, monkeypatch):
    _login(client, monkeypatch, "ai-rotate")
    created = client.post("/api/ai-connections", json=_connection_payload("rotate me"))
    assert created.status_code == 201
    connection_id = created.json()["id"]
    rotated = client.post(
        "/api/ai-connections/%s/rotate-key" % connection_id,
        json={"api_key": "sk-rotated-secret-9X9X"},
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["key_version"] == 2
    assert rotated.json()["key_masked"] == "sk-…9X9X"
    assert "rotated-secret" not in rotated.text

    disabled = client.post("/api/ai-connections/%s/disable" % connection_id)
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"


def test_runtime_resolution_decrypts_only_active_owner_connection_and_has_safe_snapshot(client, monkeypatch):
    from backend.app.services.ai_connections import resolve_connection_runtime

    owner = _login(client, monkeypatch, "ai-runtime")
    created = client.post("/api/ai-connections", json=_connection_payload("runtime"))
    assert created.status_code == 201
    connection_id = created.json()["id"]

    with client.session_factory() as session:
        runtime = resolve_connection_runtime(
            session,
            connection_id=connection_id,
            owner_id=owner.id,
            organization_id=created.json()["organization_id"],
        )
        assert runtime.api_key == "sk-live-secret-key-7H2K"
        snapshot = runtime.snapshot()
        assert snapshot == {
            "ai_connection_id": connection_id,
            "key_version": 1,
            "provider_type": "openai_responses",
            "base_url": "https://api.openai.com/v1",
            "model_name": "gpt-4.1-mini",
            "provider_options": {"max_output_tokens": 600, "timeout_seconds": 20},
        }
        assert "api_key" not in snapshot

    assert client.post("/api/ai-connections/%s/disable" % connection_id).status_code == 200
    with client.session_factory() as session:
        try:
            resolve_connection_runtime(
                session,
                connection_id=connection_id,
                owner_id=owner.id,
                organization_id=created.json()["organization_id"],
            )
        except ValueError as exc:
            assert str(exc) == "AI connection is disabled"
        else:
            raise AssertionError("disabled connection must not resolve a runtime")


def test_runtime_factory_uses_bound_byok_key_not_platform_key(client, monkeypatch):
    from backend.app.services.ai_connections import resolve_connection_runtime
    from backend.app.services.llm.factory import get_llm_scorer

    owner = _login(client, monkeypatch, "ai-factory")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "platform-key-must-not-be-used")
    created = client.post("/api/ai-connections", json=_connection_payload("factory"))
    assert created.status_code == 201
    with client.session_factory() as session:
        runtime = resolve_connection_runtime(
            session,
            connection_id=created.json()["id"],
            owner_id=owner.id,
            organization_id=created.json()["organization_id"],
        )
    scorer = get_llm_scorer(runtime)
    try:
        assert scorer.api_key == "sk-live-secret-key-7H2K"
        assert scorer.api_key != settings.OPENAI_API_KEY
        assert scorer.base_url == "https://api.openai.com/v1"
        assert scorer.model_name == "gpt-4.1-mini"
        assert scorer._ai_connection_snapshot == runtime.snapshot()
    finally:
        scorer.close()


def test_batch_binds_private_connection_snapshot_and_rejects_another_users_connection(client, monkeypatch):
    owner = _login(client, monkeypatch, "ai-batch-owner")
    connection = client.post("/api/ai-connections", json=_connection_payload("batch key"))
    assert connection.status_code == 201
    with client.session_factory() as session:
        organization_id = connection.json()["organization_id"]
        membership = session.scalar(
            select(models.OrganizationMember).where(
                models.OrganizationMember.organization_id == organization_id,
                models.OrganizationMember.user_id == owner.id,
            )
        )
        membership.role = "teacher"
        rubric = models.Rubric(
            name="batch rubric",
            version="v1",
            total_score=100,
            owner_id=owner.id,
            organization_id=organization_id,
            visibility="private",
        )
        session.add(rubric)
        session.commit()
        rubric_id = rubric.id

    created = client.post(
        "/api/batches",
        json={
            "name": "BYOK batch",
            "rubric_id": rubric_id,
            "ai_connection_id": connection.json()["id"],
        },
    )
    assert created.status_code == 200, created.text
    with client.session_factory() as session:
        batch = session.get(models.GradingBatch, created.json()["id"])
        assert batch.ai_connection_id == connection.json()["id"]
        assert batch.ai_connection_key_version == 1
        assert batch.ai_connection_snapshot == {
            "ai_connection_id": connection.json()["id"],
            "key_version": 1,
            "provider_type": "openai_responses",
            "base_url": "https://api.openai.com/v1",
            "model_name": "gpt-4.1-mini",
            "provider_options": {"max_output_tokens": 600, "timeout_seconds": 20},
        }
        assert "api_key" not in batch.ai_connection_snapshot

    assert client.post("/api/auth/logout").status_code == 204
    intruder = _login(client, monkeypatch, "ai-batch-intruder")
    with client.session_factory() as session:
        session.scalar(
            select(models.OrganizationMember).where(
                models.OrganizationMember.user_id == intruder.id
            )
        ).role = "teacher"
        session.commit()
    denied = client.post(
        "/api/batches",
        json={
            "name": "stolen connection batch",
            "rubric_id": rubric_id,
            "ai_connection_id": connection.json()["id"],
        },
    )
    assert denied.status_code == 404


def test_bound_batch_fails_closed_after_connection_key_rotation(client, monkeypatch):
    from backend.app.services.scoring.engine import _scorer_for_batch

    monkeypatch.setattr(
        "backend.app.services.scoring.engine.validate_outbound_base_url",
        lambda value: value,
    )

    owner = _login(client, monkeypatch, "ai-bound-batch")
    connection = client.post("/api/ai-connections", json=_connection_payload("bound batch key"))
    assert connection.status_code == 201
    with client.session_factory() as session:
        organization_id = connection.json()["organization_id"]
        membership = session.scalar(
            select(models.OrganizationMember).where(
                models.OrganizationMember.organization_id == organization_id,
                models.OrganizationMember.user_id == owner.id,
            )
        )
        membership.role = "teacher"
        rubric = models.Rubric(
            name="bound batch rubric",
            version="v1",
            total_score=100,
            owner_id=owner.id,
            organization_id=organization_id,
            visibility="private",
        )
        session.add(rubric)
        session.commit()
        rubric_id = rubric.id
    created = client.post(
        "/api/batches",
        json={"name": "bound batch", "rubric_id": rubric_id, "ai_connection_id": connection.json()["id"]},
    )
    assert created.status_code == 200
    with client.session_factory() as session:
        scorer = _scorer_for_batch(session, session.get(models.GradingBatch, created.json()["id"]))
        try:
            assert scorer._ai_connection_snapshot["key_version"] == 1
        finally:
            scorer.close()

    assert client.post(
        "/api/ai-connections/%s/rotate-key" % connection.json()["id"],
        json={"api_key": "sk-rotated-again-1ZZZ"},
    ).status_code == 200
    with client.session_factory() as session:
        try:
            _scorer_for_batch(session, session.get(models.GradingBatch, created.json()["id"]))
        except ValueError as exc:
            assert str(exc) == "AI connection key has changed; recreate the scoring task"
        else:
            raise AssertionError("a rotated connection must fail the old task")


def test_connection_probe_is_server_side_audited_and_keeps_key_out_of_response(client, monkeypatch):
    _login(client, monkeypatch, "ai-probe")
    created = client.post("/api/ai-connections", json=_connection_payload("probe"))
    assert created.status_code == 201

    def fake_probe(runtime):
        assert runtime.api_key == "sk-live-secret-key-7H2K"
        return {"provider_type": runtime.provider_type, "model_name": runtime.model_name}

    monkeypatch.setattr("backend.app.api.routes.ai_connections.verify_connection_runtime", fake_probe)
    probed = client.post("/api/ai-connections/%s/test" % created.json()["id"])
    assert probed.status_code == 200, probed.text
    assert "secret-key" not in probed.text
    with client.session_factory() as session:
        connection = session.get(models.AIConnection, created.json()["id"])
        assert connection.last_verified_at is not None
        events = session.scalars(select(models.AuditLog.event_type)).all()
        assert "ai_connection.verified" in events


def test_usage_ledger_records_only_snapshot_and_token_projection(client, monkeypatch):
    from backend.app.services.ai_connections import record_usage_ledger

    owner = _login(client, monkeypatch, "ai-ledger")
    created = client.post("/api/ai-connections", json=_connection_payload("ledger"))
    assert created.status_code == 201
    with client.session_factory() as session:
        organization_id = created.json()["organization_id"]
        rubric = models.Rubric(
            name="ledger rubric",
            version="v1",
            total_score=100,
            owner_id=owner.id,
            organization_id=organization_id,
            visibility="private",
        )
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(
            name="ledger batch",
            rubric_id=rubric.id,
            owner_id=owner.id,
            organization_id=organization_id,
        )
        session.add(batch)
        session.flush()
        paper = models.Paper(
            batch_id=batch.id,
            owner_id=owner.id,
            organization_id=organization_id,
            file_name="ledger.docx",
            file_path="uploads/ledger.docx",
        )
        session.add(paper)
        session.flush()
        run = models.ScoringRun(
            paper_id=paper.id,
            rubric_id=rubric.id,
            owner_id=owner.id,
            organization_id=organization_id,
            model_provider="openai",
            model_name="gpt-4.1-mini",
            ai_connection_id=created.json()["id"],
            ai_connection_key_version=1,
            ai_connection_snapshot={
                "ai_connection_id": created.json()["id"],
                "key_version": 1,
                "provider_type": "openai_responses",
                "base_url": "https://api.openai.com/v1",
                "model_name": "gpt-4.1-mini",
                "provider_options": {},
            },
            prompt_tokens=12,
            completion_tokens=7,
            total_tokens=19,
        )
        session.add(run)
        session.flush()
        record_usage_ledger(session, run)
        session.commit()
        ledger = session.scalar(select(models.AIUsageLedger))
        assert (ledger.prompt_tokens, ledger.completion_tokens, ledger.total_tokens) == (12, 7, 19)
        assert (ledger.organization_id, ledger.owner_id, ledger.ai_connection_id) == (
            organization_id,
            owner.id,
            created.json()["id"],
        )
        assert "api_key" not in str(ledger.__dict__)


def test_connection_probe_failure_returns_only_safe_error_code(client, monkeypatch):
    _login(client, monkeypatch, "ai-probe-failure")
    created = client.post("/api/ai-connections", json=_connection_payload("probe failure"))
    assert created.status_code == 201

    def failing_probe(_runtime):
        raise ValueError("vendor echoed sk-live-secret-key-7H2K")

    monkeypatch.setattr("backend.app.api.routes.ai_connections.verify_connection_runtime", failing_probe)
    response = client.post("/api/ai-connections/%s/test" % created.json()["id"])
    assert response.status_code == 400
    assert response.json() == {"detail": "AI connection test failed"}
    assert "secret-key" not in response.text
    with client.session_factory() as session:
        assert session.get(models.AIConnection, created.json()["id"]).last_error_code == "CONNECTION_TEST_FAILED"


def test_draft_probe_uses_key_only_server_side_without_persisting_it(client, monkeypatch):
    _login(client, monkeypatch, "ai-draft-probe")

    def fake_probe(runtime):
        assert runtime.api_key == "sk-live-secret-key-7H2K"
        return {"provider_type": runtime.provider_type, "model_name": runtime.model_name}

    monkeypatch.setattr("backend.app.api.routes.ai_connections.verify_connection_runtime", fake_probe)
    response = client.post("/api/ai-connections/test-draft", json=_connection_payload("draft probe"))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "provider_type": "openai_responses",
        "model_name": "gpt-4.1-mini",
        "status": "verified",
    }
    assert "secret-key" not in response.text
    with client.session_factory() as session:
        assert session.scalar(select(models.AIConnection)) is None
        assert "ai_connection.draft_tested" in session.scalars(select(models.AuditLog.event_type)).all()


def test_byok_cache_request_is_partitioned_by_organization_connection_and_key_version():
    from types import SimpleNamespace

    from backend.app.services.cache import llm_cache

    criterion = SimpleNamespace(id="criterion", code="C1", name="Criterion", max_score=100)
    first = SimpleNamespace(
        provider="openai",
        model_name="gpt-4.1-mini",
        model_version="responses-api",
        _ai_connection_organization_id="organization-a",
        _ai_connection_snapshot={"ai_connection_id": "connection-a", "key_version": 1},
    )
    second = SimpleNamespace(
        provider="openai",
        model_name="gpt-4.1-mini",
        model_version="responses-api",
        _ai_connection_organization_id="organization-b",
        _ai_connection_snapshot={"ai_connection_id": "connection-a", "key_version": 1},
    )
    rotated = SimpleNamespace(
        provider="openai",
        model_name="gpt-4.1-mini",
        model_version="responses-api",
        _ai_connection_organization_id="organization-a",
        _ai_connection_snapshot={"ai_connection_id": "connection-a", "key_version": 2},
    )
    first_key = llm_cache.key_of(llm_cache.build_request(first, criterion, [], [], "rubric-v1"))
    assert first_key != llm_cache.key_of(llm_cache.build_request(second, criterion, [], [], "rubric-v1"))
    assert first_key != llm_cache.key_of(llm_cache.build_request(rotated, criterion, [], [], "rubric-v1"))


def test_byok_security_defaults_disable_raw_debug_logging():
    from backend.app.core.config import Settings

    assert Settings(_env_file=None, AUTH_ENABLED=False).LLM_DEBUG_LOG_ENABLED is False


def test_outbound_byok_endpoint_rejects_private_dns_resolution():
    from backend.app.services.ai_connections import validate_outbound_base_url

    def private_dns(*_args, **_kwargs):
        return [(2, 1, 6, "", ("10.1.2.3", 443))]

    def public_dns(*_args, **_kwargs):
        return [(2, 1, 6, "", ("8.8.8.8", 443))]

    try:
        validate_outbound_base_url("https://model.example/v1", resolver=private_dns)
    except ValueError as exc:
        assert str(exc) == "base_url host is not allowed"
    else:
        raise AssertionError("private resolved address must be rejected")
    assert validate_outbound_base_url("https://model.example/v1", resolver=public_dns) == "https://model.example/v1"


def test_connection_management_rate_limit_is_per_authenticated_user(client, monkeypatch):
    _login(client, monkeypatch, "ai-rate-limit")
    monkeypatch.setattr(settings, "AI_CONNECTION_RATE_LIMIT_PER_MINUTE", 1)
    monkeypatch.setattr(
        "backend.app.api.routes.ai_connections.verify_connection_runtime",
        lambda runtime: {"provider_type": runtime.provider_type, "model_name": runtime.model_name},
    )
    first = client.post("/api/ai-connections/test-draft", json=_connection_payload("rate one"))
    second = client.post("/api/ai-connections/test-draft", json=_connection_payload("rate two"))
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json() == {"detail": "AI connection request rate limit exceeded"}


def test_connection_probe_never_follows_provider_redirects(monkeypatch):
    from backend.app.services.ai_connections import ConnectionRuntime, verify_connection_runtime

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("backend.app.services.ai_connections.validate_outbound_base_url", lambda value: value)
    monkeypatch.setattr("backend.app.services.ai_connections.httpx.Client", Client)
    runtime = ConnectionRuntime(
        connection_id="connection",
        key_version=1,
        organization_id="organization",
        provider_type="openai_responses",
        base_url="https://model.example/v1",
        model_name="model",
        provider_options={},
        api_key="sk-secret-7H2K",
    )
    assert verify_connection_runtime(runtime)["provider_type"] == "openai_responses"
    assert captured["follow_redirects"] is False
