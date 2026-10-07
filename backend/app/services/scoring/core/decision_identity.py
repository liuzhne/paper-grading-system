"""Identity of one semantic rule decision, for the rule-level decision ledger.

The identity hashes the complete provider envelope: document snapshot, rule and
criterion snapshots, rubric identity, selected evidence, provider identity
(model, sampling, thinking, response format), prompt version and profile
extensions.  Any change that can alter the judgment therefore misses the
ledger.  Erring wide is deliberate: a missed reuse costs one call, a wrong
reuse silently replays a stale judgment.
"""

from __future__ import annotations

from backend.app.services.scoring.core.canonical import canonical_sha256


DECISION_IDENTITY_SCHEMA = "rule-decision-identity@1"


def rule_decision_identity(envelope) -> str:
    to_mapping = getattr(envelope, "to_mapping", None)
    value = to_mapping() if callable(to_mapping) else envelope
    return canonical_sha256(
        {"schema_version": DECISION_IDENTITY_SCHEMA, "envelope": value}
    )


__all__ = ["DECISION_IDENTITY_SCHEMA", "rule_decision_identity"]
