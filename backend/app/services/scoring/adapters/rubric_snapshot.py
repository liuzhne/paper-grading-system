"""Load an authoritative published rubric graph into a detached Core DTO."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from sqlalchemy import select

from backend.app.db import models
from backend.app.services.rubrics import lifecycle
from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.contracts import CompiledRubricSnapshot


_GRAPH_FIELDS = {
    "schema_version",
    "rubric_version_id",
    "rubric_status",
    "compilation_status",
    "compilation_published_at",
    "compilation_final_version_hash",
    "version_hash",
    "hash_scheme",
    "business_profile_key",
    "version_content",
    "compiled_snapshot",
}
_VERSION_CONTENT_FIELDS = {
    "rubric",
    "criteria",
    "compilation",
    "artifacts",
    "version",
    "rules",
}


def _require_closed(value, fields: set[str], label: str) -> dict:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")
    return deepcopy(dict(value))


def _assessment_mode(scoring_mode: object) -> object:
    return {
        "deductive": "deduct",
        "banded": "band",
    }.get(scoring_mode, scoring_mode)


def _compiled_from_version_content(
    content: Mapping[str, object],
    *,
    rubric_version_id: str,
    version_hash: str,
    hash_scheme: str,
    business_profile_key: str,
) -> dict:
    rubric = content["rubric"]
    version = content["version"]
    criteria = [
        {
            "criterion_code": item["code"],
            "name": item["name"],
            "max_score": item["max_score"],
            "weight": item["weight"],
            "assessment_mode": _assessment_mode(item["scoring_mode"]),
        }
        for item in content["criteria"]
    ]
    rules = []
    for item in content["rules"]:
        source = item["rule"]
        rules.append(
            {
                "schema_version": "atomic-rule-snapshot@1",
                "rule_code": source["rule_code"],
                "criterion_code": item["criterion_code"],
                "direction": source["direction"],
                "effect_type": source["effect_type"],
                "judge_type": source["judge_type"],
                "checker_key": source["checker_key"],
                "checker_version": None,
                "checker_params": deepcopy(source["checker_params"]),
                "evidence_policy": deepcopy(source["evidence_policy"]),
                "max_points": source["max_points"],
                "repeat_policy": source["repeat_policy"],
                "cap_points": source["cap_points"],
                "depends_on_rule_codes": deepcopy(source["depends_on_rule_codes"]),
                "mutex_group": source["mutex_group"],
                "levels": [
                    {
                        "level_code": level["level_code"],
                        "points": level["points"],
                        "descriptor": level["descriptor"],
                        "positive_example": level["positive_example"],
                        "negative_example": level["negative_example"],
                        "display_order": level["display_order"],
                    }
                    for level in item["levels"]
                ],
            }
        )
    result = {
        "schema_version": "compiled-rubric-snapshot@1",
        "rubric_source_kind": "published_version",
        "rubric_version_id": rubric_version_id,
        "version_hash": version_hash,
        "hash_scheme": hash_scheme,
        "business_profile_key": business_profile_key,
        "total_score": rubric["total_score"],
        "criteria": sorted(criteria, key=lambda item: item["criterion_code"]),
        "atomic_rules": sorted(rules, key=lambda item: item["rule_code"]),
        "global_policy": deepcopy(version["global_policy"]),
        "rubric_snapshot_hash": "0" * 64,
    }
    for rule in result["atomic_rules"]:
        rule["levels"].sort(
            key=lambda level: (level["display_order"], level["level_code"])
        )
    normalized = CompiledRubricSnapshot.from_mapping(result).to_mapping()
    projection = deepcopy(normalized)
    projection.pop("rubric_snapshot_hash")
    normalized["rubric_snapshot_hash"] = canonical_sha256(
        {"scheme": "compiled-rubric-snapshot-v1", **projection}
    )
    return normalized


def _snapshot_hash(value: Mapping[str, object]) -> str:
    projection = deepcopy(dict(value))
    projection.pop("rubric_snapshot_hash", None)
    projection["criteria"] = sorted(
        projection["criteria"], key=lambda item: item["criterion_code"]
    )
    projection["atomic_rules"] = sorted(
        projection["atomic_rules"], key=lambda item: item["rule_code"]
    )
    for rule in projection["atomic_rules"]:
        rule["levels"] = sorted(
            rule["levels"],
            key=lambda item: (item["display_order"], item["level_code"]),
        )
    return canonical_sha256({"scheme": "compiled-rubric-snapshot-v1", **projection})


def _validate_profile_scheme(
    *,
    hash_scheme: str,
    business_profile_key: str,
    expected_profile_key: str,
    content: Mapping[str, object],
) -> None:
    if business_profile_key != expected_profile_key:
        raise ValueError("published rubric business profile does not match expectation")
    if hash_scheme == "rubric-content-v1":
        if business_profile_key != "thesis":
            raise ValueError("rubric-content-v1 is valid only for the backfilled thesis profile")
        if "business_profile_key" in content["version"]:
            raise ValueError("rubric-content-v1 must not fold profile into version content")
    elif hash_scheme == "rubric-content-v2":
        if content["version"].get("business_profile_key") != business_profile_key:
            raise ValueError("rubric-content-v2 profile identity does not match version content")
    else:
        raise ValueError(f"unsupported rubric hash scheme: {hash_scheme}")


class CompiledRubricSnapshotLoader:
    """Validate publication/provenance identity and return a detached snapshot."""

    def load(self, *, version_graph, expected_profile_key: str):
        graph = _require_closed(version_graph, _GRAPH_FIELDS, "version graph")
        if graph["schema_version"] != "stored-rubric-version-graph@1":
            raise ValueError("unsupported stored rubric version graph schema")
        if graph["rubric_status"] != "published":
            raise ValueError("rubric version is not published")
        if graph["compilation_status"] != "validated":
            raise ValueError("rubric compilation is not validated")
        if graph["compilation_published_at"] is None:
            raise ValueError("rubric compilation has no published sign-off")

        content = _require_closed(
            graph["version_content"], _VERSION_CONTENT_FIELDS, "version content graph"
        )
        _validate_profile_scheme(
            hash_scheme=graph["hash_scheme"],
            business_profile_key=graph["business_profile_key"],
            expected_profile_key=expected_profile_key,
            content=content,
        )
        calculated_version_hash = canonical_sha256(content)
        hashes = {
            graph["version_hash"],
            graph["compilation_final_version_hash"],
            calculated_version_hash,
        }
        if len(hashes) != 1:
            raise ValueError("published version, compilation and content graph hashes disagree")

        snapshot = CompiledRubricSnapshot.from_mapping(
            deepcopy(graph["compiled_snapshot"])
        ).to_mapping()
        identity_pairs = (
            (snapshot["rubric_version_id"], graph["rubric_version_id"], "version id"),
            (snapshot["version_hash"], graph["version_hash"], "version hash"),
            (snapshot["hash_scheme"], graph["hash_scheme"], "hash scheme"),
            (
                snapshot["business_profile_key"],
                graph["business_profile_key"],
                "business profile",
            ),
        )
        for actual, expected, label in identity_pairs:
            if actual != expected:
                raise ValueError(f"compiled snapshot {label} disagrees with version graph")
        if snapshot["rubric_snapshot_hash"] != _snapshot_hash(snapshot):
            raise ValueError("compiled rubric snapshot hash does not match content")

        expected_snapshot = _compiled_from_version_content(
            content,
            rubric_version_id=graph["rubric_version_id"],
            version_hash=graph["version_hash"],
            hash_scheme=graph["hash_scheme"],
            business_profile_key=graph["business_profile_key"],
        )
        if snapshot != expected_snapshot:
            raise ValueError("compiled snapshot content disagrees with authoritative version graph")
        return CompiledRubricSnapshot.from_mapping(snapshot)

    def load_from_session(
        self,
        *,
        session,
        rubric_version_id: str,
        expected_profile_key: str,
    ):
        version = session.get(models.RubricVersion, rubric_version_id)
        if version is None:
            raise ValueError("rubric version does not exist")
        rubric = session.get(models.Rubric, version.rubric_id)
        compilation = session.get(models.RubricCompilation, version.compilation_id)
        if rubric is None or compilation is None:
            raise ValueError("rubric version provenance graph is incomplete")
        if rubric.status != "published" or rubric.published_at is None:
            raise ValueError("rubric version is not published")
        if compilation.status != "validated" or compilation.published_at is None:
            raise ValueError("rubric compilation is not validated and published")
        if version.version_hash != compilation.final_version_hash:
            raise ValueError("rubric version hash disagrees with compilation hash")

        profile_key = getattr(version, "business_profile_key", None)
        hash_scheme = getattr(version, "hash_scheme", None)
        if profile_key != expected_profile_key:
            raise ValueError("rubric version business profile does not match expectation")
        if hash_scheme == "rubric-content-v1" and profile_key != "thesis":
            raise ValueError("rubric-content-v1 is valid only for thesis")
        if hash_scheme not in {"rubric-content-v1", "rubric-content-v2"}:
            raise ValueError("unsupported rubric hash scheme")

        calculated_hash = lifecycle._canonical_version_hash(
            session, rubric, compilation, version
        )
        if calculated_hash != version.version_hash:
            raise ValueError("rubric version hash does not match full provenance graph")

        criteria_rows = session.scalars(
            select(models.RubricCriterion).where(
                models.RubricCriterion.rubric_id == rubric.id
            )
        ).all()
        rule_rows = session.scalars(
            select(models.AtomicRule).where(
                models.AtomicRule.rubric_version_id == version.id
            )
        ).all()
        criteria = [
            {
                "criterion_code": item.code,
                "name": item.name,
                "max_score": item.max_score,
                "weight": item.weight,
                "assessment_mode": _assessment_mode(item.scoring_mode),
            }
            for item in criteria_rows
        ]
        rules = []
        for item in rule_rows:
            criterion = next(
                (candidate for candidate in criteria_rows if candidate.id == item.criterion_id),
                None,
            )
            if criterion is None:
                raise ValueError("atomic rule criterion is outside published rubric")
            levels = session.scalars(
                select(models.RuleLevel).where(
                    models.RuleLevel.atomic_rule_id == item.id
                )
            ).all()
            rules.append(
                {
                    "schema_version": "atomic-rule-snapshot@1",
                    "rule_code": item.rule_code,
                    "criterion_code": criterion.code,
                    "direction": item.direction,
                    "effect_type": item.effect_type,
                    "judge_type": item.judge_type,
                    "checker_key": item.checker_key,
                    "checker_version": None,
                    "checker_params": deepcopy(item.checker_params),
                    "evidence_policy": deepcopy(item.evidence_policy),
                    "max_points": item.max_points,
                    "repeat_policy": item.repeat_policy,
                    "cap_points": item.cap_points,
                    "depends_on_rule_codes": deepcopy(item.depends_on_rule_codes),
                    "mutex_group": item.mutex_group,
                    "levels": [
                        {
                            "level_code": level.level_code,
                            "points": level.points,
                            "descriptor": level.descriptor,
                            "positive_example": level.positive_example,
                            "negative_example": level.negative_example,
                            "display_order": level.display_order,
                        }
                        for level in sorted(
                            levels,
                            key=lambda level: (level.display_order, level.level_code),
                        )
                    ],
                }
            )

        raw_snapshot = {
            "schema_version": "compiled-rubric-snapshot@1",
            "rubric_source_kind": "published_version",
            "rubric_version_id": version.id,
            "version_hash": version.version_hash,
            "hash_scheme": hash_scheme,
            "business_profile_key": profile_key,
            "total_score": rubric.total_score,
            "criteria": sorted(criteria, key=lambda item: item["criterion_code"]),
            "atomic_rules": sorted(rules, key=lambda item: item["rule_code"]),
            "global_policy": deepcopy(version.global_policy),
            "rubric_snapshot_hash": "0" * 64,
        }
        normalized = CompiledRubricSnapshot.from_mapping(raw_snapshot).to_mapping()
        normalized["rubric_snapshot_hash"] = _snapshot_hash(normalized)
        return CompiledRubricSnapshot.from_mapping(normalized)


__all__ = ["CompiledRubricSnapshotLoader"]
