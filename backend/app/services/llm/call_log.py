"""每次模型调用一行诊断日志：只记受控字段，不记请求或响应正文。

`debug_logging` 记录原文，受保护部署禁用；这里的字段在生产也可以常开：
- 配置的模型、实际路由到的模型（`openrouter/free` 这类路由每次可能不同）；
- 上游服务商（聚合平台返回的 ``provider``）；
- HTTP 状态、错误类别和厂商错误码（如智谱的 1302/1305），用来区分并发超额、
  频率超额和服务繁忙。
"""

from __future__ import annotations

import logging
import re


logger = logging.getLogger("paper_grading.llm.calls")
_UNSAFE = re.compile(r"[^\w.:/@+-]")


def _field(value, limit: int = 80) -> str:
    """标识类字段只保留常见字符并截断，防止日志注入或夹带正文。"""

    if value is None or value == "":
        return "-"
    return _UNSAFE.sub("_", str(value))[:limit] or "-"


def _envelope_error(data):
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return None, None
    metadata = error.get("metadata") if isinstance(error.get("metadata"), dict) else {}
    return error.get("code"), metadata.get("provider_name")


def log_call_succeeded(provider, model, data, *, elapsed_ms, attempt):
    """HTTP 2xx。响应体里带 ``error`` 的（“成功状态中返回错误”）单独记成失败。"""

    routed = data.get("model") if isinstance(data, dict) else None
    upstream = data.get("provider") if isinstance(data, dict) else None
    if isinstance(data, dict) and data.get("error"):
        code, error_upstream = _envelope_error(data)
        logger.warning(
            "llm_call_error_envelope provider=%s model=%s routed_model=%s upstream=%s "
            "provider_code=%s elapsed_ms=%d attempt=%d",
            _field(provider), _field(model), _field(routed), _field(error_upstream or upstream),
            _field(code), elapsed_ms, attempt + 1,
        )
        return
    logger.info(
        "llm_call provider=%s model=%s routed_model=%s upstream=%s elapsed_ms=%d attempt=%d",
        _field(provider), _field(model), _field(routed), _field(upstream), elapsed_ms, attempt + 1,
    )


def log_call_failed(provider, model, projected, *, attempt, attempts, will_retry):
    logger.warning(
        "llm_call_failed provider=%s model=%s code=%s status=%s provider_code=%s provider_type=%s "
        "attempt=%d/%d retry=%s",
        _field(provider), _field(model), _field(projected.code), _field(projected.http_status),
        _field(projected.provider_error_code), _field(projected.provider_error_type),
        attempt + 1, attempts, "yes" if will_retry else "no",
    )
