"""Non-authoritative artifact boundary for legacy/Core rollout comparison."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from threading import RLock


class ComparisonArtifactSink:
    """Process-local sink used until comparison artifacts gain a durable port.

    Comparison data is deliberately separate from ``ScoringRun``.  Consumers
    may inspect snapshots for observability, but report/export/review queries
    cannot accidentally treat a Core candidate as an authoritative run.
    """

    def __init__(self):
        self._artifacts: list[dict] = []
        self._lock = RLock()

    def record(self, *, artifact) -> None:
        if not isinstance(artifact, Mapping):
            raise TypeError("comparison artifact must be a mapping")
        value = deepcopy(dict(artifact))
        if value.get("schema_version") != "scoring-comparison-artifact@1":
            raise ValueError("unsupported scoring comparison artifact schema")
        if value.get("authority") != "non_authoritative":
            raise ValueError("comparison artifact must be non_authoritative")
        if not isinstance(value.get("legacy_run_id"), str) or not value[
            "legacy_run_id"
        ]:
            raise ValueError("comparison artifact requires a legacy run identity")
        if value.get("candidate_run_id") is not None:
            raise ValueError("M3 comparison candidate must not reference a ScoringRun")
        with self._lock:
            self._artifacts.append(value)

    def snapshot(self) -> tuple[dict, ...]:
        with self._lock:
            return tuple(deepcopy(self._artifacts))

    def clear(self) -> None:
        with self._lock:
            self._artifacts.clear()


_comparison_artifact_sink = ComparisonArtifactSink()


def get_comparison_artifact_sink() -> ComparisonArtifactSink:
    return _comparison_artifact_sink


__all__ = ["ComparisonArtifactSink", "get_comparison_artifact_sink"]
