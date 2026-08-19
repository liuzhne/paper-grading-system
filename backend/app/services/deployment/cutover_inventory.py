"""Read-only inventory for deciding whether active rubrics can execute on Core.

This service owns no release approval.  It proves only the rubric/plan portion
of the cutover gate and therefore always leaves the production switch flag
false, even when its own blocker list is empty.
"""

from __future__ import annotations

from copy import deepcopy

from sqlalchemy import select

from backend.app.db import models
from backend.app.services.scoring.adapters.legacy_rubric import (
    LegacyRubricAdapter,
)
from backend.app.services.scoring.adapters.rubric_snapshot import (
    CompiledRubricSnapshotLoader,
)
from backend.app.services.scoring.core.execution_plan import (
    RuleExecutionPlanBuilder,
)
from backend.app.services.scoring.engine import _build_legacy_execution_plan
from backend.app.services.scoring.core.policy import build_corrected_thesis_policy
from backend.app.services.scoring.core.policy import (
    validate_weight_configuration,
)
from backend.app.services.scoring.profiles.registry import get_profile
from backend.app.services.scoring.profiles.registry import get_profile_by_key


SCHEMA_VERSION = "core-cutover-inventory@1"
_DOCUMENT_SCHEMA = "document-snapshot@1"
_PUBLISHED_REMEDIATION = (
    "deep-clone the rubric, repair/review its provenance graph, publish a new "
    "immutable version, then repin affected batches"
)
_LEGACY_REMEDIATION = (
    "migrate the rubric through import/review/publish or repair its explicit "
    "direct/composite compatibility definition"
)


def _plain(value):
    method = getattr(value, "to_mapping", None)
    if callable(method):
        return method()
    return deepcopy(value)


def _batch_projection(*, kind, batch):
    return {
        "batch_kind": kind,
        "batch_id": batch.id,
        "status": batch.status,
        "owner_id": batch.owner_id,
    }


def _blocker(
    *,
    code,
    target,
    message,
    remediation,
):
    return {
        "code": code,
        "rubric_id": target["rubric_id"],
        "rubric_version_id": target["rubric_version_id"],
        "affected_batch_ids": [
            item["batch_id"] for item in target["affected_batches"]
        ],
        "owner_ids": deepcopy(target["owner_ids"]),
        "message": message,
        "remediation": remediation,
    }


