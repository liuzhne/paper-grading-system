"""Fail-closed release-gate records for real holdout evaluation.

Private papers, teacher scores, and per-sample results stay in an external
artifact directory.  The repository-safe record contains only aggregate
metrics, content identities, approval metadata, and hashes of the private
artifacts.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from copy import deepcopy
from decimal import Decimal
from datetime import datetime
from datetime import timezone
from pathlib import Path

from backend.app.services.scoring.core.canonical import canonical_sha256


PRIVATE_MANIFEST_SCHEMA = "paper-grading/private-holdout-manifest@1"
GATE_RECORD_SCHEMA = "paper-grading/evaluation-gate@1"
APPROVAL_SCHEMA = "paper-grading/evaluation-gate-approval@1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_AGGREGATE_METRICS = (
    "qwk",
    "mae",
    "rmse",
    "exact_grade_agreement",
    "adjacent_grade_agreement",
    "review_rate",
    "blocked_rate",
)
_RUN_IDENTITY_FIELDS = (
    "rubric_version_id",
    "rubric_version_hash",
    "rubric_hash_scheme",
    "rubric_snapshot_hash",
    "policy_hash",
    "execution_plan_hash",
    "plan_schema_version",
    "checker_manifest_sha256",
    "business_profile_key",
    "workflow_profile",
    "model_provider",
    "model_name",
    "model_version",
    "engine_version",
)
_SENSITIVE_PUBLIC_KEYS = {
    "filename",
    "file_name",
    "student_id",
    "student_name",
    "advisor",
    "title",
    "human_total",
    "human_items",
    "system_total",
    "system_items",
    "per_sample",
    "truth",
}


class GateValidationError(ValueError):
    """The release-gate input is incomplete, inconsistent, or unsafe."""


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evaluation_sha256(value) -> str:
    """Canonical evaluation hash with exact decimal conversion for metrics."""

    return canonical_sha256(_decimalize(value))


def sample_id_for_artifact_sha256(artifact_sha256: str) -> str:
    _require_sha256(artifact_sha256, "artifact_sha256")
    payload = ("paper-grading/holdout-sample@1\0" + artifact_sha256).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_holdout_manifest_from_rows(
    *,
    papers_dir,
    rows,
    dataset_id: str,
    dataset_version: str,
):
    """Build a private manifest plus an in-memory filename→sample mapping.

    The manifest contains teacher scores because it is a private external
    artifact.  It deliberately contains no original filenames or paths.
    """

    dataset_id = _required_text(dataset_id, "dataset_id")
    dataset_version = _required_text(dataset_version, "dataset_version")
    base_dir = Path(papers_dir).resolve()
    if not base_dir.is_dir():
        raise GateValidationError("papers_dir must be an existing directory")

    paper_records = []
    truth_records = []
    sample_ids_by_filename = {}
    seen_filenames = set()
    seen_sample_ids = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise GateValidationError("score row %d must be an object" % index)
        filename = _required_text(row.get("filename"), "rows[%d].filename" % index)
        if filename in seen_filenames:
            raise GateValidationError("duplicate score-table filename: %s" % filename)
        seen_filenames.add(filename)
        source = (base_dir / filename).resolve()
        try:
            source.relative_to(base_dir)
        except ValueError as exc:
            raise GateValidationError("score-table filename escapes papers_dir") from exc
        if not source.is_file():
            raise GateValidationError("paper file is missing for score row %d" % index)

        artifact_sha256 = sha256_file(source)
        sample_id = sample_id_for_artifact_sha256(artifact_sha256)
        if sample_id in seen_sample_ids:
            raise GateValidationError(
                "duplicate paper content is not allowed in a release holdout"
            )
        seen_sample_ids.add(sample_id)
        sample_ids_by_filename[filename] = sample_id
        paper_records.append(
            {
                "sample_id": sample_id,
                "source_artifact_sha256": artifact_sha256,
                "byte_length": source.stat().st_size,
                "suffix": source.suffix.lower(),
            }
        )
        try:
            human_total = float(row["total"])
            human_items = {
                str(code): float(value)
                for code, value in sorted((row.get("items") or {}).items())
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise GateValidationError("score row %d has invalid truth values" % index) from exc
        truth_records.append(
            {
                "sample_id": sample_id,
                "human_total": human_total,
                "human_items": human_items,
            }
        )

    if not paper_records:
        raise GateValidationError("release holdout must contain at least one sample")
    paper_records.sort(key=lambda item: item["sample_id"])
    truth_records.sort(key=lambda item: item["sample_id"])
    paper_manifest_sha256 = evaluation_sha256(paper_records)
    truth_sha256 = evaluation_sha256(truth_records)
    public_identity = {
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "sample_count": len(paper_records),
        "sample_ids_sha256": evaluation_sha256(
            [item["sample_id"] for item in paper_records]
        ),
        "paper_manifest_sha256": paper_manifest_sha256,
        "truth_sha256": truth_sha256,
    }
    public_identity["dataset_manifest_sha256"] = evaluation_sha256(
        {
            "schema": "paper-grading/holdout-public-identity@1",
            **public_identity,
        }
    )
    manifest = {
        "schema": PRIVATE_MANIFEST_SCHEMA,
        "dataset": {
            "id": dataset_id,
            "version": dataset_version,
            "visibility": "external_private_not_in_repository",
        },
        "papers": paper_records,
        "truth": truth_records,
        "public_identity": public_identity,
    }
    return manifest, sample_ids_by_filename


def build_holdout_manifest(
    *,
    papers_dir,
    scores_path,
    dataset_id: str,
    dataset_version: str,
):
    # Local import keeps the identity helpers usable in minimal/offline tooling.
    from backend.app.eval.labeled_dataset import load_scores_table

    return build_holdout_manifest_from_rows(
        papers_dir=papers_dir,
        rows=load_scores_table(scores_path),
        dataset_id=dataset_id,
        dataset_version=dataset_version,
    )


def summarize_run_identities(report: dict) -> tuple[dict, list[str]]:
    per_sample = report.get("per_sample") or []
    issues = []
    summary = {}
    for field in _RUN_IDENTITY_FIELDS:
        values = {
            sample.get("run_identity", {}).get(field)
            for sample in per_sample
            if sample.get("run_identity", {}).get(field) not in (None, "")
        }
        if len(values) != 1:
            issues.append(
                "%s must have exactly one non-empty value across all samples" % field
            )
            summary[field] = None
        else:
            summary[field] = next(iter(values))

    document_records = []
    source_records = []
    for sample in per_sample:
        sample_id = sample.get("sample_id")
        identity = sample.get("run_identity") or {}
        document_hash = identity.get("document_snapshot_hash")
        source_hash = identity.get("source_artifact_hash")
        normalized_hash = identity.get("normalized_content_hash")
        if not sample_id or not document_hash or not source_hash or not normalized_hash:
            issues.append("every evaluated sample must retain document/source identities")
            continue
        for value, field in (
            (sample_id, "sample_id"),
            (document_hash, "document_snapshot_hash"),
            (source_hash, "source_artifact_hash"),
            (normalized_hash, "normalized_content_hash"),
        ):
            if not _is_sha256(value):
                issues.append("%s must be a lowercase SHA-256 digest" % field)
        document_records.append(
            {"sample_id": sample_id, "document_snapshot_hash": document_hash}
        )
        source_records.append(
            {
                "sample_id": sample_id,
                "source_artifact_hash": source_hash,
                "normalized_content_hash": normalized_hash,
            }
        )
    document_records.sort(key=lambda item: item["sample_id"])
    source_records.sort(key=lambda item: item["sample_id"])
    summary["document_snapshot_manifest_sha256"] = (
        evaluation_sha256(document_records) if document_records else None
    )
    summary["scored_source_manifest_sha256"] = (
        evaluation_sha256(source_records) if source_records else None
    )
    summary["evaluated_sample_ids_sha256"] = (
        evaluation_sha256(
            sorted(
                sample.get("sample_id")
                for sample in per_sample
                if sample.get("sample_id")
            )
        )
        if per_sample
        else None
    )
    return summary, _unique(issues)


def collect_repository_identity(repo_root) -> dict:
    root = Path(repo_root).resolve()
    revision = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    dirty_output = _git(root, "status", "--porcelain", "--untracked-files=normal")
    lockfile = root / "uv.lock"
    return {
        "revision": revision,
        "branch": branch,
        "dirty": bool(dirty_output.strip()),
        "lockfile": "uv.lock",
        "lockfile_sha256": sha256_file(lockfile) if lockfile.is_file() else None,
    }


def build_runtime_source_identity(
    *,
    repo_root,
    provider: str,
    prompt_version: str,
) -> dict:
    root = Path(repo_root).resolve()
    provider = _required_text(provider, "provider")
    prompt_version = _required_text(prompt_version, "prompt_version")
    adapter_by_provider = {
        "openai": "backend/app/services/llm/openai_adapter.py",
        "openai_compatible": "backend/app/services/llm/openai_compatible_adapter.py",
    }
    files = [
        "backend/app/services/scoring/engine.py",
        "backend/app/services/llm/base.py",
        "backend/app/services/cache/llm_cache.py",
    ]
    adapter = adapter_by_provider.get(provider)
    if adapter:
        files.append(adapter)
    source_manifest = []
    for relative in sorted(files):
        path = root / relative
        if not path.is_file():
            raise GateValidationError("runtime source file is missing: %s" % relative)
        source_manifest.append({"path": relative, "sha256": sha256_file(path)})
    return {
        "prompt_version": prompt_version,
        "prompt_source_manifest_sha256": evaluation_sha256(source_manifest),
        "runtime_source_manifest": source_manifest,
    }


def build_model_artifact_identity(
    *,
    provider: str,
    model_name: str,
    model_version: str,
    artifact_path=None,
    immutable_revision: str | None = None,
) -> dict:
    provider = _required_text(provider, "provider")
    model_name = _required_text(model_name, "model_name")
    model_version = _required_text(model_version, "model_version")
    if bool(artifact_path) == bool(immutable_revision):
        raise GateValidationError(
            "provide exactly one of model artifact path or immutable model revision"
        )
    if artifact_path:
        path = Path(artifact_path).resolve()
        if not path.is_file():
            raise GateValidationError("model artifact path must be an existing file")
        method = "artifact_sha256"
        immutable_identity = sha256_file(path)
        revision = None
    else:
        revision = _required_text(immutable_revision, "immutable_revision")
        method = "provider_immutable_revision"
        immutable_identity = evaluation_sha256(
            {
                "scheme": "provider-model-artifact-identity@1",
                "provider": provider,
                "model_name": model_name,
                "model_version": model_version,
                "immutable_revision": revision,
            }
        )
    return {
        "provider": provider,
        "model_name": model_name,
        "model_version": model_version,
        "identity_method": method,
        "immutable_revision": revision,
        "artifact_identity_sha256": immutable_identity,
    }


def ensure_external_artifact_dir(path, repo_root) -> Path:
    artifact_dir = Path(path).resolve()
    repository = Path(repo_root).resolve()
    try:
        artifact_dir.relative_to(repository)
    except ValueError:
        pass
    else:
        raise GateValidationError(
            "private release-gate artifacts must be outside the repository"
        )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def build_gate_candidate(
    *,
    evaluation_id: str,
    report: dict,
    dataset_identity: dict,
    run_identity: dict,
    run_identity_issues: list[str],
    anchors_identity: dict,
    model_identity: dict,
    prompt_identity: dict,
    repository_identity: dict,
    private_artifacts: dict,
    artifact_directory_external: bool,
    generated_at: str | None = None,
) -> dict:
    metrics = {key: report.get(key) for key in _AGGREGATE_METRICS}
    missing = list(run_identity_issues)
    required_hashes = {
        "dataset.dataset_manifest_sha256": dataset_identity.get(
            "dataset_manifest_sha256"
        ),
        "dataset.paper_manifest_sha256": dataset_identity.get(
            "paper_manifest_sha256"
        ),
        "dataset.sample_ids_sha256": dataset_identity.get(
            "sample_ids_sha256"
        ),
        "dataset.truth_sha256": dataset_identity.get("truth_sha256"),
        "documents.document_snapshot_manifest_sha256": run_identity.get(
            "document_snapshot_manifest_sha256"
        ),
        "documents.scored_source_manifest_sha256": run_identity.get(
            "scored_source_manifest_sha256"
        ),
        "rubric.version_hash": run_identity.get("rubric_version_hash"),
        "rubric.snapshot_sha256": run_identity.get("rubric_snapshot_hash"),
        "policy.sha256": run_identity.get("policy_hash"),
        "plan.sha256": run_identity.get("execution_plan_hash"),
        "checker.manifest_sha256": run_identity.get("checker_manifest_sha256"),
        "anchors.manifest_sha256": anchors_identity.get("manifest_sha256"),
        "model.artifact_identity_sha256": model_identity.get(
            "artifact_identity_sha256"
        ),
        "prompt.source_manifest_sha256": prompt_identity.get(
            "prompt_source_manifest_sha256"
        ),
        "environment.lockfile_sha256": repository_identity.get(
            "lockfile_sha256"
        ),
        "artifacts.private_manifest_sha256": private_artifacts.get(
            "private_manifest_sha256"
        ),
        "artifacts.private_report_sha256": private_artifacts.get(
            "private_report_sha256"
        ),
    }
    for label, value in required_hashes.items():
        if not _is_sha256(value):
            missing.append("%s is missing or malformed" % label)
    for label, value in (
        ("dataset.dataset_id", dataset_identity.get("dataset_id")),
        ("dataset.dataset_version", dataset_identity.get("dataset_version")),
        ("rubric.version_id", run_identity.get("rubric_version_id")),
        ("rubric.hash_scheme", run_identity.get("rubric_hash_scheme")),
        ("plan.schema_version", run_identity.get("plan_schema_version")),
        ("profile.key", run_identity.get("business_profile_key")),
        ("profile.workflow", run_identity.get("workflow_profile")),
        ("model.provider", model_identity.get("provider")),
        ("model.model_name", model_identity.get("model_name")),
        ("model.model_version", model_identity.get("model_version")),
        ("prompt.version", prompt_identity.get("prompt_version")),
        ("code.revision", repository_identity.get("revision")),
    ):
        if value in (None, ""):
            missing.append("%s is missing" % label)
    revision = repository_identity.get("revision")
    if revision and not _COMMIT.fullmatch(str(revision)):
        missing.append("code.revision must be a 40-character commit")
    if repository_identity.get("dirty") is not False:
        missing.append("code worktree must be clean")
    if model_identity.get("provider") == "mock":
        missing.append("real release gates cannot use the mock provider")
    for field in ("provider", "model_name", "model_version"):
        model_value = model_identity.get(field)
        run_value = run_identity.get("model_" + field if field == "provider" else field)
        if model_value != run_value:
            missing.append(
                "model.%s does not match the persisted scoring run" % field
            )
    if not artifact_directory_external:
        missing.append("private artifacts must be outside the repository")
    if dataset_identity.get("sample_ids_sha256") != run_identity.get(
        "evaluated_sample_ids_sha256"
    ):
        missing.append("evaluated sample identities do not match the frozen holdout")
    dataset_size = report.get("dataset_size")
    sample_count = dataset_identity.get("sample_count")
    if not (
        isinstance(dataset_size, int)
        and isinstance(sample_count, int)
        and report.get("n") == dataset_size == sample_count
    ):
        missing.append("every frozen holdout sample must enter aggregate metrics")
    if report.get("errors"):
        missing.append("evaluation report contains sample errors")
    if report.get("unmatched_predictions"):
        missing.append("evaluation report contains unmatched predictions")
    for key, value in metrics.items():
        if value is None:
            missing.append("aggregate metric %s is missing" % key)

    record = {
        "schema": GATE_RECORD_SCHEMA,
        "evaluation_id": _required_text(evaluation_id, "evaluation_id"),
        "status": "candidate_awaiting_approval",
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat(),
        "reproducible": not missing,
        "gating_eligible": not missing,
        "gate_passed": False,
        "missing_or_invalid": _unique(missing),
        "profile_key": run_identity.get("business_profile_key"),
        "evaluation": {
            "sample_count": report.get("n"),
            "dataset_size": dataset_size,
            "completed_runs": report.get("completed_runs"),
            "error_count": len(report.get("errors") or []),
            "metrics": metrics,
            "per_criterion": deepcopy(report.get("per_criterion") or {}),
            "grade_confusion": deepcopy(report.get("grade_confusion")),
            "grade_labels": deepcopy(report.get("grade_labels")),
        },
        "dataset": deepcopy(dataset_identity),
        "documents": {
            "document_snapshot_manifest_sha256": run_identity.get(
                "document_snapshot_manifest_sha256"
            ),
            "scored_source_manifest_sha256": run_identity.get(
                "scored_source_manifest_sha256"
            ),
        },
        "rubric": {
            "version_id": run_identity.get("rubric_version_id"),
            "version_hash": run_identity.get("rubric_version_hash"),
            "hash_scheme": run_identity.get("rubric_hash_scheme"),
            "snapshot_sha256": run_identity.get("rubric_snapshot_hash"),
        },
        "policy": {"sha256": run_identity.get("policy_hash")},
        "plan": {
            "schema_version": run_identity.get("plan_schema_version"),
            "sha256": run_identity.get("execution_plan_hash"),
        },
        "prompt": {
            "version": prompt_identity.get("prompt_version"),
            "source_manifest_sha256": prompt_identity.get(
                "prompt_source_manifest_sha256"
            ),
        },
        "anchors": deepcopy(anchors_identity),
        "checker": {
            "manifest_sha256": run_identity.get("checker_manifest_sha256")
        },
        "model": deepcopy(model_identity),
        "runtime": {
            "engine_version": run_identity.get("engine_version"),
            "workflow_profile": run_identity.get("workflow_profile"),
        },
        "code": {
            "revision": repository_identity.get("revision"),
            "branch": repository_identity.get("branch"),
            "dirty": repository_identity.get("dirty"),
        },
        "environment": {
            "lockfile": repository_identity.get("lockfile"),
            "lockfile_sha256": repository_identity.get("lockfile_sha256"),
        },
        "private_artifacts": deepcopy(private_artifacts),
        "approval": None,
        "manual_confirmations_pending": [
            "privacy_review",
            "anchor_holdout_exclusion",
            "baseline_and_regression_threshold_approval",
        ],
    }
    assert_public_record_safe(record)
    record["candidate_sha256"] = evaluation_sha256(record)
    return record


def finalize_gate(candidate: dict, approval: dict) -> dict:
    if candidate.get("schema") != GATE_RECORD_SCHEMA:
        raise GateValidationError("unsupported gate record schema")
    _validate_candidate_hash(candidate)
    if approval.get("schema") != APPROVAL_SCHEMA:
        raise GateValidationError("unsupported approval schema")
    if approval.get("candidate_sha256") != candidate.get("candidate_sha256"):
        raise GateValidationError("approval does not reference this exact candidate")
    approved_by = _required_text(approval.get("approved_by"), "approved_by")
    approved_at = _required_text(approval.get("approved_at"), "approved_at")
    privacy = approval.get("privacy_review") or {}
    privacy_requirements = (
        "dataset_deidentified",
        "repository_scan_passed",
        "anchors_exclude_holdout",
        "teacher_truth_external_only",
    )
    failed_privacy = [key for key in privacy_requirements if privacy.get(key) is not True]
    if failed_privacy:
        raise GateValidationError(
            "privacy review is incomplete: %s" % ", ".join(failed_privacy)
        )

    baseline = approval.get("accepted_baseline") or {}
    accepted_metrics = baseline.get("metrics") or {}
    actual_metrics = candidate.get("evaluation", {}).get("metrics") or {}
    for key in _AGGREGATE_METRICS:
        if accepted_metrics.get(key) != actual_metrics.get(key):
            raise GateValidationError(
                "accepted baseline metric %s does not match candidate" % key
            )
    thresholds = baseline.get("acceptance_thresholds") or {}
    tolerances = baseline.get("regression_tolerances") or {}
    threshold_issues = _threshold_issues(actual_metrics, thresholds)
    _validate_regression_tolerances(tolerances)

    result = deepcopy(candidate)
    result["approval"] = {
        "schema": APPROVAL_SCHEMA,
        "approved_by": approved_by,
        "approved_at": approved_at,
        "privacy_review": deepcopy(privacy),
        "accepted_baseline": deepcopy(baseline),
    }
    result["anchors"]["holdout_exclusion_proven"] = True
    result["manual_confirmations_pending"] = []
    if candidate.get("gating_eligible") and not threshold_issues:
        result["status"] = "passed"
        result["gate_passed"] = True
        result["missing_or_invalid"] = []
    else:
        result["status"] = (
            "failed_thresholds" if threshold_issues else "ineligible"
        )
        result["gate_passed"] = False
        result["missing_or_invalid"] = _unique(
            list(result.get("missing_or_invalid") or []) + threshold_issues
        )
    result["final_record_sha256"] = evaluation_sha256(
        {key: value for key, value in result.items() if key != "final_record_sha256"}
    )
    assert_public_record_safe(result)
    return result


def approved_regression_issues(report: dict, approved_record: dict) -> list[str]:
    if approved_record.get("gate_passed") is not True:
        return ["reference record is not an approved, passed release gate"]
    baseline = (
        approved_record.get("approval", {})
        .get("accepted_baseline", {})
    )
    metrics = baseline.get("metrics") or {}
    tolerances = baseline.get("regression_tolerances") or {}
    _validate_regression_tolerances(tolerances)
    issues = []
    comparisons = (
        ("qwk", "drop", "qwk_drop"),
        ("mae", "rise", "mae_rise"),
        ("rmse", "rise", "rmse_rise"),
        ("exact_grade_agreement", "drop", "exact_grade_agreement_drop"),
        ("adjacent_grade_agreement", "drop", "adjacent_grade_agreement_drop"),
        ("review_rate", "rise", "review_rate_rise"),
    )
    for metric, direction, tolerance_key in comparisons:
        current = report.get(metric)
        reference = metrics.get(metric)
        tolerance = tolerances.get(tolerance_key)
        if current is None or reference is None or tolerance is None:
            issues.append("regression identity is incomplete for %s" % metric)
            continue
        if direction == "drop" and current < reference - tolerance:
            issues.append(
                "%s regressed: %.12g < %.12g - %.12g"
                % (metric, current, reference, tolerance)
            )
        if direction == "rise" and current > reference + tolerance:
            issues.append(
                "%s regressed: %.12g > %.12g + %.12g"
                % (metric, current, reference, tolerance)
            )
    return issues


def approved_candidate_regression_issues(
    candidate: dict,
    approved_record: dict,
) -> list[str]:
    """Compare one new candidate with an approved release baseline."""

    _validate_candidate_hash(candidate)
    _validate_final_record_hash(approved_record)
    issues = []
    if candidate.get("gating_eligible") is not True:
        issues.append("current candidate is not gating eligible")
    if approved_record.get("gate_passed") is not True:
        issues.append("reference record is not an approved, passed release gate")
        return issues
    identity_paths = (
        ("profile_key",),
        ("dataset", "dataset_id"),
        ("dataset", "dataset_version"),
        ("dataset", "sample_count"),
        ("dataset", "dataset_manifest_sha256"),
        ("dataset", "paper_manifest_sha256"),
        ("dataset", "truth_sha256"),
        ("rubric", "version_hash"),
        ("rubric", "snapshot_sha256"),
        ("anchors", "manifest_sha256"),
    )
    for path in identity_paths:
        if _nested(candidate, path) != _nested(approved_record, path):
            issues.append(
                "%s changed; create and approve a new baseline"
                % ".".join(path)
            )
    issues.extend(
        approved_regression_issues(
            candidate.get("evaluation", {}).get("metrics") or {},
            approved_record,
        )
    )
    return _unique(issues)


def build_regression_record(
    *,
    candidate: dict,
    approved_record: dict,
) -> dict:
    issues = approved_candidate_regression_issues(candidate, approved_record)
    record = {
        "schema": "paper-grading/evaluation-gate-regression@1",
        "candidate_sha256": candidate.get("candidate_sha256"),
        "reference_evaluation_id": approved_record.get("evaluation_id"),
        "reference_record_sha256": approved_record.get("final_record_sha256"),
        "status": "passed" if not issues else "failed",
        "regression_passed": not issues,
        "issues": issues,
        "metrics": deepcopy(candidate.get("evaluation", {}).get("metrics") or {}),
    }
    assert_public_record_safe(record)
    record["regression_record_sha256"] = evaluation_sha256(record)
    return record


def assert_public_record_safe(value) -> None:
    def walk(item, path):
        if isinstance(item, dict):
            for key, nested in item.items():
                if str(key).lower() in _SENSITIVE_PUBLIC_KEYS:
                    raise GateValidationError(
                        "public gate record contains forbidden field: %s"
                        % ".".join(path + [str(key)])
                    )
                walk(nested, path + [str(key)])
        elif isinstance(item, list):
            for index, nested in enumerate(item):
                walk(nested, path + [str(index)])

    walk(value, [])


def write_json(path, value) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def _threshold_issues(metrics: dict, thresholds: dict) -> list[str]:
    supported = {
        "minimum_qwk": ("qwk", "minimum"),
        "maximum_mae": ("mae", "maximum"),
        "maximum_rmse": ("rmse", "maximum"),
        "minimum_exact_grade_agreement": (
            "exact_grade_agreement",
            "minimum",
        ),
        "minimum_adjacent_grade_agreement": (
            "adjacent_grade_agreement",
            "minimum",
        ),
        "maximum_review_rate": ("review_rate", "maximum"),
        "maximum_blocked_rate": ("blocked_rate", "maximum"),
    }
    unknown = set(thresholds) - set(supported)
    missing = set(supported) - set(thresholds)
    if unknown:
        raise GateValidationError(
            "unknown acceptance thresholds: %s" % sorted(unknown)
        )
    if missing:
        raise GateValidationError(
            "missing acceptance thresholds: %s" % sorted(missing)
        )
    issues = []
    for key, (metric, direction) in supported.items():
        limit = _non_negative_number(thresholds[key], key)
        current = metrics.get(metric)
        if current is None:
            issues.append("%s is missing" % metric)
        elif direction == "minimum" and current < limit:
            issues.append("%s %.12g is below approved minimum %.12g" % (metric, current, limit))
        elif direction == "maximum" and current > limit:
            issues.append("%s %.12g exceeds approved maximum %.12g" % (metric, current, limit))
    return issues


def _validate_regression_tolerances(tolerances: dict) -> None:
    required = {
        "qwk_drop",
        "mae_rise",
        "rmse_rise",
        "exact_grade_agreement_drop",
        "adjacent_grade_agreement_drop",
        "review_rate_rise",
    }
    unknown = set(tolerances) - required
    missing = required - set(tolerances)
    if unknown:
        raise GateValidationError(
            "unknown regression tolerances: %s" % sorted(unknown)
        )
    if missing:
        raise GateValidationError(
            "missing regression tolerances: %s" % sorted(missing)
        )
    for key in required:
        _non_negative_number(tolerances[key], key)


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GateValidationError("unable to collect git identity") from exc
    return result.stdout.strip()


def _required_text(value, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GateValidationError("%s must be a non-empty string" % path)
    return value.strip()


def _require_sha256(value, path: str) -> str:
    if not _is_sha256(value):
        raise GateValidationError("%s must be a lowercase SHA-256 digest" % path)
    return value


def _is_sha256(value) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def _non_negative_number(value, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise GateValidationError("%s must be a non-negative number" % path)
    return float(value)


def _unique(values):
    return list(dict.fromkeys(values))


def _nested(value, path):
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _validate_candidate_hash(candidate):
    supplied = candidate.get("candidate_sha256")
    expected = evaluation_sha256(
        {
            key: value
            for key, value in candidate.items()
            if key != "candidate_sha256"
        }
    )
    if supplied != expected:
        raise GateValidationError("candidate content does not match candidate_sha256")


def _validate_final_record_hash(record):
    supplied = record.get("final_record_sha256")
    expected = evaluation_sha256(
        {
            key: value
            for key, value in record.items()
            if key != "final_record_sha256"
        }
    )
    if supplied != expected:
        raise GateValidationError(
            "approved reference content does not match final_record_sha256"
        )


def _decimalize(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {str(key): _decimalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decimalize(item) for item in value]
    return value


__all__ = [
    "APPROVAL_SCHEMA",
    "GATE_RECORD_SCHEMA",
    "GateValidationError",
    "approved_candidate_regression_issues",
    "approved_regression_issues",
    "assert_public_record_safe",
    "build_gate_candidate",
    "build_holdout_manifest",
    "build_holdout_manifest_from_rows",
    "build_model_artifact_identity",
    "build_regression_record",
    "build_runtime_source_identity",
    "collect_repository_identity",
    "ensure_external_artifact_dir",
    "evaluation_sha256",
    "finalize_gate",
    "sample_id_for_artifact_sha256",
    "sha256_file",
    "summarize_run_identities",
    "write_json",
]
