"""日志配置与测试连接失败的诊断日志（2026-10-14：平台模型测试连接失败，日志里看不出原因）。"""

from __future__ import annotations

from contextlib import contextmanager
import logging
import sys

import httpx
import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings
from backend.app.core.log_config import FORMAT
from backend.app.core.log_config import QUIET_LOGGERS
from backend.app.core.log_config import configure_logging
from backend.app.services.ai_connection_protocol import ProtocolEndpointMissing
from backend.app.services.ai_connections import ConnectionRuntime
from backend.app.services.ai_connections import verify_connection_runtime


KEY = "bedrock-api-key-ABSK-7Q2Z"
BEDROCK = "https://bedrock-runtime.ap-southeast-1.amazonaws.com/anthropic/v1"


@contextmanager
def bare_root_logger():
    """pytest 在每个测试执行期间给根日志器挂自己的处理器；在测试体内临时拿掉，测完原样恢复。"""

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_quiet = {name: logging.getLogger(name).level for name in QUIET_LOGGERS}
    root.handlers = []
    try:
        yield root
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_quiet.items():
            logging.getLogger(name).setLevel(level)


def test_configure_logging_adds_one_stdout_handler_with_time_level_and_source():
    with bare_root_logger() as root:
        assert configure_logging("info") is True

        (handler,) = root.handlers
        assert isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout
        assert handler.formatter._fmt == FORMAT == "%(asctime)s %(levelname)s %(name)s %(message)s"
        assert root.level == logging.INFO
        # 第三方 HTTP 客户端会记请求地址：不随 LOG_LEVEL 调低。
        assert all(logging.getLogger(name).level == logging.WARNING for name in QUIET_LOGGERS)
        # 重复调用（uvicorn 多次进入 lifespan、worker 重启）不叠加处理器。
        assert configure_logging("DEBUG") is False
        assert len(root.handlers) == 1


def test_configure_logging_leaves_an_existing_configuration_alone():
    root = logging.getLogger()
    before = (root.handlers[:], root.level)

    assert configure_logging("DEBUG") is False
    assert (root.handlers, root.level) == before


def test_log_level_setting_accepts_any_case_and_rejects_unknown_levels():
    assert Settings(_env_file=None).LOG_LEVEL == "INFO"
    assert Settings(_env_file=None, LOG_LEVEL=" debug ").LOG_LEVEL == "DEBUG"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, LOG_LEVEL="verbose")


class _Client:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error

    def __call__(self, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, url, *, json, headers):
        if self.error is not None:
            raise self.error
        return self.response(url)


def _runtime(model="anthropic.claude-opus-5"):
    return ConnectionRuntime(
        connection_id="c", key_version=1, organization_id="o", provider_type="anthropic_messages",
        base_url=BEDROCK, model_name=model, provider_options={}, api_key=KEY,
    )


def _probe(monkeypatch, client):
    monkeypatch.setattr("backend.app.services.ai_connections.validate_outbound_base_url", lambda value: value)
    monkeypatch.setattr("backend.app.services.ai_connections.httpx.Client", client)


def _failures(caplog):
    return [r for r in caplog.records if r.getMessage().startswith("ai_connection_test_failed")]


def test_bedrock_rejection_is_logged_with_status_and_error_type_but_no_body_or_key(monkeypatch, caplog):
    body = {"message": "The provided model identifier is invalid. echo: %s" % KEY}
    _probe(monkeypatch, _Client(response=lambda url: httpx.Response(
        400,
        json=body,
        headers={"x-amzn-errortype": "ValidationException:http://internal.amazon.com/coral/com.amazon.bedrock/"},
        request=httpx.Request("POST", url),
    )))
    caplog.set_level(logging.INFO, logger="paper_grading.ai_connections")

    with pytest.raises(ValueError):
        verify_connection_runtime(_runtime())

    (record,) = _failures(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "ai_connection_test_failed provider_type=anthropic_messages "
        "host=bedrock-runtime.ap-southeast-1.amazonaws.com model=anthropic.claude-opus-5 "
        "status=400 error_type=ValidationException"
    )
    assert KEY not in caplog.text and "model identifier" not in caplog.text


def test_anthropic_json_error_type_and_network_errors_are_named(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="paper_grading.ai_connections")
    _probe(monkeypatch, _Client(response=lambda url: httpx.Response(
        401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}},
        request=httpx.Request("POST", url),
    )))
    with pytest.raises(ValueError):
        verify_connection_runtime(_runtime())
    _probe(monkeypatch, _Client(error=httpx.ConnectTimeout("timed out")))
    with pytest.raises(ValueError):
        verify_connection_runtime(_runtime())

    messages = [r.getMessage() for r in _failures(caplog)]
    assert messages[0].endswith("status=401 error_type=authentication_error")
    assert messages[1].endswith("status=- error_type=ConnectTimeout")


def test_missing_endpoint_during_protocol_detection_is_only_info(monkeypatch, caplog):
    _probe(monkeypatch, _Client(response=lambda url: httpx.Response(404, json={}, request=httpx.Request("POST", url))))
    caplog.set_level(logging.INFO, logger="paper_grading.ai_connections")

    with pytest.raises(ProtocolEndpointMissing):
        verify_connection_runtime(_runtime())

    (record,) = _failures(caplog)
    assert record.levelno == logging.INFO
    assert record.getMessage().endswith("status=404 error_type=-")


def test_model_names_cannot_inject_extra_log_lines(monkeypatch, caplog):
    _probe(monkeypatch, _Client(response=lambda url: httpx.Response(403, json={}, request=httpx.Request("POST", url))))
    caplog.set_level(logging.INFO, logger="paper_grading.ai_connections")

    with pytest.raises(ValueError):
        verify_connection_runtime(_runtime(model="claude\nINFO forged line"))

    (record,) = _failures(caplog)
    assert "\n" not in record.getMessage()
