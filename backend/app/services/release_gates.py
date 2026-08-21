"""Database-backed release-gate relationships and exact-candidate approval."""

from copy import deepcopy
from datetime import datetime
from datetime import timezone
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import ReleaseGateApproval
from backend.app.db.models import ReleaseGateProfile
from backend.app.db.models import ReleaseGateRun
from backend.app.db.models import Rubric
from backend.app.db.models import RubricCompilation
from backend.app.db.models import RubricVersion
from backend.app.eval.gating import APPROVAL_SCHEMA
from backend.app.eval.gating import GateValidationError
from backend.app.eval.gating import assert_public_record_safe
from backend.app.eval.gating import evaluation_sha256
from backend.app.eval.gating import finalize_gate
from backend.app.eval.gating import validate_gate_candidate_record
from backend.app.eval.gating import validate_gate_policy
from backend.app.services.batch_scoring.jobs import evaluate_observation_policy
from backend.app.services.batch_scoring.jobs import validate_observation_policy


PROFILE_SCHEMA = "paper-grading/release-gate-profile@1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ROLLING_REVISIONS = {"latest", "stable", "production", "default", "current"}
_GATE03_EVIDENCE_SCHEMA = "paper-grading/gate-03-evidence@1"
_GATE03_CRITERION_CODES = {"T01", "T02", "T03", "T04", "T05", "T06"}
_GATE03_OPERATIONAL_METRICS = {
    "sample_size",
    "completed_items",
    "invalid_evidence_rate",
    "unauthorized_rule_rate",
    "manual_review_rate",
    "cache_hit_rate",
    "cache_by_profile_and_rubric_version",
    "checker_failure_rate",
    "llm_failure_rate",
    "retry_rate",
    "p50_latency_ms",
    "p95_latency_ms",
    "legacy_core_delta",
    "error_counts",
    "attempt_error_counts",
}
_GATE03_AGGREGATE_METRICS = {
    "qwk",
    "mae",
    "rmse",
    "exact_grade_agreement",
    "adjacent_grade_agreement",
    "review_rate",
    "blocked_rate",
    "invalid_evidence_rate",
}


class ReleaseGateServiceError(ValueError):
    pass


class ReleaseGateConflict(ReleaseGateServiceError):
    pass


def _unique_strings(values):
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _sha(value, path):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ReleaseGateServiceError(
            "%s must be a lowercase SHA-256 digest" % path
        )
    return value


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        raise ReleaseGateServiceError("%s must be a non-empty string" % path)
    return value.strip()


def _exact_keys(value, required, path):
    if not isinstance(value, dict):
        raise ReleaseGateServiceError("%s must be an object" % path)
    missing = sorted(set(required) - set(value))
    unknown = sorted(set(value) - set(required))
    if missing or unknown:
        raise ReleaseGateServiceError(
            "%s keys mismatch (missing=%s, unknown=%s)"
            % (path, missing, unknown)
        )


def _validate_dataset_identity(identity):
    required = {
        "dataset_id",
        "dataset_version",
        "sample_count",
        "dataset_manifest_sha256",
        "sample_ids_sha256",
        "paper_manifest_sha256",
        "truth_sha256",
    }
    _exact_keys(identity, required, "dataset_identity")
    _text(identity["dataset_id"], "dataset_identity.dataset_id")
    _text(identity["dataset_version"], "dataset_identity.dataset_version")
    if (
        isinstance(identity["sample_count"], bool)
        or not isinstance(identity["sample_count"], int)
        or identity["sample_count"] <= 0
    ):
        raise ReleaseGateServiceError(
            "dataset_identity.sample_count must be a positive integer"
        )
    for key in (
        "dataset_manifest_sha256",
        "sample_ids_sha256",
        "paper_manifest_sha256",
        "truth_sha256",
    ):
        _sha(identity[key], "dataset_identity.%s" % key)


