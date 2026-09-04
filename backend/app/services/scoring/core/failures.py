"""Transport-neutral projection of task failures onto stable Core issues."""

from __future__ import annotations


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
    return "RULE_EXECUTION_FAILED", "rule execution failed"


__all__ = ["project_rule_execution_failure"]