def _published_target(db, target):
    version_id = target["rubric_version_id"]
    version = db.get(models.RubricVersion, version_id)
    if version is None or version.rubric_id != target["rubric_id"]:
        return _blocker(
            code="PUBLISHED_VERSION_INVALID",
            target=target,
            message="published rubric version failed immutable provenance validation",
            remediation=_PUBLISHED_REMEDIATION,
        )

    evaluation_batches = [
        item
        for item in target["_batch_rows"]
        if isinstance(item, models.EvaluationBatch)
    ]
    if any(
        item.business_profile_key != version.business_profile_key
        for item in evaluation_batches
    ):
        return _blocker(
            code="BATCH_PROFILE_MISMATCH",
            target=target,
            message="active batch profile key does not match its pinned rubric version",
            remediation=(
                "create a correctly profiled evaluation batch pinned to the intended "
                "published version"
            ),
        )
    requested_versions = sorted(
        {item.business_profile_version for item in evaluation_batches}
    )
    try:
        if requested_versions:
            if len(requested_versions) != 1:
                raise ValueError("active batches disagree on business profile version")
            profile = get_profile(
                profile_key=version.business_profile_key,
                profile_version=requested_versions[0],
            )
        else:
            profile = get_profile_by_key(version.business_profile_key)
    except (LookupError, ValueError):
        return _blocker(
            code="PROFILE_NOT_EXECUTABLE",
            target=target,
            message="published rubric profile identity is not registered by this deployment",
            remediation=(
                "deploy the exact immutable profile implementation or repin batches "
                "to a supported published version"
            ),
        )

    try:
        snapshot = CompiledRubricSnapshotLoader().load_from_session(
            session=db,
            rubric_version_id=version.id,
            expected_profile_key=version.business_profile_key,
        )
    except (KeyError, LookupError, TypeError, ValueError):
        return _blocker(
            code="PUBLISHED_VERSION_INVALID",
            target=target,
            message="published rubric version failed immutable provenance validation",
            remediation=_PUBLISHED_REMEDIATION,
        )
    try:
        registry = profile.build_checker_registry()
        plan = RuleExecutionPlanBuilder(
            checker_registry=registry,
            policy_compiler_version="scoring-policy-compiler@1",
            engine_contract_version="scoring-core@1",
        ).build(
            rubric=snapshot,
            profile=profile,
            document_schema_version=_DOCUMENT_SCHEMA,
        )
    except (KeyError, LookupError, TypeError, ValueError) as exc:
        message = str(exc).casefold()
        code = (
            "CHECKER_NOT_EXECUTABLE"
            if "checker" in message
            else "PLAN_BUILD_FAILED"
        )
        return _blocker(
            code=code,
            target=target,
            message="published rubric cannot build an executable Core plan",
            remediation=(
                "repair checker/profile/policy/rule identities in a deep-cloned "
                "rubric and publish a new immutable version"
            ),
        )

    snapshot_value = snapshot.to_mapping()
    plan_value = plan.to_mapping()
    target["identity"] = {
        "business_profile_key": profile.profile_key,
        "business_profile_version": profile.profile_version,
        "rubric_version_hash": snapshot_value["version_hash"],
        "rubric_hash_scheme": snapshot_value["hash_scheme"],
        "rubric_snapshot_hash": snapshot_value["rubric_snapshot_hash"],
        "policy_hash": plan_value["policy_hash"],
        "plan_hash": plan_value["plan_hash"],
        "plan_schema_version": plan_value["schema_version"],
        "checker_manifest": sorted(plan_value["checker_manifest"]),
        "compatibility_node_kinds": [],
    }
    return None


def _legacy_target(db, target):
    rubric = db.get(models.Rubric, target["rubric_id"])
    compilations = db.scalars(
        select(models.RubricCompilation).where(
            models.RubricCompilation.rubric_id == target["rubric_id"]
        )
    ).all()
    if rubric is None or compilations:
        return _blocker(
            code="LEGACY_NOT_EXECUTABLE",
            target=target,
            message="legacy rubric cannot be represented by approved compatibility nodes",
            remediation=_LEGACY_REMEDIATION,
        )
    try:
        profile = get_profile_by_key("thesis")
        criteria = sorted(
            list(rubric.criteria),
            key=lambda item: (item.display_order, item.code),
        )
        weights = validate_weight_configuration(
            criteria,
            total_score=rubric.total_score,
        )
        policy = build_corrected_thesis_policy(
            rubric.total_score,
            weights.mode,
        )
        adapter = LegacyRubricAdapter()
        snapshot = adapter.adapt(
            rubric=rubric,
            criteria=criteria,
            policy_snapshot=_plain(policy),
            business_profile_key=profile.profile_key,
            compilation_rows=(),
        )
        compatibility = adapter.adapt_compatibility_nodes(
            criteria=criteria,
            rubric_source_kind="legacy_unversioned",
        )
        registry = profile.build_checker_registry()
        plan = _build_legacy_execution_plan(
            rubric_snapshot=snapshot,
            profile=profile,
            registry=registry,
            compatibility_nodes=compatibility,
        )
    except (KeyError, LookupError, TypeError, ValueError):
        return _blocker(
            code="LEGACY_NOT_EXECUTABLE",
            target=target,
            message="legacy rubric cannot be represented by approved compatibility nodes",
            remediation=_LEGACY_REMEDIATION,
        )

    snapshot_value = snapshot.to_mapping()
    plan_value = plan.to_mapping()
    target["identity"] = {
        "business_profile_key": profile.profile_key,
        "business_profile_version": profile.profile_version,
        "rubric_version_hash": None,
        "rubric_hash_scheme": None,
        "rubric_snapshot_hash": snapshot_value["rubric_snapshot_hash"],
        "policy_hash": plan_value["policy_hash"],
        "plan_hash": plan_value["plan_hash"],
        "plan_schema_version": plan_value["schema_version"],
        "checker_manifest": sorted(plan_value["checker_manifest"]),
        "compatibility_node_kinds": sorted(
            {item.to_mapping()["node_kind"] for item in compatibility}
        ),
    }
    return None


