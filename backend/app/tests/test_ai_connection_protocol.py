import httpx
import pytest
from sqlalchemy import select

from backend.app.db import models
from backend.app.services.ai_connection_protocol import CHAT
from backend.app.services.ai_connection_protocol import ProtocolEndpointMissing
from backend.app.services.ai_connection_protocol import ProtocolNotDetected
from backend.app.services.ai_connection_protocol import RESPONSES
from backend.app.services.ai_connection_protocol import known_host_protocol
from backend.app.services.ai_connection_protocol import resolve_protocol
from backend.app.services.ai_connection_protocol import split_endpoint_suffix
from backend.app.tests.test_ai_connections import _login


class _Probe:
    """Scripted probe: maps provider_type -> None (ok) or an exception to raise."""

    def __init__(self, **outcomes):
        self.outcomes = outcomes
        self.calls = []

    def __call__(self, provider_type, base_url):
        self.calls.append((provider_type, base_url))
        outcome = self.outcomes.get(provider_type)
        if outcome is not None:
            raise outcome


@pytest.mark.parametrize("url,base,protocol", [
    ("https://gw.example/v1/chat/completions", "https://gw.example/v1", CHAT),
    ("https://gw.example/v1/Chat/Completions/", "https://gw.example/v1", CHAT),
    ("https://gw.example/v1/responses", "https://gw.example/v1", RESPONSES),
    ("https://gw.example/v1", "https://gw.example/v1", None),
])
def test_pasted_full_endpoint_is_trimmed_and_decides_protocol(url, base, protocol):
    assert split_endpoint_suffix(url) == (base, protocol)


@pytest.mark.parametrize("url,protocol", [
    ("https://api.openai.com/v1", RESPONSES),
    ("https://open.bigmodel.cn/api/paas/v4", CHAT),
    ("https://llm-abc.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", CHAT),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", CHAT),
    ("https://notopenai.com/v1", None),
    ("https://api.openai.com.evil.example/v1", None),
])
def test_known_hosts_match_exact_host_or_parent_domain_only(url, protocol):
    assert known_host_protocol(url) == protocol


def test_saving_stays_offline_when_suffix_or_known_host_decides():
    probe = _Probe()
    suffix = resolve_protocol(requested="auto", base_url="https://gw.example/v1/responses", verify=False, probe=probe)
    known = resolve_protocol(requested="auto", base_url="https://api.deepseek.com/v1", verify=False, probe=probe)
    assert (suffix.provider_type, suffix.base_url, suffix.source, suffix.verified) == (
        RESPONSES, "https://gw.example/v1", "url_suffix", False)
    assert (known.provider_type, known.source, known.verified) == (CHAT, "known_host", False)
    assert probe.calls == []


def test_manual_choice_wins_over_known_host():
    probe = _Probe()
    result = resolve_protocol(requested=CHAT, base_url="https://api.openai.com/v1", verify=True, probe=probe)
    assert (result.provider_type, result.source, result.verified) == (CHAT, "manual", True)
    assert probe.calls == [(CHAT, "https://api.openai.com/v1")]


def test_unknown_host_tries_chat_first_then_responses_only_when_endpoint_missing():
    probe = _Probe(openai_compatible=ProtocolEndpointMissing("missing"))
    result = resolve_protocol(requested="auto", base_url="https://gw.example/v1", verify=False, probe=probe)
    assert (result.provider_type, result.source, result.verified) == (RESPONSES, "probe", True)
    assert [call[0] for call in probe.calls] == [CHAT, RESPONSES]


def test_known_host_falls_back_when_its_preferred_endpoint_is_missing():
    probe = _Probe(openai_responses=ProtocolEndpointMissing("missing"))
    result = resolve_protocol(requested="auto", base_url="https://api.openai.com/v1", verify=True, probe=probe)
    assert (result.provider_type, result.source) == (CHAT, "probe")
    assert [call[0] for call in probe.calls] == [RESPONSES, CHAT]


def test_auth_or_other_failures_do_not_try_the_other_protocol():
    probe = _Probe(openai_compatible=ValueError("AI connection test failed"))
    with pytest.raises(ValueError, match="AI connection test failed"):
        resolve_protocol(requested="auto", base_url="https://gw.example/v1", verify=True, probe=probe)
    assert [call[0] for call in probe.calls] == [CHAT]


def test_no_protocol_found_is_reported_explicitly():
    probe = _Probe(openai_compatible=ProtocolEndpointMissing("m"), openai_responses=ProtocolEndpointMissing("m"))
    with pytest.raises(ProtocolNotDetected, match="手动选择协议"):
        resolve_protocol(requested="auto", base_url="https://gw.example/v1", verify=True, probe=probe)


