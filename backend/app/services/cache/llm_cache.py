"""L0 运行账本 + 缓存（设计§7 / 阶段3）。

以 hash(模型 + prompt版本 + 输入文本 + Rubric版本 + 采样配置) 为 key，持久化每次 LLM 评分调用的
**完整输入(request)与输出(response)**。作用：幂等缓存（同输入复用，省钱省时）、审计（每分可回溯当时模型看了
什么、答了什么）、可复现（命中即复现，见设计§7"诚实边界"）。

存储：本地 SQLite，位于 settings.STORAGE_ROOT/llm_cache.sqlite（CLI/服务端通用；测试隔离在临时目录）。
缓存为最佳努力（best-effort）：任何读写异常都不得影响评分主流程。
"""

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Optional

from backend.app.core.config import settings
from backend.app.services.scoring.core.canonical import canonical_json
from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.contracts import PromptEnvelopeV1
from backend.app.services.scoring.core.contracts import PromptEnvelopeV2
from backend.app.services.scoring.core.contracts import PromptEnvelopeV3

# ⚠️ 凡改动评分 prompt/输入构造，务必 bump 本版本号以使旧缓存失效（设计§7：prompt 进哈希）。
PROMPT_VERSION = "2026-08-31-1"  # Core V3 real-provider boundary + BYOK partition identity


@dataclass(frozen=True)
class CacheRetentionPolicy:
    """Storage and access boundary frozen with a provider envelope."""

    retention_seconds: int
    store_controlled_original: bool
    allowed_scopes: tuple[str, ...]

    def __post_init__(self):
        if isinstance(self.retention_seconds, bool) or not isinstance(self.retention_seconds, int):
            raise TypeError("retention_seconds must be an integer")
        if self.retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        if not isinstance(self.store_controlled_original, bool):
            raise TypeError("store_controlled_original must be boolean")
        scopes = tuple(self.allowed_scopes)
        if not scopes or any(not isinstance(scope, str) or not scope.strip() for scope in scopes):
            raise ValueError("allowed_scopes must contain non-empty scope names")
        if len(set(scopes)) != len(scopes):
            raise ValueError("allowed_scopes must not contain duplicates")
        object.__setattr__(self, "allowed_scopes", scopes)


@dataclass(frozen=True)
class CacheEnvelopeEntry:
    key: str
    original_envelope_hash: str
    original_envelope: Optional[PromptEnvelopeV1 | PromptEnvelopeV3]
    redacted_audit_projection: dict
    response: object
    access_scope: str
    allowed_scopes: tuple[str, ...]
    created_at: datetime
    expires_at: datetime


def build_request(scorer, criterion, candidates, structure_checks, rubric_version, anchors=None):
    """构造进入哈希且作为审计留存的"完整输入"。
    校准锚点并入哈希 → 锚点变化即缓存失效（保可复现，设计§7）。"""
    connection_snapshot = getattr(scorer, "_ai_connection_snapshot", None) or {}
    return {
        "prompt_version": PROMPT_VERSION,
        "provider": getattr(scorer, "provider", ""),
        "model": getattr(scorer, "model_name", ""),
        "model_version": getattr(scorer, "model_version", ""),
        # BYOK credentials never enter the key.  The tenant, connection and
        # rotation version do, so even byte-identical submissions cannot reuse
        # another user's/provider account cache entry.
        "connection_scope": {
            "organization_id": getattr(scorer, "_ai_connection_organization_id", None),
            "ai_connection_id": connection_snapshot.get("ai_connection_id"),
            "key_version": connection_snapshot.get("key_version"),
        },
        "sampling": {
            "openai_temperature": settings.OPENAI_TEMPERATURE,
            "openai_compatible_temperature": settings.OPENAI_COMPATIBLE_TEMPERATURE,
        },
        "rubric_version": rubric_version,
        "criterion": {
            "id": getattr(criterion, "id", None),
            "code": getattr(criterion, "code", None),
            "name": getattr(criterion, "name", None),
            "max_score": float(getattr(criterion, "max_score", 0) or 0),
            "description": getattr(criterion, "description", None),
            "evidence_hints": list(getattr(criterion, "evidence_hints", None) or []),
            "deduction_rules": list(getattr(criterion, "deduction_rules", None) or []),
            "criterion_type": getattr(criterion, "criterion_type", None),
            "scoring_mode": getattr(criterion, "scoring_mode", None),
            "applies_to": getattr(criterion, "applies_to", None),
            "rubric_levels": list(getattr(criterion, "rubric_levels", None) or []),
        },
        "candidates": [{"chunk_id": c.get("chunk_id"), "text": c.get("text")} for c in (candidates or []) if isinstance(c, dict)],
        "structure_checks": structure_checks,
        "calibration_anchors": anchors or [],
    }