def _active_batches(db):
    grading = db.scalars(
        select(models.GradingBatch).order_by(models.GradingBatch.id)
    ).all()
    evaluations = db.scalars(
        select(models.EvaluationBatch)
        .where(models.EvaluationBatch.status == "active")
        .order_by(models.EvaluationBatch.id)
    ).all()
    return grading, evaluations


def build_core_cutover_inventory(db):
    """Return a deterministic read-only inventory of active rubric targets."""

    grading_batches, evaluation_batches = _active_batches(db)
    grouped = {}
    for kind, rows in (
        ("grading_batch", grading_batches),
        ("evaluation_batch", evaluation_batches),
    ):
        for batch in rows:
            key = (batch.rubric_id, batch.rubric_version_id)
            grouped.setdefault(key, []).append((kind, batch))

    targets = []
    blockers = []
    for (rubric_id, version_id), batch_entries in sorted(
        grouped.items(),
        key=lambda item: (item[0][0], item[0][1] or ""),
    ):
        rubric = db.get(models.Rubric, rubric_id)
        batches = sorted(
            (
                _batch_projection(kind=kind, batch=batch)
                for kind, batch in batch_entries
            ),
            key=lambda item: (item["batch_id"], item["batch_kind"]),
        )
        owner_ids = sorted(
            {
                owner_id
                for owner_id in (
                    *[item["owner_id"] for item in batches],
                    getattr(rubric, "owner_id", None),
                )
                if owner_id is not None
            }
        )
        target = {
            "rubric_id": rubric_id,
            "rubric_name": None if rubric is None else rubric.name,
            "rubric_version_id": version_id,
            "rubric_source_kind": (
                "published_version"
                if version_id is not None
                else "legacy_unversioned"
            ),
            "owner_ids": owner_ids,
            "affected_batches": batches,
            "status": "blocked",
            "identity": None,
            "blocker_codes": [],
            "_batch_rows": [batch for _kind, batch in batch_entries],
        }
        blocker = (
            _published_target(db, target)
            if version_id is not None
            else _legacy_target(db, target)
        )
        if blocker is None:
            target["status"] = "ready"
        else:
            target["blocker_codes"] = [blocker["code"]]
            blockers.append(blocker)
        target.pop("_batch_rows")
        targets.append(target)

    blockers.sort(
        key=lambda item: (
            item["rubric_id"],
            item["rubric_version_id"] or "",
            item["code"],
        )
    )
    ready = sum(item["status"] == "ready" for item in targets)
    clear = not blockers
    return {
        "schema_version": SCHEMA_VERSION,
        "authorization_scope": "rubric_inventory_only",
        "summary": {
            "active_grading_batches": len(grading_batches),
            "active_evaluation_batches": len(evaluation_batches),
            "targets": len(targets),
            "ready_targets": ready,
            "blocked_targets": len(targets) - ready,
            "blockers": len(blockers),
        },
        "targets": targets,
        "blockers": blockers,
        "inventory_clear": clear,
        "cutover_authorized": clear,
        "production_default_switch_authorized": False,
    }


__all__ = ["SCHEMA_VERSION", "build_core_cutover_inventory"]
