"""Transport-neutral projection of task failures onto stable Core issues."""

from __future__ import annotations

import re


_SAFE_EXCEPTION_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")


def project_rule_execution_failure(exc: Exception) -> tuple[str, str]:
    """Use duck-typed safe error contracts without importing adapters."""

    direct_code = getattr(exc, "code", None)
    if isinstance(direct_code, str) and direct_code.strip():
        return direct_code.strip().upper(), "rule input could not be prepared safely"
    provider_error = getattr(exc, "error", None)
    provider_code = getattr(provider_error, "code", None)
    if isinstance(provider_code, str) and provider_code.strip():
        normalized = provider_code.strip().upper()
        if not normalized.startswith("PROVIDER_"):
            normalized = "PROVIDER_" + normalized
        return normalized, "semantic provider request failed"
    exception_type = type(exc).__name__
    if not _SAFE_EXCEPTION_TYPE.fullmatch(exception_type):
        exception_type = "Exception"
    return (
        "RULE_EXECUTION_FAILED",
        f"rule execution failed (exception_type={exception_type})",
    )


__all__ = ["project_rule_execution_failure"]
