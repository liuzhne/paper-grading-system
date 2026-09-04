"""Application-layer observability boundary for the pure scoring Core."""

from __future__ import annotations

from collections.abc import Mapping

from backend.app.services.llm_observability import observation
from backend.app.services.scoring.core.engine import score_submission


def _mapping(value):
    if isinstance(value, Mapping):
        return value
    method = getattr(value, "to_mapping", None)
    if not callable(method):
        raise TypeError("scoring request must be a mapping or Core DTO")
    return method()


def score_submission_observed(
    *,
    request,
    checker_registry,
    llm_runtime,
    profile,
    organization_id: str | None = None,
    batch_job_id: str | None = None,
    score_fn=score_submission,
):
    """Trace one Core run without making Core depend on infrastructure.

    A persisted ``ScoringRun`` does not exist until after execution.  The
    immutable request idempotency key is therefore the correlation identity
    during execution and is persisted on the eventual run unchanged.
    """

    value = _mapping(request)
    document = value["document"]
    plan = value["plan"]
    runtime = value["runtime_identity"]
    metadata = {
        "scoring_request_id": value["idempotency_key"],
        "organization_id": organization_id,
        "batch_job_id": batch_job_id,
        "document_snapshot_hash": document["document_snapshot_hash"],
        "rubric_snapshot_hash": plan["rubric_snapshot_hash"],
        "plan_hash": plan["plan_hash"],
        "policy_hash": plan["policy_hash"],
        "profile_key": runtime["profile_key"],
        "profile_version": runtime["profile_version"],
        "prompt_version": runtime["prompt_version"],
        "provider": runtime["provider"]["name"],
        "model": runtime["provider"]["model"],
        "rescore_generation": value["rescore_generation"],
    }
    with observation("scoring_run", as_type="chain", metadata=metadata) as span:
        outcome = score_fn(
            request=request,
            checker_registry=checker_registry,
            llm_runtime=llm_runtime,
            profile=profile,
        )
        mapped = outcome.to_mapping()
        span.update(
            output={
                "status": mapped["status"],
                "review_issue_count": len(mapped["review_issues"]),
                "final_total_present": mapped["final_total"] is not None,
            }
        )
        return outcome


__all__ = ["score_submission_observed"]