@pytest.mark.parametrize("status,missing", [(404, True), (405, True), (401, False), (400, False)])
def test_connection_probe_marks_only_404_405_as_missing_endpoint(status, missing, monkeypatch):
    from backend.app.services.ai_connections import ConnectionRuntime
    from backend.app.services.ai_connections import verify_connection_runtime

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **_kwargs):
            return httpx.Response(status, request=httpx.Request("POST", url))

    monkeypatch.setattr("backend.app.services.ai_connections.validate_outbound_base_url", lambda value: value)
    monkeypatch.setattr("backend.app.services.ai_connections.httpx.Client", Client)
    runtime = ConnectionRuntime(connection_id="c", key_version=1, organization_id="o", provider_type=CHAT,
                                base_url="https://gw.example/v1", model_name="m", provider_options={}, api_key="sk-7H2K")
    with pytest.raises(ValueError) as caught:
        verify_connection_runtime(runtime)
    assert isinstance(caught.value, ProtocolEndpointMissing) is missing
    assert str(caught.value) == "AI connection test failed"


def _payload(name, base_url, provider_type=None):
    payload = {"name": name, "base_url": base_url, "model_name": "model-x", "provider_options": {},
               "api_key": "sk-live-secret-key-7H2K"}
    if provider_type:
        payload["provider_type"] = provider_type
    return payload


def _scripted_route_probe(monkeypatch, missing=()):
    calls = []

    def fake_probe(runtime):
        calls.append((runtime.provider_type, runtime.base_url))
        if runtime.provider_type in missing:
            raise ProtocolEndpointMissing("AI connection test failed")
        return {"provider_type": runtime.provider_type, "model_name": runtime.model_name}

    monkeypatch.setattr("backend.app.api.routes.ai_connections.verify_connection_runtime", fake_probe)
    return calls


def test_create_without_protocol_uses_known_host_offline(client, monkeypatch):
    _login(client, monkeypatch, "proto-known")
    calls = _scripted_route_probe(monkeypatch)
    created = client.post("/api/ai-connections", json=_payload(
        "百炼", "https://llm-abc.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"))
    assert created.status_code == 201, created.text
    assert created.json()["provider_type"] == CHAT
    assert created.json()["last_verified_at"] is None
    assert calls == []


def test_create_unknown_host_probes_and_stores_detected_protocol(client, monkeypatch):
    _login(client, monkeypatch, "proto-probe")
    calls = _scripted_route_probe(monkeypatch, missing={CHAT})
    created = client.post("/api/ai-connections", json=_payload("网关", "https://gw.example/v1/"))
    assert created.status_code == 201, created.text
    assert created.json()["provider_type"] == RESPONSES
    assert created.json()["last_verified_at"] is not None
    assert [call[0] for call in calls] == [CHAT, RESPONSES]
    with client.session_factory() as session:
        audit = session.scalar(select(models.AuditLog).where(models.AuditLog.event_type == "ai_connection.created"))
        assert audit.event_metadata["protocol_detection"] == "probe"
        assert audit.event_metadata["provider_type"] == RESPONSES


def test_create_unknown_host_probe_failure_saves_nothing_and_explains(client, monkeypatch):
    _login(client, monkeypatch, "proto-fail")

    def failing_probe(_runtime):
        raise ValueError("vendor echoed sk-live-secret-key-7H2K")

    monkeypatch.setattr("backend.app.api.routes.ai_connections.verify_connection_runtime", failing_probe)
    response = client.post("/api/ai-connections", json=_payload("网关", "https://gw.example/v1"))
    assert response.status_code == 400
    assert "无法自动识别协议" in response.json()["detail"]
    assert "secret-key" not in response.text
    with client.session_factory() as session:
        assert session.scalar(select(models.AIConnection)) is None


def test_test_draft_returns_detected_protocol_source_and_trimmed_url(client, monkeypatch):
    _login(client, monkeypatch, "proto-draft")
    calls = _scripted_route_probe(monkeypatch)
    response = client.post("/api/ai-connections/test-draft", json=_payload(
        "粘贴完整地址", "https://gw.example/v1/chat/completions"))
    assert response.status_code == 200, response.text
    assert response.json() == {"provider_type": CHAT, "model_name": "model-x", "status": "verified",
                               "detection": "url_suffix", "base_url": "https://gw.example/v1"}
    assert calls == [(CHAT, "https://gw.example/v1")]


def test_saved_connection_test_keeps_stored_protocol(client, monkeypatch):
    _login(client, monkeypatch, "proto-saved")
    created = client.post("/api/ai-connections", json=_payload("手动", "https://gw.example/v1", RESPONSES))
    assert created.status_code == 201, created.text
    calls = _scripted_route_probe(monkeypatch)
    tested = client.post("/api/ai-connections/%s/test" % created.json()["id"])
    assert tested.status_code == 200, tested.text
    assert tested.json()["provider_type"] == RESPONSES
    assert tested.json()["detection"] == "stored"
    assert calls == [(RESPONSES, "https://gw.example/v1")]