def key_of(request):
    if isinstance(request, (PromptEnvelopeV1, PromptEnvelopeV3)):
        return canonical_sha256(request.to_mapping())
    blob = json.dumps(request, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get(key):
    if not key:
        return None
    try:
        with closing(_connect()) as conn:
            row = conn.execute("SELECT response FROM llm_cache WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return json.loads(row[0])
    except Exception:
        return None


def put(key, request, response, model=""):
    if not key:
        return
    try:
        with closing(_connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_cache (key, model, request, response, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    key,
                    model,
                    json.dumps(request, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(response, ensure_ascii=False, default=str),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
    except Exception:
        pass


def _utc_now(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value):
    parsed = datetime.fromisoformat(value)
    return _utc_now(parsed)


def _coerce_prompt_envelope(value):
    if isinstance(value, (PromptEnvelopeV1, PromptEnvelopeV3)):
        return value
    if isinstance(value, Mapping):
        schema_version = value.get("schema_version")
        if schema_version == "prompt-envelope@3":
            return PromptEnvelopeV3.from_mapping(value)
        if schema_version == "prompt-envelope@2":
            return PromptEnvelopeV2.from_mapping(value)
    return PromptEnvelopeV1.from_mapping(value)


def redact_prompt_envelope(envelope):
    """Return the deliberately non-authoritative audit projection.

    Identity hashes and structural metadata remain inspectable, while direct
    student text and the paper title are removed.  Calibration anchors are
    curated, de-identified exemplars and intentionally remain visible so an
    auditor can reconstruct the calibration context.  The projection is never
    passed to a provider and never used as a cache key.
    """

    envelope = _coerce_prompt_envelope(envelope)
    projection = envelope.to_mapping()
    if projection["schema_version"] == "prompt-envelope@3":
        for unit in projection["evidence_units"]:
            unit["normalized_text"] = "[REDACTED]"
    else:
        projection["submission"]["title"] = "[REDACTED]"
        for unit in projection["evidence_units"]:
            unit["text"] = "[REDACTED]"
    return projection


def put_envelope(*, envelope, response, policy, access_scope, now=None):
    """Persist the exact provider envelope under a controlled retention policy."""

    envelope = _coerce_prompt_envelope(envelope)
    if not isinstance(policy, CacheRetentionPolicy):
        raise TypeError("policy must be CacheRetentionPolicy")
    if access_scope not in policy.allowed_scopes:
        raise PermissionError("cache write scope is not authorized by the retention policy")

    created_at = _utc_now(now)
    expires_at = created_at + timedelta(seconds=policy.retention_seconds)
    key = key_of(envelope)
    original = canonical_json(envelope.to_mapping()) if policy.store_controlled_original else None
    redacted = canonical_json(redact_prompt_envelope(envelope))
    with closing(_connect()) as conn:
        # This key is also the immutable ledger identity.  Retries and
        # concurrent writers must not replace the first response, extend its
        # retention, start retaining controlled originals, or broaden access.
        conn.execute(
            "INSERT OR IGNORE INTO llm_envelope_cache "
            "(key, original_envelope_hash, original_envelope, redacted_projection, response, "
            "access_scope, allowed_scopes, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                key,
                original,
                redacted,
                json.dumps(response, ensure_ascii=False, separators=(",", ":"), default=str),
                access_scope,
                json.dumps(list(policy.allowed_scopes), ensure_ascii=False, separators=(",", ":")),
                created_at.isoformat(),
                expires_at.isoformat(),
            ),
        )
        conn.commit()
    return key


def get_entry(key, *, requester_scope, now=None):
    """Read an unexpired entry only when its frozen access policy permits it."""

    if not key or not requester_scope:
        return None
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT original_envelope_hash, original_envelope, redacted_projection, response, "
            "access_scope, allowed_scopes, created_at, expires_at "
            "FROM llm_envelope_cache WHERE key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None

    try:
        if row[0] != key:
            return None
        raw_allowed_scopes = json.loads(row[5])
        if (
            not isinstance(raw_allowed_scopes, list)
            or not raw_allowed_scopes
            or any(not isinstance(scope, str) or not scope.strip() for scope in raw_allowed_scopes)
            or len(set(raw_allowed_scopes)) != len(raw_allowed_scopes)
        ):
            return None
        allowed_scopes = tuple(raw_allowed_scopes)
        if row[4] not in allowed_scopes or requester_scope not in allowed_scopes:
            return None
        created_at = _parse_timestamp(row[6])
        expires_at = _parse_timestamp(row[7])
        if created_at >= expires_at or _utc_now(now) >= expires_at:
            return None
        original = _coerce_prompt_envelope(json.loads(row[1])) if row[1] else None
        if original is not None and key_of(original) != key:
            return None
        redacted_projection = json.loads(row[2])
        if not isinstance(redacted_projection, dict):
            return None
        response = json.loads(row[3])
        if not isinstance(response, Mapping):
            return None
    except Exception:
        # The ledger is a best-effort cache.  Any malformed serialized field
        # is a cache miss, never an authorization bypass or scoring failure.
        return None
    return CacheEnvelopeEntry(
        key=key,
        original_envelope_hash=row[0],
        original_envelope=original,
        redacted_audit_projection=redacted_projection,
        response=response,
        access_scope=row[4],
        allowed_scopes=allowed_scopes,
        created_at=created_at,
        expires_at=expires_at,
    )


def purge_expired(*, now=None):
    cutoff = _utc_now(now).isoformat()
    with closing(_connect()) as conn:
        cursor = conn.execute("DELETE FROM llm_envelope_cache WHERE expires_at <= ?", (cutoff,))
        conn.commit()
        return cursor.rowcount


def _connect():
    root = settings.STORAGE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / "llm_cache.sqlite"), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")  # 多进程评分并发写共享缓存，避免 SQLITE_BUSY
    conn.execute(
        "CREATE TABLE IF NOT EXISTS llm_cache ("
        "key TEXT PRIMARY KEY, model TEXT, request TEXT, response TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS llm_envelope_cache ("
        "key TEXT PRIMARY KEY, original_envelope_hash TEXT NOT NULL, "
        "original_envelope TEXT, redacted_projection TEXT NOT NULL, response TEXT NOT NULL, "
        "access_scope TEXT NOT NULL, allowed_scopes TEXT NOT NULL, "
        "created_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    return conn
