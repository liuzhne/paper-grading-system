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
from contextlib import closing
from datetime import datetime
from datetime import timezone

from backend.app.core.config import settings

# ⚠️ 凡改动评分 prompt/输入构造，务必 bump 本版本号以使旧缓存失效（设计§7：prompt 进哈希）。
PROMPT_VERSION = "2026-05-29-1"


def build_request(scorer, criterion, candidates, structure_checks, rubric_version, anchors=None):
    """构造进入哈希且作为审计留存的"完整输入"。
    校准锚点并入哈希 → 锚点变化即缓存失效（保可复现，设计§7）。"""
    return {
        "prompt_version": PROMPT_VERSION,
        "provider": getattr(scorer, "provider", ""),
        "model": getattr(scorer, "model_name", ""),
        "model_version": getattr(scorer, "model_version", ""),
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
        "candidates": [{"chunk_id": c.get("chunk_id"), "text": c.get("text")} for c in (candidates or [])],
        "structure_checks": structure_checks,
        "calibration_anchors": anchors or [],
    }


def key_of(request):
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


def _connect():
    root = settings.STORAGE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / "llm_cache.sqlite"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS llm_cache ("
        "key TEXT PRIMARY KEY, model TEXT, request TEXT, response TEXT, created_at TEXT)"
    )
    return conn
