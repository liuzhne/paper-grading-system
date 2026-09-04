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
from backend.app.db.models import AIConnection
from backend.app.db.models import AIUsageLedger
from backend.app.db.models import utcnow


_ALLOWED_OPTION_KEYS = {
    "timeout_seconds",
    "max_output_tokens",
    "max_tokens",
    "temperature",
    "top_p",
    "response_format_json",
    "thinking_type",
    "service_tier",
}
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
            "provider_options": dict(sorted(self.provider_options.items())),
        }


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
    for key in ("temperature", "top_p"):
        if key in normalized and not 0 <= float(normalized[key]) <= 2:
            raise ValueError("%s must be between 0 and 2" % key)
    if "response_format_json" in normalized and not isinstance(normalized["response_format_json"], bool):
        raise ValueError("response_format_json must be boolean")
    if "thinking_type" in normalized and not isinstance(normalized["thinking_type"], str):
        raise ValueError("thinking_type must be text")
    if "service_tier" in normalized and normalized["service_tier"] not in {
        "auto",
        "on_demand",
        "flex",
        "performance",
    }:
        raise ValueError("service_tier is unsupported")
    return normalized


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
    if provider_type not in {"openai_responses", "openai_compatible"}:
        raise ValueError("unsupported AI provider type")
    normalized_url = validate_base_url(base_url)
    normalized_options = validate_provider_options(provider_options)
    secret = api_key.strip()
    if len(secret) < 4:
        raise ValueError("API key must contain at least four characters")
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
    connection = AIConnection(
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
    connection.status = "active"
    connection.disabled_at = None
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
    else:
        raise ValueError("unsupported AI connection provider type")
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.post(
                endpoint,
                json=payload,
                headers={"Authorization": "Bearer %s" % runtime.api_key},
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        # The caller records only a stable code; provider bodies may include
        # customer or key-adjacent diagnostics and must never leave this layer.
        raise ValueError("AI connection test failed") from exc
    return {"provider_type": runtime.provider_type, "model_name": runtime.model_name}


def record_usage_ledger(db: Session, scoring_run) -> None:
    """Append a non-secret usage projection for a BYOK scoring run."""

    snapshot = getattr(scoring_run, "ai_connection_snapshot", None) or {}
    connection_id = snapshot.get("ai_connection_id")
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
            request_count=1,
        )
    )
