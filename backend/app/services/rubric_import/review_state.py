"""规则审查的业务流程（解析重构方案 §7、阶段 6.5）。

审查结果存在当前编译记录的 ``raw_model_output.rule_review``，带指纹（按全部规则计算，
任何规则变化都会让审查过期）。审查不阻断发布；发布前在同一事务中把审查状态
（未审查 / 已过期 / 已审查及未处理问题数）写入 ``human_changes``，便于日后追查。
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db import models
from backend.app.services.rubric_import.parse_state import ParseStateError
from backend.app.services.rubric_import.parse_state import current_compilation
from backend.app.services.rubric_import.review import ReviewError
from backend.app.services.rubric_import.review import build_bundles
from backend.app.services.rubric_import.review import precheck
from backend.app.services.rubric_import.review import review_fingerprint
from backend.app.services.rubric_import.review import review_rules

REVIEWABLE_STATUSES = ("draft", "review")


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _criteria(session: Session, rubric_id: str) -> list[dict]:
    rows = session.scalars(
        select(models.RubricCriterion)
        .where(models.RubricCriterion.rubric_id == rubric_id)
        .order_by(models.RubricCriterion.display_order, models.RubricCriterion.code)
    ).all()
    return [
        {"code": c.code, "name": c.name, "max_score": float(c.max_score),
         "deduction_rules": list(c.deduction_rules or []),
         "deduction_rules_structured": deepcopy(list(c.deduction_rules_structured or []))}
        for c in rows
    ]


def _require_reviewable(session: Session, rubric_id: str):
    rubric = session.get(models.Rubric, rubric_id)
    if rubric is None:
        raise ParseStateError(404, "RUBRIC_NOT_FOUND", "评分模板不存在。")
    if rubric.status not in REVIEWABLE_STATUSES:
        raise ParseStateError(409, "RUBRIC_NOT_EDITABLE", "已发布或归档的评分模板不能再审查。")
    compilation = current_compilation(session, rubric_id)
    if compilation is None:
        raise ParseStateError(404, "COMPILATION_NOT_FOUND", "当前模板没有可审查的执行草稿。")
    return rubric, compilation


def run_rule_review(session: Session, rubric_id: str, scorer, *, scope: str, dry_run: bool, actor_id: str) -> dict:
    rubric, compilation = _require_reviewable(session, rubric_id)
    criteria = _criteria(session, rubric_id)
    prechecks = precheck(criteria, total_score=float(rubric.total_score))
    bundles = build_bundles(criteria, scope=scope)
    if not bundles:
        raise ParseStateError(422, "NOTHING_TO_REVIEW", "没有需要审查的扣分规则。")
    codes = [bundle["criterion"]["code"] for bundle in bundles]
    if dry_run:
        return {
            "criteria_codes": codes,
            "prechecks": prechecks,
            "estimate": {"calls": len(bundles) + (1 if len(bundles) >= 2 else 0),
                         "chars": len(json.dumps(bundles, ensure_ascii=False))},
        }
    try:
        result = review_rules(bundles, scorer)
    except ReviewError as exc:
        status = 503 if exc.code in {"AI_CONNECTION_MISSING", "AI_PROVIDER_ERROR"} else 422
        raise ParseStateError(status, exc.code, exc.message) from exc
    stored = {
        **result,
        "scope": scope,
        "criteria_codes": codes,
        "prechecks": prechecks,
        "fingerprint": review_fingerprint(build_bundles(criteria, scope="all")),
        "requested_by": actor_id,
        "created_at": _now(),
    }
    compilation.raw_model_output = {**deepcopy(compilation.raw_model_output or {}), "rule_review": stored}
    session.flush()
    return {**deepcopy(stored), "reviewed": True, "stale": False}


def _stale(session: Session, rubric_id: str, stored: dict) -> bool:
    return review_fingerprint(build_bundles(_criteria(session, rubric_id), scope="all")) != stored.get("fingerprint")


def read_rule_review(session: Session, rubric_id: str) -> dict:
    compilation = current_compilation(session, rubric_id)
    stored = (compilation.raw_model_output or {}).get("rule_review") if compilation else None
    if not stored:
        return {"reviewed": False}
    return {**deepcopy(stored), "reviewed": True, "stale": _stale(session, rubric_id, stored)}


def dismiss_finding(session: Session, rubric_id: str, finding_id: str, *, reason: str, actor_id: str) -> dict:
    _, compilation = _require_reviewable(session, rubric_id)
    output = deepcopy(compilation.raw_model_output or {})
    stored = output.get("rule_review") or {}
    finding = next((f for f in stored.get("findings") or [] if f.get("id") == finding_id), None)
    if finding is None:
        raise ParseStateError(404, "FINDING_NOT_FOUND", "审查问题不存在。")
    if not str(reason or "").strip():
        raise ParseStateError(422, "REASON_REQUIRED", "请填写豁免原因。")
    finding.update(status="dismissed", dismissed_by=actor_id, dismiss_reason=reason.strip(), dismissed_at=_now())
    compilation.raw_model_output = output
    compilation.human_changes = [
        *deepcopy(list(compilation.human_changes or [])),
        {"action": "rule_review_dismiss", "finding_id": finding_id, "actor_id": actor_id,
         "reason": reason.strip(), "occurred_at": _now()},
    ]
    session.flush()
    return deepcopy(finding)


def record_review_at_publish(session: Session, rubric_id: str, compilation_id: str, *, actor_id: str) -> None:
    """发布前（同一事务）记录审查状态；审查可跳过，但必须留痕。"""

    compilation = session.get(models.RubricCompilation, compilation_id)
    if compilation is None or compilation.rubric_id != rubric_id:
        return
    stored = (compilation.raw_model_output or {}).get("rule_review")
    if not stored:
        state = "not_reviewed"
    elif _stale(session, rubric_id, stored):
        state = "stale"
    else:
        state = "reviewed"
    open_findings = sum(1 for f in (stored or {}).get("findings") or [] if f.get("status") == "open")
    compilation.human_changes = [
        *deepcopy(list(compilation.human_changes or [])),
        {"action": "rule_review_at_publish", "review_state": state, "open_findings": open_findings,
         "actor_id": actor_id, "occurred_at": _now()},
    ]
    session.flush()