def _validate_model_identity(identity):
    required = {
        "provider",
        "model_name",
        "model_version",
        "identity_method",
        "immutable_revision",
        "artifact_identity_sha256",
    }
    _exact_keys(identity, required, "model_identity")
    provider = _text(identity["provider"], "model_identity.provider")
    if provider.lower() == "mock":
        raise ReleaseGateServiceError(
            "release gate profile cannot use the mock provider"
        )
    _text(identity["model_name"], "model_identity.model_name")
    _text(identity["model_version"], "model_identity.model_version")
    method = _text(identity["identity_method"], "model_identity.identity_method")
    if method not in {"provider_immutable_revision", "artifact_sha256"}:
        raise ReleaseGateServiceError("unsupported model identity_method")
    revision = identity.get("immutable_revision")
    if method == "provider_immutable_revision":
        revision = _text(revision, "model_identity.immutable_revision")
        if revision.lower() in _ROLLING_REVISIONS:
            raise ReleaseGateServiceError(
                "model_identity.immutable_revision cannot be a rolling alias"
            )
    elif revision not in (None, ""):
        raise ReleaseGateServiceError(
            "local artifact identity cannot also declare immutable_revision"
        )
    _sha(
        identity["artifact_identity_sha256"],
        "model_identity.artifact_identity_sha256",
    )


def _validate_anchors_identity(identity):
    required = {
        "schema",
        "count",
        "manifest_sha256",
        "holdout_exclusion_proven",
    }
    _exact_keys(identity, required, "anchors_identity")
    _text(identity["schema"], "anchors_identity.schema")
    if (
        isinstance(identity["count"], bool)
        or not isinstance(identity["count"], int)
        or identity["count"] < 0
    ):
        raise ReleaseGateServiceError(
            "anchors_identity.count must be a non-negative integer"
        )
    _sha(identity["manifest_sha256"], "anchors_identity.manifest_sha256")
    if identity["holdout_exclusion_proven"] is not True:
        raise ReleaseGateServiceError(
            "anchors_identity.holdout_exclusion_proven must be true"
        )


def _published_version(db, rubric_id, rubric_version_id):
    rubric = db.get(Rubric, rubric_id)
    version = db.get(RubricVersion, rubric_version_id)
    if rubric is None or version is None or version.rubric_id != rubric_id:
        raise ReleaseGateServiceError(
            "rubric_version_id must belong to rubric_id"
        )
    if rubric.status != "published" or rubric.published_at is None:
        raise ReleaseGateServiceError(
            "release gate profile requires a published immutable RubricVersion"
        )
    eligible = []
    for candidate in db.scalars(
        select(RubricVersion).where(RubricVersion.rubric_id == rubric_id)
    ).all():
        compilation = db.get(RubricCompilation, candidate.compilation_id)
        if (
            compilation is not None
            and compilation.rubric_id == rubric.id
            and compilation.status == "validated"
            and compilation.reviewed_by is not None
            and compilation.reviewed_at is not None
            and compilation.reviewed_at == compilation.published_at
            and compilation.published_at == rubric.published_at
            and compilation.final_version_hash == candidate.version_hash
        ):
            eligible.append(candidate)
    if len(eligible) != 1:
        raise ReleaseGateServiceError(
            "release gate profile requires exactly one published immutable "
            "RubricVersion"
        )
    if eligible[0].id != version.id:
        raise ReleaseGateServiceError(
            "rubric_version_id is not the published immutable RubricVersion"
        )
    return eligible[0]


def _profile_identity(
    *,
    profile,
    version,
):
    return {
        "schema": PROFILE_SCHEMA,
        "gate_key": profile.gate_key,
        "name": profile.name,
        "rubric_id": profile.rubric_id,
        "rubric_version_id": version.id,
        "rubric_version_hash": version.version_hash,
        "rubric_hash_scheme": version.hash_scheme,
        "dataset_identity": deepcopy(profile.dataset_identity),
        "model_identity": deepcopy(profile.model_identity),
        "anchors_identity": deepcopy(profile.anchors_identity),
        "acceptance_thresholds": deepcopy(profile.acceptance_thresholds),
        "regression_tolerances": deepcopy(profile.regression_tolerances),
    }


