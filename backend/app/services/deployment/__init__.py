"""Deployment readiness services; all audits are read-only and fail closed."""

from backend.app.services.deployment.cutover_inventory import (
    build_core_cutover_inventory,
)


__all__ = ["build_core_cutover_inventory"]
