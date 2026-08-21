"""Compatibility adapters at the boundary of the scoring Core."""

from backend.app.services.scoring.adapters.legacy_rubric import LegacyRubricError
from backend.app.services.scoring.adapters.legacy_rubric import adapt_legacy_rubric
from backend.app.services.scoring.adapters.legacy_rubric import apply_legacy_deductions
from backend.app.services.scoring.adapters.legacy_rubric import apply_legacy_direct_compat

__all__ = [
    "LegacyRubricError",
    "adapt_legacy_rubric",
    "apply_legacy_deductions",
    "apply_legacy_direct_compat",
]
