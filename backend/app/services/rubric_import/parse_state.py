"""评分标准的解析台账状态：查询覆盖率、人工处理单元、发布门禁（解析重构方案 §8–§10）。

台账写在文件导入产生的 ``RubricCompilation.raw_parse_output`` 中；重编译时由
``pipeline.persist_prepared_import`` 继承到新的编译记录。人工处理结果直接写入台账
单元状态（``extracted_by=human``），同时在 ``human_changes`` 追加审计事件。
只有带台账的编译记录参与“疑似规则未处理”门禁；手工 JSON 创建、克隆等无台账的草稿不受影响。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.db import models
from backend.app.services.rubric_import.classification.llm_classifier import ClassificationError
from backend.app.services.rubric_import.classification.llm_classifier import classification_fingerprint
from backend.app.services.rubric_import.classification.llm_classifier import classify_units
from backend.app.services.rubric_import.classification.llm_classifier import select_units
from backend.app.services.rubric_import.classification.signals import profile_signal_terms
from backend.app.services.rubric_import.coverage import compute_coverage
from backend.app.services.rubric_import.sources.units import SourceLedger

LEDGER_KEYS = (
    "source_ledger", "coverage", "extraction", "triggers", "template_summary", "business_profile_key", "source_conflicts",
)
MODEL_OUTPUT_KEYS = ("unit_classifications", "structure_suggestions", "rule_review")
RESOLVE_ACTIONS = ("assign", "not_rule", "restore")


class ParseStateError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def has_ledger(compilation: models.RubricCompilation | None) -> bool:
    return bool(compilation is not None and (compilation.raw_parse_output or {}).get("source_ledger"))


def ledger_payload(raw: dict | None) -> dict:
    """重编译继承用：只取台账相关键。"""

    return {key: deepcopy(raw[key]) for key in LEDGER_KEYS if raw and key in raw}


def model_output_payload(raw: dict | None) -> dict:
    """重编译继承用：LLM 建议随草稿延续，靠指纹判断是否过期。"""

    return {key: deepcopy(raw[key]) for key in MODEL_OUTPUT_KEYS if raw and key in raw}


def criteria_payload(session: Session, rubric_id: str) -> list[dict]:
    criteria = session.scalars(
        select(models.RubricCriterion)
        .where(models.RubricCriterion.rubric_id == rubric_id)
        .order_by(models.RubricCriterion.display_order, models.RubricCriterion.code)
    ).all()
    return [{"code": c.code, "name": c.name, "description": c.description} for c in criteria]


def _classification_view(session: Session, rubric_id: str, compilation, raw: dict) -> dict | None:
    stored = (compilation.raw_model_output or {}).get("unit_classifications")
    if not stored:
        return None
    ledger = SourceLedger.from_mapping(raw["source_ledger"])
    units = stored.get("units") or []
    current_units = [
        {"unit_id": u["unit_id"], "text": ledger.unit(u["unit_id"]).text if ledger.has(u["unit_id"]) else None,
         "context": dict(ledger.unit(u["unit_id"]).context) if ledger.has(u["unit_id"]) else {}}
        for u in units
    ]
    stale = classification_fingerprint(current_units, criteria_payload(session, rubric_id)) != stored.get("fingerprint")
    return {**deepcopy(stored), "stale": stale}


def _structure_view(session: Session, rubric_id: str, compilation):
    from backend.app.services.rubric_import.structure_state import structure_suggestion_view

    return structure_suggestion_view(session, rubric_id, compilation)


def require_editable_ledger(session: Session, rubric_id: str):
    rubric = session.get(models.Rubric, rubric_id)
    if rubric is None:
        raise ParseStateError(404, "RUBRIC_NOT_FOUND", "评分模板不存在。")
    if rubric.status != "draft":
        raise ParseStateError(409, "RUBRIC_NOT_EDITABLE", "当前评分模板不是可编辑草稿。")
    compilation = current_compilation(session, rubric_id)
    if not has_ledger(compilation):
        raise ParseStateError(404, "LEDGER_NOT_FOUND", "当前草稿没有解析台账。")
    return compilation


def run_unit_classification(session: Session, rubric_id: str, scorer, *, unit_ids=None, actor_id: str) -> dict:
    compilation = require_editable_ledger(session, rubric_id)
    raw = compilation.raw_parse_output
    ledger = SourceLedger.from_mapping(raw["source_ledger"])
    units = select_units(ledger, _coverage(raw), unit_ids=unit_ids)
    if not units:
        raise ParseStateError(422, "NOTHING_TO_CLASSIFY", "没有需要分类的未认领内容。")
    try:
        result = classify_units(units, criteria_payload(session, rubric_id), scorer)
    except ClassificationError as exc:
        raise ParseStateError(503, exc.code, exc.message) from exc
    # Provider calls run without holding the merge lock. Serialize only the short
    # read/merge/write section, and refresh the identity map after waiting for a
    # concurrent batch to commit.
    compilation_id = compilation.id
    # A no-op UPDATE obtains a write lock on both PostgreSQL (row) and
    # SQLite (database), where SELECT FOR UPDATE would otherwise be ignored.
    session.execute(
        update(models.RubricCompilation)
        .where(models.RubricCompilation.id == compilation_id)
        .values(raw_model_output=models.RubricCompilation.raw_model_output)
        .execution_options(synchronize_session=False)
    )
    session.expire_all()
    current = require_editable_ledger(session, rubric_id)
    if current.id != compilation_id:
        raise ParseStateError(409, "CLASSIFICATION_INPUT_CHANGED", "评分标准已变化，请重新归类。")
    raw = compilation.raw_parse_output
    ledger = SourceLedger.from_mapping(raw["source_ledger"])
    fresh_units = select_units(ledger, _coverage(raw), unit_ids=[u["unit_id"] for u in units])
    if classification_fingerprint(fresh_units, criteria_payload(session, rubric_id)) != result["fingerprint"]:
        raise ParseStateError(409, "CLASSIFICATION_INPUT_CHANGED", "归类期间原文或评分项已变化，请重新归类。")
    previous = _classification_view(session, rubric_id, compilation, raw)
    if previous and not previous["stale"]:
        requested = {u["unit_id"] for u in units}
        eligible = {u["unit_id"] for u in select_units(ledger, _coverage(raw))}
        retained = [u for u in previous.get("units", [])
                    if u["unit_id"] not in requested and u["unit_id"] in eligible]
        retained_ids = {u["unit_id"] for u in retained}
        for key in ("results", "rejected"):
            result[key] = [item for item in previous.get(key, [])
                           if item.get("unit_id") in retained_ids] + result[key]
        for key in ("failed_unit_ids", "unclassified_unit_ids"):
            result[key] = [uid for uid in previous.get(key, []) if uid in retained_ids] + result[key]
        units = retained + units
        result["fingerprint"] = classification_fingerprint(units, criteria_payload(session, rubric_id))
    stored = {
        **result,
        "units": [{"unit_id": u["unit_id"], "text": u["text"], "context": u.get("context", {})} for u in units],
        "requested_by": actor_id,
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    }
    compilation.raw_model_output = {**deepcopy(compilation.raw_model_output or {}), "unit_classifications": stored}
    session.flush()
    return {**deepcopy(stored), "stale": False}


def current_compilation(session: Session, rubric_id: str) -> models.RubricCompilation | None:
    from backend.app.services.rubrics.draft_graph import read_execution_draft

    active = read_execution_draft(session=session, rubric_id=rubric_id)["active_compilation"]
    return session.get(models.RubricCompilation, active["id"]) if active else None


def _coverage(raw: dict) -> dict:
    ledger = SourceLedger.from_mapping(raw["source_ledger"])
    terms = profile_signal_terms(raw.get("business_profile_key") or "thesis")
    return compute_coverage(ledger, profile_terms=terms)


def conflicts_with_state(raw: dict) -> list[dict]:
    """双文件冲突：锚点单元被人工处理（指派或确认不是规则）即视为已裁决。"""

    ledger = SourceLedger.from_mapping(raw["source_ledger"])
    result = []
    for conflict in raw.get("source_conflicts") or []:
        anchor = conflict.get("anchor_unit_id")
        resolved = bool(anchor and ledger.has(anchor) and ledger.status(anchor).status != "unclaimed")
        result.append({**deepcopy(conflict), "resolved": resolved})
    return result


def unresolved_blocking_units(compilation: models.RubricCompilation | None) -> list[dict]:
    if not has_ledger(compilation):
        return []
    raw = compilation.raw_parse_output
    items = [item for item in _coverage(raw)["unclaimed"] if item["blocking"]]
    seen = {item["unit_id"] for item in items}
    for conflict in conflicts_with_state(raw):
        if not conflict["resolved"] and conflict.get("anchor_unit_id") not in seen:
            items.append({"unit_id": conflict.get("anchor_unit_id"), "kind": "conflict", "text": conflict["message"]})
    return items


def unclaimed_summary(coverage: dict | None) -> dict:
    items = (coverage or {}).get("unclaimed") or []
    return {
        "total": len(items),
        "suspected": sum(1 for item in items if item["suspected"]),
        "blocking": sum(1 for item in items if item["blocking"]),
    }


def read_parse_coverage(session: Session, rubric_id: str) -> dict:
    compilation = current_compilation(session, rubric_id)
    if not has_ledger(compilation):
        return {
            "rubric_id": rubric_id,
            "compilation_id": compilation.id if compilation else None,
            "has_ledger": False,
            "coverage": None,
            "triggers": [],
            "extraction": None,
            "conflicts": [],
            "unit_classifications": None,
            "structure_suggestions": None,
            "unclaimed_summary": unclaimed_summary(None),
            "artifacts": [],
        }
    raw = compilation.raw_parse_output
    coverage = _coverage(raw)
    artifacts = [
        {
            "id": a.id,
            "artifact_type": a.artifact_type,
            "file_name": a.file_name,
            "file_size_bytes": a.file_size_bytes,
            "file_hash": a.file_hash,
            "created_at": a.created_at.isoformat(),
        }
        for a in compilation.artifacts
    ]
    return {
        "rubric_id": rubric_id,
        "compilation_id": compilation.id,
        "has_ledger": True,
        "coverage": coverage,
        "triggers": deepcopy(raw.get("triggers") or []),
        "extraction": deepcopy(raw.get("extraction")),
        "conflicts": conflicts_with_state(raw),
        "unit_classifications": _classification_view(session, rubric_id, compilation, raw),
        "structure_suggestions": _structure_view(session, rubric_id, compilation),
        "unclaimed_summary": unclaimed_summary(coverage),
        "artifacts": artifacts,
    }


def resolve_unit(session: Session, rubric_id: str, unit_id: str, **kwargs) -> dict:
    result = resolve_units(session, rubric_id, [unit_id], **kwargs)
    return {"unit": result["units"][0], "coverage": result["coverage"]}


def resolve_units(
    session: Session,
    rubric_id: str,
    unit_ids: list[str],
    *,
    action: str,
    reason: str,
    actor_id: str,
    criterion_code: str | None = None,
) -> dict:
    """人工处理一个或多个原文单元；任一单元不存在则整体拒绝，不做部分处理。"""

    rubric = session.get(models.Rubric, rubric_id)
    if rubric is None:
        raise ParseStateError(404, "RUBRIC_NOT_FOUND", "评分模板不存在。")
    if rubric.status != "draft":
        raise ParseStateError(409, "RUBRIC_NOT_EDITABLE", "当前评分模板不是可编辑草稿。")
    if action not in RESOLVE_ACTIONS:
        raise ParseStateError(422, "UNIT_ACTION_INVALID", "不支持的处理方式。")
    if not str(reason or "").strip():
        raise ParseStateError(422, "UNIT_REASON_REQUIRED", "请填写处理原因。")
    unit_ids = list(dict.fromkeys(unit_ids))
    if not unit_ids:
        raise ParseStateError(422, "UNIT_IDS_REQUIRED", "请选择要处理的原文内容。")
    compilation = current_compilation(session, rubric_id)
    if not has_ledger(compilation):
        raise ParseStateError(404, "LEDGER_NOT_FOUND", "当前草稿没有解析台账。")
    raw = deepcopy(compilation.raw_parse_output)
    ledger = SourceLedger.from_mapping(raw["source_ledger"])
    missing = [unit_id for unit_id in unit_ids if not ledger.has(unit_id)]
    if missing:
        raise ParseStateError(404, "UNIT_NOT_FOUND", "原文单元不存在：" + "、".join(missing))
    if action == "assign":
        codes = set(
            session.scalars(
                select(models.RubricCriterion.code).where(models.RubricCriterion.rubric_id == rubric_id)
            ).all()
        )
        if not criterion_code or criterion_code not in codes:
            raise ParseStateError(422, "CRITERION_NOT_FOUND", "请选择已有的评分项。")
        for unit_id in unit_ids:
            ledger.claim(unit_id, f"{criterion_code}.manual", extracted_by="human")
    elif action == "restore":
        payload = ledger.to_mapping()
        for item in payload["units"]:
            if item["unit_id"] in unit_ids:
                # 自动抽取的规则仍保留，移出只撤销人工归类。
                refs = [ref for ref in item.get("claimed_by", []) if not ref.endswith(".manual")]
                item.update(status="consumed" if refs else "unclaimed", claimed_by=refs,
                            reason=None, extracted_by="human")
        ledger = SourceLedger.from_mapping(payload)
    else:
        # 人工确认“不是规则”：即使台账中此前为其他状态，也以人工结论为准。
        ledger = _override_status(ledger, set(unit_ids))
    raw["source_ledger"] = ledger.to_mapping()
    raw["coverage"] = _coverage(raw)
    compilation.raw_parse_output = raw
    occurred_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    compilation.human_changes = [
        *deepcopy(list(compilation.human_changes or [])),
        *[
            {
                "action": "unit_resolution",
                "unit_id": unit_id,
                "decision": action,
                "criterion_code": criterion_code if action == "assign" else None,
                "actor_id": actor_id,
                "reason": reason.strip(),
                "occurred_at": occurred_at,
            }
            for unit_id in unit_ids
        ],
    ]
    session.flush()
    return {
        "units": [{"unit_id": unit_id, **ledger.status(unit_id).to_mapping()} for unit_id in unit_ids],
        "coverage": raw["coverage"],
    }


def _override_status(ledger: SourceLedger, unit_ids: set[str]) -> SourceLedger:
    payload = ledger.to_mapping()
    for item in payload["units"]:
        if item["unit_id"] in unit_ids:
            item.update(status="context", claimed_by=[], reason="人工确认不是规则", extracted_by="human")
    return SourceLedger.from_mapping(payload)


def assigned_rule_sources(session: Session, rubric_id: str, criterion_code: str) -> list[dict]:
    """Read human-authorized source material without changing criteria or executable rules."""
    compilation = current_compilation(session, rubric_id)
    if not has_ledger(compilation):
        return []
    ledger = SourceLedger.from_mapping(compilation.raw_parse_output["source_ledger"])
    return [{"text": unit.text, "source_refs": [unit.unit_id], "reason": "assigned_source"}
            for unit in ledger
            if f"{criterion_code}.manual" in ledger.status(unit.unit_id).claimed_by]
