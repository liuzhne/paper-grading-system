"""Read the first-step source workspace from the selected rubric's provenance.

File identities come from its active compilation, never from an arbitrary newest
upload. Import sessions only enrich matching files with the human draft state.
This projection does not read file bytes or modify lifecycle state.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, defer, selectinload

from backend.app.db import models
from backend.app.services.rubric_import.parse_state import current_compilation


def _file_compilation(session: Session, compilation: models.RubricCompilation | None):
    """Older JSON successors may retain a ledger but not duplicate file artifacts."""
    visited = set()
    while compilation is not None and compilation.id not in visited:
        visited.add(compilation.id)
        if any(item.artifact_type in {"word", "excel"} for item in compilation.artifacts):
            return compilation
        predecessor_id = (compilation.validation_result or {}).get("supersedes_compilation_id")
        predecessor = session.get(models.RubricCompilation, predecessor_id) if predecessor_id else None
        if predecessor is None or predecessor.rubric_id != compilation.rubric_id:
            return None
        compilation = predecessor
    return None


def _matching_import_session(session: Session, rubric_id: str, artifacts: list):
    signatures = {(item.artifact_type, item.file_hash) for item in artifacts}
    if not signatures:
        return None
    candidates = session.scalars(
        select(models.RubricImportSession)
        .where(models.RubricImportSession.rubric_id == rubric_id,
               models.RubricImportSession.status == "confirmed")
        .options(defer(models.RubricImportSession.rules_file_bytes),
                 defer(models.RubricImportSession.template_file_bytes))
        .order_by(models.RubricImportSession.updated_at.desc(), models.RubricImportSession.id.desc())
    )
    for candidate in candidates:
        files = (candidate.prepared_graph or {}).get("artifacts") or []
        if signatures == {(item.get("artifact_type"), item.get("file_hash"))
                          for item in files if item.get("artifact_type") in {"word", "excel"}}:
            return candidate
    return None


def _source_ref(source: models.SourceRule, kind: str) -> dict:
    return {
        "kind": kind,
        "sheet_name": source.sheet_name,
        "row_number": source.row_number,
        "locator": source.cell_locator,
        "text": source.raw_text,
    }


def _previews(compilation, artifacts) -> dict:
    units = ((compilation.raw_parse_output or {}).get("source_ledger") or {}).get("units") or []
    previews = {kind: [
        {"unit_id": item.get("unit_id"), "kind": item.get("kind"),
         "locator": deepcopy(item.get("context") or {}), "text": item.get("text") or ""}
        for item in units if item.get("doc_id") == kind
    ] for kind in ("word", "excel")}
    for kind in ("word", "excel"):
        if previews[kind]:
            continue
        # Pre-ledger imports still have a persisted source graph, even if the
        # original upload session (and original binary) never existed.
        for artifact in artifacts:
            if artifact.artifact_type != kind:
                continue
            previews[kind].extend(
                {"unit_id": source.id, "kind": "row" if kind == "excel" else "paragraph",
                 "locator": {"sheet_name": source.sheet_name, "row_number": source.row_number,
                             "cell_locator": source.cell_locator}, "text": source.raw_text}
                for source in artifact.source_rules
            )
            previews[kind].extend(
                {"unit_id": item.id, "kind": item.kind,
                 "locator": deepcopy(item.source_locator or {}), "text": item.raw_text}
                for item in artifact.template_items
            )
    return previews


def read_source_workspace(*, session: Session, rubric: models.Rubric) -> dict:
    active = current_compilation(session, rubric.id)
    source = _file_compilation(session, active)
    artifacts = [item for item in source.artifacts if item.artifact_type in {"word", "excel"}] if source else []
    files = {"rules": None, "template": None}
    metadata = {"rules": None, "template": None}
    for artifact in artifacts:
        slot = "rules" if artifact.artifact_type == "excel" else "template"
        files[slot] = artifact.file_name
        metadata[slot] = {"size_bytes": artifact.file_size_bytes,
                          "uploaded_at": artifact.created_at.replace(tzinfo=timezone.utc)}

    imported = _matching_import_session(session, rubric.id, artifacts)
    draft_by_code = {item["code"]: item for item in (imported.draft_data or {}).get("criteria", [])
                     if not item.get("deleted")} if imported else {}
    refs_by_code: dict[str, list] = {}
    if source:
        kinds = {item.id: item.artifact_type for item in artifacts}
        rules = session.scalars(
            select(models.AtomicRule).join(models.RubricVersion)
            .where(models.RubricVersion.compilation_id == source.id)
            .options(selectinload(models.AtomicRule.source_rules))
            .order_by(models.AtomicRule.rule_code)
        )
        code_by_id = {item.id: item.code for item in rubric.criteria}
        for rule in rules:
            code = code_by_id.get(rule.criterion_id)
            if code is None:
                continue
            refs = refs_by_code.setdefault(code, [])
            for original in rule.source_rules:
                if original.source_artifact_id in kinds:
                    ref = _source_ref(original, kinds[original.source_artifact_id])
                    if ref not in refs:
                        refs.append(ref)
        # Very early imports can predate AtomicRules while retaining source rows.
        for artifact in artifacts:
            for original in artifact.source_rules:
                refs_by_code.setdefault(original.source_rule_code, []).append(
                    _source_ref(original, artifact.artifact_type)
                )

    criteria = []
    for criterion in rubric.criteria:
        draft = draft_by_code.get(criterion.code) or {}
        refs = deepcopy(draft.get("source_refs") or refs_by_code.get(criterion.code) or [])
        unique_refs = []
        for ref in refs:
            if ref not in unique_refs:
                unique_refs.append(ref)
        criteria.append({
            "code": criterion.code,
            "source_refs": unique_refs,
            "parse_status": draft.get("parse_status") or ("parsed" if unique_refs else "unknown"),
        })
    return {
        "rubric_id": rubric.id,
        "compilation_id": active.id if active else None,
        "files": files,
        "file_metadata": metadata,
        "criteria": criteria,
        "score_adjustments": deepcopy(imported.score_adjustments or []) if imported else [],
        "previews": _previews(active, artifacts) if active else {"word": [], "excel": []},
    }