def _validate_profile_integrity(db, profile):
    version = _published_version(
        db,
        profile.rubric_id,
        profile.rubric_version_id,
    )
    expected = evaluation_sha256(
        _profile_identity(profile=profile, version=version)
    )
    if profile.profile_hash != expected:
        raise ReleaseGateServiceError(
            "profile content does not match profile_hash"
        )
    return version


def create_profile(db: Session, payload, actor_id: str) -> ReleaseGateProfile:
    version = _published_version(
        db,
        payload.rubric_id,
        payload.rubric_version_id,
    )
    dataset_identity = deepcopy(payload.dataset_identity)
    model_identity = deepcopy(payload.model_identity)
    anchors_identity = deepcopy(payload.anchors_identity)
    thresholds = deepcopy(payload.acceptance_thresholds)
    tolerances = deepcopy(payload.regression_tolerances)
    _validate_dataset_identity(dataset_identity)
    _validate_model_identity(model_identity)
    _validate_anchors_identity(anchors_identity)
    try:
        validate_gate_policy(thresholds, tolerances)
    except GateValidationError as exc:
        raise ReleaseGateServiceError(str(exc)) from exc

    exists = db.scalar(
        select(ReleaseGateProfile).where(
            ReleaseGateProfile.gate_key == payload.gate_key,
            ReleaseGateProfile.name == payload.name,
        )
    )
    if exists is not None:
        raise ReleaseGateConflict("release gate profile already exists")

    profile = ReleaseGateProfile(
        gate_key=payload.gate_key,
        name=payload.name,
        rubric_id=payload.rubric_id,
        rubric_version_id=version.id,
        dataset_identity=dataset_identity,
        model_identity=model_identity,
        anchors_identity=anchors_identity,
        acceptance_thresholds=thresholds,
        regression_tolerances=tolerances,
        profile_hash="0" * 64,
        status="active",
        created_by=actor_id,
    )
    profile.profile_hash = evaluation_sha256(
        _profile_identity(profile=profile, version=version)
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def list_profiles(db: Session):
    return db.scalars(
        select(ReleaseGateProfile).order_by(ReleaseGateProfile.created_at.desc())
    ).all()


def get_profile(db: Session, profile_id: str):
    return db.get(ReleaseGateProfile, profile_id)


def resolve_profile(db: Session, profile_id: str):
    profile = db.get(ReleaseGateProfile, profile_id)
    if profile is None:
        raise ReleaseGateServiceError("release gate profile not found")
    if profile.status != "active":
        raise ReleaseGateServiceError("release gate profile is not active")
    _validate_profile_integrity(db, profile)
    return profile


def _candidate_identity_issues(profile, version, candidate):
    issues = []
    expected = (
        ("dataset", candidate.get("dataset"), profile.dataset_identity),
        ("model", candidate.get("model"), profile.model_identity),
    )
    for path, actual, frozen in expected:
        if actual != frozen:
            if isinstance(actual, dict) and isinstance(frozen, dict):
                for key in sorted(set(actual) | set(frozen)):
                    if actual.get(key) != frozen.get(key):
                        issues.append("%s.%s does not match profile" % (path, key))
            else:
                issues.append("%s does not match profile" % path)
    candidate_anchors = candidate.get("anchors") or {}
    for key in ("schema", "count", "manifest_sha256"):
        if candidate_anchors.get(key) != profile.anchors_identity.get(key):
            issues.append("anchors.%s does not match profile" % key)
    candidate_rubric = candidate.get("rubric") or {}
    for key, expected_value in (
        ("version_id", version.id),
        ("version_hash", version.version_hash),
        ("hash_scheme", version.hash_scheme),
    ):
        if candidate_rubric.get(key) != expected_value:
            issues.append("rubric.%s does not match profile" % key)
    return issues


def _candidate_relationship_issues(profile, version, candidate):
    if profile.gate_key == "GATE-03":
        return _gate03_candidate_issues(
            profile,
            version,
            candidate,
            test_only=False,
        )
    issues = _candidate_identity_issues(profile, version, candidate)
    if candidate.get("gating_eligible") is not True:
        issues.append("candidate is not gating eligible")
    if candidate.get("status") != "candidate_awaiting_approval":
        issues.append("candidate status is not awaiting approval")
    if candidate.get("gate_passed") is not False:
        issues.append("candidate must not already be passed")
    return issues


def _comparison_issues(value, path):
    required = {
        "reference_id",
        "reference_sha256",
        "status",
        "metric_deltas",
        "significant_differences",
        "difference_explanations",
    }
    if not isinstance(value, dict):
        return ["%s must be an object" % path]
    issues = [
        "%s.%s is missing" % (path, key)
        for key in sorted(required - set(value))
    ]
    unknown = sorted(set(value) - required)
    if unknown:
        issues.append("%s contains unknown fields: %s" % (path, unknown))
    if value.get("status") != "passed":
        issues.append("%s.status must be passed" % path)
    try:
        _text(value.get("reference_id"), "%s.reference_id" % path)
    except ReleaseGateServiceError as exc:
        issues.append(str(exc))
    try:
        _sha(value.get("reference_sha256"), "%s.reference_sha256" % path)
    except ReleaseGateServiceError as exc:
        issues.append(str(exc))
    if not isinstance(value.get("metric_deltas"), dict):
        issues.append("%s.metric_deltas must be an object" % path)
    significant = value.get("significant_differences")
    explanations = value.get("difference_explanations")
    if not isinstance(significant, list):
        issues.append("%s.significant_differences must be an array" % path)
    if not isinstance(explanations, list):
        issues.append("%s.difference_explanations must be an array" % path)
    elif isinstance(significant, list) and len(explanations) < len(significant):
        issues.append(
            "%s requires an explanation for every significant difference" % path
        )
    return issues


def _gate03_candidate_issues(profile, version, candidate, *, test_only):
    issues = _candidate_identity_issues(profile, version, candidate)
    if profile.gate_key != "GATE-03":
        issues.append("test-only rehearsal requires a GATE-03 profile")
    execution = candidate.get("execution") or {}
    expected_execution = (
        {
            "mode": "test_only",
            "dataset_class": "synthetic",
            "model_class": "immutable_test_fixture",
        }
        if test_only
        else {
            "mode": "formal_release",
            "dataset_class": "external_private_holdout",
            "model_class": "immutable_production",
        }
    )
    if execution != expected_execution:
        issues.append(
            "execution must identify the %s run"
            % ("immutable synthetic test-only" if test_only else "formal production")
        )
    for key, expected in (
        (
            "status",
            "ineligible" if test_only else "candidate_awaiting_approval",
        ),
        ("gating_eligible", not test_only),
        ("gate_passed", False),
        ("production_default_switch_authorized", False),
    ):
        if candidate.get(key) is not expected and candidate.get(key) != expected:
            issues.append("%s must be %r" % (key, expected))
    if test_only and candidate.get("manual_confirmations_pending") != []:
        issues.append("test-only rehearsal cannot await production confirmations")
    if test_only and (candidate.get("anchors") or {}).get(
        "holdout_exclusion_proven"
    ) is not True:
        issues.append("anchors.holdout_exclusion_proven must be true")

    evaluation = candidate.get("evaluation") or {}
    metrics = evaluation.get("metrics") or {}
    for key in sorted(_GATE03_AGGREGATE_METRICS - set(metrics)):
        issues.append("evaluation.metrics.%s is missing" % key)
    dimensions = evaluation.get("per_criterion") or {}
    if set(dimensions) != _GATE03_CRITERION_CODES:
        issues.append("evaluation.per_criterion must contain exactly T01-T06")
    for code, values in dimensions.items():
        if not isinstance(values, dict) or not {"n", "mae", "bias"}.issubset(values):
            issues.append("evaluation.per_criterion.%s requires n, mae and bias" % code)

    evidence = candidate.get("gate03_evidence")
    if not isinstance(evidence, dict):
        return issues + ["gate03_evidence must be an object"]
    if evidence.get("schema") != _GATE03_EVIDENCE_SCHEMA:
        issues.append("gate03_evidence.schema is unsupported")
    operational = evidence.get("operational_metrics")
    if not isinstance(operational, dict):
        issues.append("gate03_evidence.operational_metrics must be an object")
        operational = {}
    for key in sorted(_GATE03_OPERATIONAL_METRICS - set(operational)):
        issues.append("gate03_evidence.operational_metrics.%s is missing" % key)
    expected_source_hash = evaluation_sha256(
        {"policy": evidence.get("observation_policy"), "metrics": operational}
    )
    if evidence.get("observation_source_sha256") != expected_source_hash:
        issues.append("gate03_evidence.observation_source_sha256 does not match")

    policy = evidence.get("observation_policy")
    normalized_policy = None
    try:
        normalized_policy = validate_observation_policy(policy)
    except ValueError as exc:
        issues.append("gate03_evidence.observation_policy: %s" % exc)
    if normalized_policy is not None:
        expected_policy_hash = evaluation_sha256(normalized_policy)
        if evidence.get("observation_policy_sha256") != expected_policy_hash:
            issues.append("gate03_evidence.observation_policy_sha256 does not match")
        try:
            expected_report = evaluate_observation_policy(
                normalized_policy, operational
            )
        except ValueError as exc:
            issues.append("gate03_evidence.operational_metrics: %s" % exc)
        else:
            if evidence.get("observation_report") != expected_report:
                issues.append("gate03_evidence.observation_report does not match policy")
            if expected_report.get("ready_for_gate") is not True:
                issues.append("gate03 observation policy did not pass")
            if expected_report.get("production_default_switch_authorized") is not False:
                issues.append("observation report cannot authorize the default switch")

    for key in ("approved_baseline_comparison", "m5_parity_comparison"):
        issues.extend(_comparison_issues(evidence.get(key), "gate03_evidence.%s" % key))
    try:
        private_report_hash = _sha(
            evidence.get("private_per_sample_report_sha256"),
            "gate03_evidence.private_per_sample_report_sha256",
        )
    except ReleaseGateServiceError as exc:
        issues.append(str(exc))
    else:
        if not test_only and private_report_hash != (
            candidate.get("private_artifacts") or {}
        ).get("private_report_sha256"):
            issues.append(
                "gate03_evidence.private_per_sample_report_sha256 does not "
                "match private_artifacts.private_report_sha256"
            )
    return issues


def register_run(
    db: Session,
    profile_id: str,
    candidate_record: dict,
) -> ReleaseGateRun:
    profile = resolve_profile(db, profile_id)
    candidate = deepcopy(candidate_record)
    try:
        validate_gate_candidate_record(candidate)
    except GateValidationError as exc:
        raise ReleaseGateServiceError(str(exc)) from exc
    version = _validate_profile_integrity(db, profile)
    issues = _candidate_relationship_issues(profile, version, candidate)
    if issues:
        raise ReleaseGateServiceError("; ".join(issues))
    if db.scalar(
        select(ReleaseGateRun).where(
            (ReleaseGateRun.evaluation_id == candidate.get("evaluation_id"))
            | (
                ReleaseGateRun.candidate_sha256
                == candidate.get("candidate_sha256")
            )
        )
    ) is not None:
        raise ReleaseGateConflict("release gate candidate already registered")
    run = ReleaseGateRun(
        profile_id=profile.id,
        evaluation_id=_text(candidate.get("evaluation_id"), "evaluation_id"),
        candidate_sha256=candidate["candidate_sha256"],
        candidate_record=candidate,
        status="candidate_awaiting_approval",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def register_test_only_rehearsal(
    db: Session,
    profile_id: str,
    candidate_record: dict,
) -> ReleaseGateRun:
    """Persist a complete GATE-03 rehearsal as permanently ineligible.

    This intentionally has no approval transition.  It exists so operators can
    exercise and audit the exact production archive contract with synthetic
    data without ever turning that exercise into release authorization.
    """

    profile = resolve_profile(db, profile_id)
    candidate = deepcopy(candidate_record)
    try:
        validate_gate_candidate_record(candidate)
    except GateValidationError as exc:
        raise ReleaseGateServiceError(str(exc)) from exc
    version = _validate_profile_integrity(db, profile)
    issues = _gate03_candidate_issues(
        profile,
        version,
        candidate,
        test_only=True,
    )
    if issues:
        raise ReleaseGateServiceError("; ".join(_unique_strings(issues)))
    if db.scalar(
        select(ReleaseGateRun).where(
            (ReleaseGateRun.evaluation_id == candidate.get("evaluation_id"))
            | (ReleaseGateRun.candidate_sha256 == candidate.get("candidate_sha256"))
        )
    ) is not None:
        raise ReleaseGateConflict("release gate candidate already registered")

    final = deepcopy(candidate)
    final["final_record_sha256"] = evaluation_sha256(final)
    assert_public_record_safe(final)
    finalized_at = datetime.now(timezone.utc).replace(tzinfo=None)
    run = ReleaseGateRun(
        profile_id=profile.id,
        evaluation_id=_text(candidate.get("evaluation_id"), "evaluation_id"),
        candidate_sha256=candidate["candidate_sha256"],
        candidate_record=candidate,
        status="ineligible",
        final_record=final,
        final_record_sha256=final["final_record_sha256"],
        finalized_at=finalized_at,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def get_run(db: Session, run_id: str):
    return db.get(ReleaseGateRun, run_id)


def approve_run(
    db: Session,
    run_id: str,
    privacy_review: dict,
    actor_id: str,
) -> ReleaseGateRun:
    run = db.get(ReleaseGateRun, run_id)
    if run is None:
        raise ReleaseGateServiceError("release gate run not found")
    if run.status != "candidate_awaiting_approval" or run.approval is not None:
        raise ReleaseGateConflict("release gate run is already finalized")
    profile = run.profile
    _validate_profile_integrity(db, profile)
    accepted_baseline = {
        "metrics": deepcopy(
            (run.candidate_record.get("evaluation") or {}).get("metrics") or {}
        ),
        "acceptance_thresholds": deepcopy(profile.acceptance_thresholds),
        "regression_tolerances": deepcopy(profile.regression_tolerances),
    }
    approved_at = datetime.now(timezone.utc)
    approval_record = {
        "schema": APPROVAL_SCHEMA,
        "candidate_sha256": run.candidate_sha256,
        "approved_by": actor_id,
        "approved_at": approved_at.isoformat(),
        "privacy_review": deepcopy(privacy_review),
        "accepted_baseline": deepcopy(accepted_baseline),
    }
    try:
        final = finalize_gate(run.candidate_record, approval_record)
    except GateValidationError as exc:
        raise ReleaseGateServiceError(str(exc)) from exc
    if profile.gate_key == "GATE-03":
        # Only a complete formal GATE-03 candidate that passed the exact-hash
        # maintainer approval may authorize the later PGS-36 cutover action.
        final["production_default_switch_authorized"] = bool(
            final.get("gate_passed")
        )
        final.pop("final_record_sha256", None)
        final["final_record_sha256"] = evaluation_sha256(final)
        assert_public_record_safe(final)

    approval = ReleaseGateApproval(
        run_id=run.id,
        candidate_sha256=run.candidate_sha256,
        privacy_review=deepcopy(privacy_review),
        accepted_baseline=accepted_baseline,
        approved_by=actor_id,
        approved_at=approved_at.replace(tzinfo=None),
    )
    run.status = final["status"]
    run.final_record = final
    run.final_record_sha256 = final["final_record_sha256"]
    run.finalized_at = approved_at.replace(tzinfo=None)
    db.add(approval)
    db.commit()
    db.refresh(run)
    return run
