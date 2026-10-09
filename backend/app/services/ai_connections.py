"""Private BYOK connection persistence and short-lived runtime resolution."""

import base64
import hashlib
import ipaddress
import secrets
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.db.models import User
from backend.app.db.models import AIConnection
from backend.app.db.models import AIUsageLedger
from backend.app.db.models import utcnow
from backend.app.services.ai_connection_protocol import MESSAGES
from backend.app.services.ai_connection_protocol import PROTOCOLS
from backend.app.services.ai_connection_protocol import ProtocolEndpointMissing
from backend.app.services.ai_connection_protocol import normalize_base_url
from backend.app.services.ai_connection_protocol import split_endpoint_suffix


_ALLOWED_OPTION_KEYS = {
    "timeout_seconds",
    "max_output_tokens",
    "max_tokens",
    "temperature",
    "top_p",
    "response_format_json",
    "thinking_type",
    "service_tier",
    "max_concurrency",
    # 仅 Claude（anthropic_messages）：思考强度与结构化输出模式，见 validate_provider_options_for。
    "effort",
    "structured_output",
}
_CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_CLAUDE_THINKING_TYPES = ("adaptive", "disabled")
_CLAUDE_STRUCTURED_OUTPUT_MODES = ("json_schema", "off")
# 只对 OpenAI 系协议有意义的键；Claude 连接带上它们等于「设了却不生效」。
_OPENAI_ONLY_OPTION_KEYS = frozenset({"response_format_json", "service_tier", "max_output_tokens"})
_CLAUDE_ONLY_OPTION_KEYS = frozenset({"effort", "structured_output"})
# 只决定「同时发几个请求」，不影响模型看到什么、返回什么；不进复现快照。
# 否则用户为了躲 429 调低并发，会让所有已锁定该连接的批次报「连接配置已变更」。
SCHEDULING_OPTION_KEYS = frozenset({"max_concurrency"})
MAX_CONNECTION_CONCURRENCY = 8
_RATE_LOCK = threading.Lock()
_RATE_WINDOWS: dict[str, deque[float]] = {}


def enforce_connection_rate_limit(user_id: str) -> None:
    """Small process-local guard; deployments can layer gateway limits on top."""

    now = time.monotonic()
    limit = max(1, int(settings.AI_CONNECTION_RATE_LIMIT_PER_MINUTE))
    with _RATE_LOCK:
        window = _RATE_WINDOWS.setdefault(user_id, deque())
        while window and window[0] <= now - 60:
            window.popleft()
        if len(window) >= limit:
            raise ValueError("AI connection request rate limit exceeded")
        window.append(now)


class AIConnectionBindingError(ValueError):
    """A batch-pinned connection no longer matches its snapshot.

    Carries a stable code so batch items report the actionable cause instead
    of a generic ``scoring_failure (ValueError)``.
    """

    failure_kind = "checker"

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ConnectionRuntime:
    connection_id: str
    key_version: int
    organization_id: str
    provider_type: str
    base_url: str
    model_name: str
    provider_options: dict[str, Any]
    api_key: str

    def snapshot(self) -> dict[str, Any]:
        """Persist only reproducibility metadata, never key material."""

        return {
            "ai_connection_id": self.connection_id,
            "key_version": self.key_version,
            "provider_type": self.provider_type,
            "base_url": self.base_url,
            "model_name": self.model_name,
            "provider_options": dict(sorted(
                (key, value)
                for key, value in self.provider_options.items()
                if key not in SCHEDULING_OPTION_KEYS
            )),
        }


def connection_max_concurrency(options: dict | None) -> int | None:
    """连接声明的同时请求上限；未设置返回 None（由各调用方用自己的默认值）。"""

    value = (options or {}).get("max_concurrency")
    return int(value) if value is not None else None


def _master_key() -> bytes:
    secret = (settings.BYOK_MASTER_KEY or "").strip()
    if not secret:
        raise ValueError("BYOK master key is not configured")
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _aad(*, organization_id: str, owner_id: str, key_version: int) -> bytes:
    return ("ai-connection|%s|%s|%d" % (organization_id, owner_id, key_version)).encode(
        "utf-8"
    )


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decoded(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))


