"""Fail-closed database and teacher-score preflight for formal QWK gates."""

import re
from math import isclose

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import Rubric
from backend.app.db.models import RubricCompilation
from backend.app.db.models import RubricVersion
from backend.app.eval.gating import GateValidationError
from backend.app.eval.labeled_dataset import load_scores_table


PGS8_CRITERION_CODES = ("T01", "T02", "T03", "T04", "T05", "T06")
_SAFE_DIAGNOSTIC_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


def _draft_blocker_identifiers(compilations) -> list[str]:
    labels = set()
    for compilation in compilations:
        if (
            compilation.status == "superseded"
            or compilation.published_at is not None
        ):
            continue
        for blocker in compilation.blockers or []:
            if not isinstance(blocker, dict):
                continue
            code = str(blocker.get("code") or "")
            criterion_code = str(blocker.get("criterion_code") or "")
            if _SAFE_DIAGNOSTIC_IDENTIFIER.fullmatch(code) is None:
                continue
            if criterion_code:
                if _SAFE_DIAGNOSTIC_IDENTIFIER.fullmatch(criterion_code) is None:
                    continue
                labels.add("%s(%s)" % (code, criterion_code))
            else:
                labels.add(code)
    return sorted(labels)


def validate_release_rubric_and_scores(
    db: Session,
    rubric_id: str,
    scores_path,
    *,
    expected_codes=PGS8_CRITERION_CODES,
) -> RubricVersion:
    """Return the sole published immutable version or reject the gate input.

    The preflight is intentionally stricter than ordinary experimental QWK:
    it requires the PGS-8 criterion contract, a consistently published
    ``RubricVersion``, complete per-item teacher truth, valid score ranges and
    total/item conservation before any paper is sent to a model.
    """

    rubric = db.get(Rubric, rubric_id)
    if rubric is None:
        raise GateValidationError("release rubric does not exist")
    compilations = db.scalars(
        select(RubricCompilation).where(RubricCompilation.rubric_id == rubric.id)
    ).all()
    if rubric.status != "published" or rubric.published_at is None:
        blockers = _draft_blocker_identifiers(compilations)
        detail = "; blockers=%s" % ",".join(blockers) if blockers else ""
        raise GateValidationError("release rubric must be published%s" % detail)

    criteria = list(rubric.criteria)
    rubric_codes = [criterion.code for criterion in criteria]
    required_codes = list(expected_codes)
    if rubric_codes != required_codes:
        raise GateValidationError(
            "release rubric criterion codes must be exactly %s; got %s"
            % (",".join(required_codes), ",".join(rubric_codes))
        )

    compilation_by_id = {item.id: item for item in compilations}
    candidates = []
    for version in db.scalars(
        select(RubricVersion).where(RubricVersion.rubric_id == rubric.id)
    ).all():
        compilation = compilation_by_id.get(version.compilation_id)
        if (
            compilation is not None
            and compilation.status == "validated"
            and compilation.published_at == rubric.published_at
            and compilation.final_version_hash == version.version_hash
            and len(version.version_hash or "") == 64
            and bool(version.hash_scheme)
            and bool(version.business_profile_key)
            and bool(version.workflow_profile)
        ):
            candidates.append(version)
    if len(candidates) != 1:
        raise GateValidationError(
            "release rubric requires exactly one consistently published RubricVersion"
        )

    max_by_code = {
        criterion.code: float(criterion.max_score) for criterion in criteria
    }
    rubric_total = float(rubric.total_score)
    if not isclose(sum(max_by_code.values()), rubric_total, abs_tol=0.005):
        raise GateValidationError(
            "release rubric total_score does not equal criterion maxima"
        )

    try:
        rows = load_scores_table(scores_path)
    except (OSError, ValueError) as exc:
        raise GateValidationError("teacher scores cannot be parsed: %s" % exc) from exc
    if not rows:
        raise GateValidationError("teacher scores contain no valid samples")

    required_set = set(required_codes)
    for row_number, row in enumerate(rows, start=2):
        item_codes = set(row["items"])
        if item_codes != required_set:
            missing = sorted(required_set - item_codes)
            extra = sorted(item_codes - required_set)
            raise GateValidationError(
                "teacher scores row %s criterion mismatch (missing=%s, extra=%s)"
                % (row_number, missing, extra)
            )
        for code, score in row["items"].items():
            if score < 0 or score > max_by_code[code]:
                raise GateValidationError(
                    "teacher scores row %s %s is outside 0..%g"
                    % (row_number, code, max_by_code[code])
                )
        item_total = sum(row["items"].values())
        if not isclose(item_total, row["total"], abs_tol=0.005):
            raise GateValidationError(
                "teacher scores row %s total does not equal item sum" % row_number
            )
        if row["total"] < 0 or row["total"] > rubric_total:
            raise GateValidationError(
                "teacher scores row %s total is outside 0..%g"
                % (row_number, rubric_total)
            )

    return candidates[0]