def encrypt_api_key(
    api_key: str, *, organization_id: str, owner_id: str, key_version: int
) -> tuple[str, str, str]:
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(_master_key()).encrypt(
        nonce,
        api_key.encode("utf-8"),
        _aad(
            organization_id=organization_id,
            owner_id=owner_id,
            key_version=key_version,
        ),
    )
    return _encoded(encrypted[:-16]), _encoded(nonce), _encoded(encrypted[-16:])


def decrypt_api_key(connection: AIConnection) -> str:
    try:
        plaintext = AESGCM(_master_key()).decrypt(
            _decoded(connection.api_key_nonce),
            _decoded(connection.api_key_ciphertext) + _decoded(connection.api_key_tag),
            _aad(
                organization_id=connection.organization_id,
                owner_id=connection.owner_id,
                key_version=connection.key_version,
            ),
        )
        return plaintext.decode("utf-8")
    except (InvalidTag, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("AI connection key cannot be decrypted") from exc


def key_masked(last4: str) -> str:
    return "sk-…%s" % last4


def validate_base_url(value: str) -> str:
    """Reject clearly unsafe endpoints before a connection can be persisted."""

    raw = value.strip().rstrip("/")
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url must be an HTTPS URL without credentials")
    host = parsed.hostname.rstrip(".").casefold()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("base_url host is not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return raw
    if not ip.is_global:
        raise ValueError("base_url host is not allowed")
    return raw


def validate_outbound_base_url(value: str, *, resolver=socket.getaddrinfo) -> str:
    """Resolve immediately before egress and reject DNS rebinding to private IPs."""

    normalized = validate_base_url(value)
    host = urlsplit(normalized).hostname
    try:
        results = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("base_url host cannot be resolved") from exc
    addresses = {item[4][0] for item in results if len(item) > 4 and item[4]}
    if not addresses:
        raise ValueError("base_url host cannot be resolved")
    try:
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError("base_url host is not allowed")
    except ValueError as exc:
        if str(exc) == "base_url host is not allowed":
            raise
        raise ValueError("base_url host cannot be resolved") from exc
    return normalized


def validate_provider_options(options: dict | None) -> dict:
    normalized = dict(options or {})
    unknown = sorted(set(normalized) - _ALLOWED_OPTION_KEYS)
    if unknown:
        raise ValueError("unsupported provider options: %s" % ", ".join(unknown))
    if "timeout_seconds" in normalized and not 1 <= float(normalized["timeout_seconds"]) <= 300:
        raise ValueError("timeout_seconds must be between 1 and 300")
    for key in ("max_output_tokens", "max_tokens"):
        if key in normalized and not 1 <= int(normalized[key]) <= 100_000:
            raise ValueError("%s must be between 1 and 100000" % key)
    if "temperature" in normalized and not 0 <= float(normalized["temperature"]) <= 2:
        raise ValueError("temperature must be between 0 and 2")
    if "top_p" in normalized and not 0 < float(normalized["top_p"]) <= 1:
        raise ValueError("top_p must be greater than 0 and at most 1")
    if "response_format_json" in normalized and not isinstance(normalized["response_format_json"], bool):
        raise ValueError("response_format_json must be boolean")
    if "thinking_type" in normalized and not isinstance(normalized["thinking_type"], str):
        raise ValueError("thinking_type must be text")
    if "max_concurrency" in normalized:
        value = normalized["max_concurrency"]
        # bool 是 int 的子类：True 会被当成 1 静默接受。
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_CONNECTION_CONCURRENCY:
            raise ValueError(
                "max_concurrency must be an integer between 1 and %d" % MAX_CONNECTION_CONCURRENCY
            )
    if "service_tier" in normalized and normalized["service_tier"] not in {
        "auto",
        "on_demand",
        "flex",
        "performance",
    }:
        raise ValueError("service_tier is unsupported")
    if "effort" in normalized and normalized["effort"] not in _CLAUDE_EFFORT_LEVELS:
        raise ValueError("effort must be one of: %s" % ", ".join(_CLAUDE_EFFORT_LEVELS))
    if (
        "structured_output" in normalized
        and normalized["structured_output"] not in _CLAUDE_STRUCTURED_OUTPUT_MODES
    ):
        raise ValueError("structured_output must be json_schema or off")
    return normalized


def validate_provider_options_for(provider_type: str, options: dict | None) -> dict:
    """在协议确定之后校验连接参数，拒绝该协议不会使用的键。"""

    normalized = validate_provider_options(options)
    if provider_type == MESSAGES:
        stray = sorted(set(normalized) & _OPENAI_ONLY_OPTION_KEYS)
        if stray:
            raise ValueError("options not used by Claude connections: %s" % ", ".join(stray))
        thinking = normalized.get("thinking_type")
        if thinking not in (None, "") and thinking not in _CLAUDE_THINKING_TYPES:
            raise ValueError("thinking_type for Claude must be adaptive or disabled")
        if "temperature" in normalized and "top_p" in normalized:
            raise ValueError("Claude accepts temperature or top_p, not both")
    else:
        stray = sorted(set(normalized) & _CLAUDE_ONLY_OPTION_KEYS)
        if stray:
            raise ValueError("options only used by Claude connections: %s" % ", ".join(stray))
    return normalized


def normalize_connection_base_url(provider_type: str, base_url: str) -> str:
    """校验地址；Claude 连接再去掉粘贴进来的 /messages 并统一成以 /v1 结尾。"""

    checked = validate_base_url(base_url)
    if provider_type != MESSAGES:
        return checked
    base, _suffix = split_endpoint_suffix(checked)
    return normalize_base_url(MESSAGES, base)


def active_connection_id(db: Session, *, owner_id: str, organization_id: str) -> str | None:
    return db.scalar(select(AIConnection.id).where(
        AIConnection.owner_id == owner_id,
        AIConnection.organization_id == organization_id,
        AIConnection.status == "active",
    ))


def lock_connection_owner(db: Session, owner_id: str) -> None:
    # PostgreSQL serializes concurrent creates/switches even when no connection exists.
    db.execute(select(User.id).where(User.id == owner_id).with_for_update())


def activate_connection(db: Session, connection: AIConnection) -> None:
    lock_connection_owner(db, connection.owner_id)
    others = db.scalars(select(AIConnection).where(
        AIConnection.owner_id == connection.owner_id,
        AIConnection.organization_id == connection.organization_id,
        AIConnection.status == "active",
        AIConnection.id != connection.id,
    )).all()
    for other in others:
        disable_connection(other)
    # Release the partial unique index slot before enabling the chosen connection.
    db.flush()
    connection.status = "active"
    connection.disabled_at = None
    db.flush()


def create_connection(
    db: Session,
    *,
    owner_id: str,
    organization_id: str,
    name: str,
    provider_type: str,
    base_url: str,
    model_name: str,
    provider_options: dict | None,
    api_key: str,
) -> AIConnection:
    if provider_type not in PROTOCOLS:
        raise ValueError("unsupported AI provider type")
    normalized_url = normalize_connection_base_url(provider_type, base_url)
    normalized_options = validate_provider_options_for(provider_type, provider_options)
    secret = api_key.strip()
    if len(secret) < 4:
        raise ValueError("API key must contain at least four characters")
    lock_connection_owner(db, owner_id)
    existing = db.scalar(
        select(AIConnection).where(
            AIConnection.owner_id == owner_id,
            AIConnection.name == name.strip(),
            AIConnection.status != "deleted",
        )
    )
    if existing is not None:
        raise ValueError("AI connection name already exists")
    key_version = settings.BYOK_KEY_VERSION
    ciphertext, nonce, tag = encrypt_api_key(
        secret,
        organization_id=organization_id,
        owner_id=owner_id,
        key_version=key_version,
    )
    has_connection = db.scalar(select(AIConnection.id).where(
        AIConnection.owner_id == owner_id,
        AIConnection.organization_id == organization_id,
        AIConnection.status != "deleted",
    ).limit(1)) is not None
    connection = AIConnection(
        status="disabled" if has_connection else "active",
        disabled_at=utcnow() if has_connection else None,
        organization_id=organization_id,
        owner_id=owner_id,
        name=name.strip(),
        scope="private",
        provider_type=provider_type,
        base_url=normalized_url,
        model_name=model_name.strip(),
        provider_options=normalized_options,
        api_key_ciphertext=ciphertext,
        api_key_nonce=nonce,
        api_key_tag=tag,
        key_version=key_version,
        key_last4=secret[-4:],
    )
    db.add(connection)
    db.flush()
    return connection


def resolve_connection_runtime(
    db: Session,
    *,
    connection_id: str,
    owner_id: str,
    organization_id: str,
    allow_disabled: bool = False,
) -> ConnectionRuntime:
    connection = db.scalar(
        select(AIConnection).where(
            AIConnection.id == connection_id,
            AIConnection.owner_id == owner_id,
            AIConnection.organization_id == organization_id,
        )
    )
    if connection is None:
        raise ValueError("AI connection not found")
    if connection.status == "disabled" and not allow_disabled:
        raise ValueError("AI connection is disabled")
    if connection.status != "active" and not (allow_disabled and connection.status == "disabled"):
        raise ValueError("AI connection is unavailable")
    return ConnectionRuntime(
        connection_id=connection.id,
        key_version=connection.key_version,
        organization_id=connection.organization_id,
        provider_type=connection.provider_type,
        base_url=connection.base_url,
        model_name=connection.model_name,
        provider_options=dict(connection.provider_options or {}),
        api_key=decrypt_api_key(connection),
    )


def connection_snapshot_for_owner(
    db: Session,
    *,
    connection_id: str,
    owner_id: str,
    organization_id: str,
) -> dict[str, Any]:
    """Bind a task without decrypting a key that it will not use yet."""

    connection = db.scalar(
        select(AIConnection).where(
            AIConnection.id == connection_id,
            AIConnection.owner_id == owner_id,
            AIConnection.organization_id == organization_id,
        )
    )
    if connection is None:
        raise ValueError("AI connection not found")
    if connection.status == "disabled":
        raise ValueError("AI connection is disabled")
    if connection.status != "active":
        raise ValueError("AI connection is unavailable")
    return ConnectionRuntime(
        connection_id=connection.id,
        key_version=connection.key_version,
        organization_id=connection.organization_id,
        provider_type=connection.provider_type,
        base_url=connection.base_url,
        model_name=connection.model_name,
        provider_options=dict(connection.provider_options or {}),
        api_key="",
    ).snapshot()


def rotate_connection_key(connection: AIConnection, api_key: str) -> None:
    secret = api_key.strip()
    if len(secret) < 4:
        raise ValueError("API key must contain at least four characters")
    next_version = connection.key_version + 1
    ciphertext, nonce, tag = encrypt_api_key(
        secret,
        organization_id=connection.organization_id,
        owner_id=connection.owner_id,
        key_version=next_version,
    )
    connection.api_key_ciphertext = ciphertext
    connection.api_key_nonce = nonce
    connection.api_key_tag = tag
    connection.key_version = next_version
    connection.key_last4 = secret[-4:]
    connection.last_error_code = None


def disable_connection(connection: AIConnection) -> None:
    connection.status = "disabled"
    connection.disabled_at = utcnow()


def verify_connection_runtime(runtime: ConnectionRuntime) -> dict[str, str]:
    """Perform a minimal server-side vendor probe without persisting its body."""

    timeout = float(runtime.provider_options.get("timeout_seconds", 30))
    base_url = validate_outbound_base_url(runtime.base_url)
    if runtime.provider_type == "openai_responses":
        endpoint = base_url.rstrip("/") + "/responses"
        payload = {
            "model": runtime.model_name,
            "input": "Return exactly {\"ok\":true}.",
            "max_output_tokens": 16,
        }
    elif runtime.provider_type == "openai_compatible":
        endpoint = base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": runtime.model_name,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8,
        }
    elif runtime.provider_type == MESSAGES:
        endpoint, payload = _claude_probe(runtime, base_url)
    else:
        raise ValueError("unsupported AI connection provider type")
    headers = (
        {"x-api-key": runtime.api_key, "anthropic-version": _CLAUDE_API_VERSION}
        if runtime.provider_type == MESSAGES
        else {"Authorization": "Bearer %s" % runtime.api_key}
    )
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # 404/405 只说明这个协议的接口不在该地址下，协议识别据此改试另一种协议；
        # 其它状态照常按测试失败处理。两者对调用方都是 ValueError，行为不变。
        if exc.response.status_code in (404, 405):
            raise ProtocolEndpointMissing("AI connection test failed") from exc
        if runtime.provider_type == MESSAGES and exc.response.status_code == 400:
            # Claude 的 400 多半是参数组合不被该模型或端点接受（新模型拒收 temperature，
            # Bedrock mantle 拒收结构化输出）。只给方向，不回显厂商正文。
            raise ValueError(
                "AI connection test failed: 请求被拒绝，请检查模型名，以及高级设置里的"
                "思考强度、结构化输出与采样参数"
            ) from exc
        raise ValueError("AI connection test failed") from exc
    except httpx.HTTPError as exc:
        # The caller records only a stable code; provider bodies may include
        # customer or key-adjacent diagnostics and must never leave this layer.
        raise ValueError("AI connection test failed") from exc
    return {"provider_type": runtime.provider_type, "model_name": runtime.model_name}


_CLAUDE_API_VERSION = "2023-06-01"
_CLAUDE_PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _claude_probe(runtime: ConnectionRuntime, base_url: str):
    """最小 Messages 请求，带上与评分相同的参数形状。

    「Opus 5.5 配了 temperature」「mantle 开了结构化输出」这类组合在测试时就该失败，
    而不是等评分时才 400。
    """

    from backend.app.services.llm.anthropic_messages_adapter import request_controls
    from backend.app.services.llm.anthropic_messages_adapter import resolve_structured_output

    options = runtime.provider_options
    payload = {
        "model": runtime.model_name,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": 'Reply with {"ok":true}.'}],
        **request_controls(
            temperature=options.get("temperature"),
            top_p=options.get("top_p"),
            thinking_type=(options.get("thinking_type") or "").strip() or None,
            effort=options.get("effort"),
        ),
    }
    if resolve_structured_output(base_url, options) == "json_schema":
        payload.setdefault("output_config", {})["format"] = {
            "type": "json_schema",
            "schema": _CLAUDE_PROBE_SCHEMA,
        }
    return base_url.rstrip("/") + "/messages", payload


def usage_connection_id(snapshot) -> str | None:
    """真实 BYOK 外键；平台来源仅存在于不可变快照中。"""
    connection_id = (snapshot or {}).get("ai_connection_id")
    return None if connection_id == "platform" else connection_id


def record_usage_ledger(db: Session, scoring_run, usage=None) -> None:
    """Append a non-secret usage projection for a BYOK scoring run.

    ``usage`` carries metered request/failure counts; without it the row keeps
    the historical one-request projection.
    """

    snapshot = getattr(scoring_run, "ai_connection_snapshot", None) or {}
    connection_id = usage_connection_id(snapshot)
    # 平台用量保留在 ScoringRun；此表只投影具有真实用户连接外键的 BYOK。
    if not connection_id:
        return
    db.add(
        AIUsageLedger(
            organization_id=scoring_run.organization_id,
            owner_id=scoring_run.owner_id,
            ai_connection_id=connection_id,
            scoring_run_id=scoring_run.id,
            provider_type=snapshot["provider_type"],
            model_name=snapshot["model_name"],
            prompt_tokens=int(scoring_run.prompt_tokens or 0),
            completion_tokens=int(scoring_run.completion_tokens or 0),
            total_tokens=int(scoring_run.total_tokens or 0),
            request_count=(
                1 if usage is None else int(usage.get("request_count") or 0)
            ),
            failure_count=(
                0 if usage is None else int(usage.get("failure_count") or 0)
            ),
        )
    )
