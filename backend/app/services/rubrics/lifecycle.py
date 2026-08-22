"""P1-03 评分标准状态机、发布签核与 clone-for-edit。

服务函数只负责校验、修改当前 ``Session`` 并执行 ``flush``，事务提交或
回滚始终由调用者负责。模型层通过 ``p103_lifecycle_operation`` 识别这里
发起的受控状态变更；标记只在本次 flush 期间可见，不能被后续写操作复用。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from decimal import Decimal
from decimal import InvalidOperation
from typing import Iterator
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from backend.app.db import models
from backend.app.services.rubrics.executable_validator import (
    validate_publishable_rubric,
)
from backend.app.services.scoring.core.policy import build_corrected_thesis_policy


_OPERATION_KEY = "p103_lifecycle_operation"


class RubricLifecycleError(ValueError):
    """生命周期转换或克隆请求违反业务合同。"""


@contextmanager
def _lifecycle_operation(session: Session, operation: str) -> Iterator[None]:
    """仅在一次受控 flush 内公开生命周期操作标记。"""

    previous = session.info.get(_OPERATION_KEY)
    session.info[_OPERATION_KEY] = operation
    try:
        yield
    finally:
        if previous is None:
            session.info.pop(_OPERATION_KEY, None)
        else:
            session.info[_OPERATION_KEY] = previous


def _require_rubric(
    session: Session,
    rubric_id: str,
    *,
    for_update: bool = False,
) -> models.Rubric:
    statement = select(models.Rubric).where(models.Rubric.id == rubric_id)
    if for_update:
        statement = statement.with_for_update()
    rubric = session.scalar(statement)
    if rubric is None:
        raise RubricLifecycleError("评分标准不存在")
    return rubric


def _require_user(session: Session, user_id: str, *, label: str) -> models.User:
    if not user_id:
        raise RubricLifecycleError(f"{label}不存在")
    user = session.get(models.User, user_id)
    if user is None:
        raise RubricLifecycleError(f"{label}不存在")
    return user


def _require_state(rubric: models.Rubric, expected: str, operation: str) -> None:
    if rubric.status != expected:
        raise RubricLifecycleError(
            f"评分标准当前状态为 {rubric.status!r}，不能执行 {operation}；"
            f"要求状态为 {expected!r}"
        )


def _publication_time(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if now.tzinfo is not None:
        return now.astimezone(timezone.utc).replace(tzinfo=None)
    return now


def _canonical_value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _content_columns(instance, excluded):
    return {
        column.name: _canonical_value(getattr(instance, column.name))
        for column in instance.__table__.columns
        if column.name not in excluded
    }


def _stable_rows(rows):
    return sorted(
        rows,
        key=lambda row: json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _canonical_version_hash(
    session: Session,
    rubric: models.Rubric,
    compilation: models.RubricCompilation,
    version: models.RubricVersion,
) -> str:
    """按版本声明的 scheme 对完整、去 ID 来源图计算稳定 SHA-256。

    ``rubric-content-v1`` 的字节投影在 0011 已经发布，必须继续使用当时
    的显式字段集合；0013 新增的 profile 和 scheme 不能悄然改变历史 hash。
    ``rubric-content-v2`` 仅把业务 profile 纳入 version 子对象，scheme
    自身是解释投影的元数据，不属于 hash 内容。
    """

    hash_scheme = version.hash_scheme or "rubric-content-v1"
    if hash_scheme not in {"rubric-content-v1", "rubric-content-v2"}:
        raise RubricLifecycleError(
            f"不支持的 RubricVersion hash scheme: {hash_scheme!r}"
        )

    graph = _load_source_graph(session, rubric.id)
    artifacts = [
        item for item in graph.artifacts if item.compilation_id == compilation.id
    ]
    artifact_ids = {item.id for item in artifacts}
    source_rules = [
        item for item in graph.source_rules if item.source_artifact_id in artifact_ids
    ]
    templates = [
        item for item in graph.templates if item.source_artifact_id in artifact_ids
    ]
    rules = [
        item for item in graph.rules if item.rubric_version_id == version.id
    ]
    rule_ids = {item.id for item in rules}
    source_by_id = {
        item.id: _content_columns(
            item,
            {"id", "source_artifact_id", "created_at"},
        )
        for item in source_rules
    }
    template_by_id = {
        item.id: _content_columns(
            item,
            {"id", "source_artifact_id", "created_at"},
        )
        for item in templates
    }
    criterion_code = {item.id: item.code for item in graph.criteria}
    source_links_by_rule: dict[str, list[dict]] = {rule_id: [] for rule_id in rule_ids}
    for rule_id, source_rule_id in graph.source_links:
        if rule_id not in rule_ids:
            continue
        source_payload = source_by_id.get(source_rule_id)
        if source_payload is None:
            raise RubricLifecycleError("来源规则映射指向发布 compilation 之外")
        source_links_by_rule[rule_id].append(source_payload)

    levels_by_rule: dict[str, list[dict]] = {rule_id: [] for rule_id in rule_ids}
    for level in graph.levels:
        if level.atomic_rule_id in rule_ids:
            levels_by_rule[level.atomic_rule_id].append(
                _content_columns(level, {"id", "atomic_rule_id", "created_at"})
            )
    links_by_rule: dict[str, list[dict]] = {rule_id: [] for rule_id in rule_ids}
    for link in graph.template_links:
        if link.rule_id not in rule_ids:
            continue
        template_payload = template_by_id.get(link.template_item_id)
        if template_payload is None:
            raise RubricLifecycleError("模板映射指向发布 compilation 之外")
        links_by_rule[link.rule_id].append(
            {
                "link": _content_columns(
                    link,
                    {
                        "id",
                        "rule_id",
                        "template_item_id",
                        "review_status",
                        "reviewed_by",
                        "reviewed_at",
                        "created_at",
                    },
                ),
                "template": template_payload,
            }
        )

    rule_payloads = []
    for rule in rules:
        criterion = criterion_code.get(rule.criterion_id)
        if criterion is None:
            raise RubricLifecycleError("原子规则指向评分标准之外的 criterion")
        rule_payloads.append(
            {
                "criterion_code": criterion,
                "rule": _content_columns(
                    rule,
                    {
                        "id",
                        "rubric_version_id",
                        "criterion_id",
                        "status",
                        "reviewed_by",
                        "reviewed_at",
                        "created_at",
                    },
                ),
                "levels": _stable_rows(levels_by_rule[rule.id]),
                "source_rules": _stable_rows(source_links_by_rule[rule.id]),
                "template_links": _stable_rows(links_by_rule[rule.id]),
            }
        )

    artifact_payloads = []
    for artifact in artifacts:
        artifact_payloads.append(
            {
                "artifact": _content_columns(
                    artifact,
                    {"id", "compilation_id", "uploaded_by", "created_at"},
                ),
                "source_rules": _stable_rows(
                    source_by_id[item.id]
                    for item in source_rules
                    if item.source_artifact_id == artifact.id
                ),
                "templates": _stable_rows(
                    template_by_id[item.id]
                    for item in templates
                    if item.source_artifact_id == artifact.id
                ),
            }
        )

    payload = {
        "rubric": _content_columns(
            rubric,
            {
                "id",
                "owner_id",
                "organization_id",
                "visibility",
                "name",
                "version",
                "status",
                "created_by",
                "created_at",
                "published_at",
                "published_by",
                "archived_by",
            },
        ),
        "criteria": _stable_rows(
            _content_columns(item, {"id", "rubric_id", "created_at"})
            for item in graph.criteria
        ),
        "compilation": _content_columns(
            compilation,
            {
                "id",
                "rubric_id",
                "status",
                "created_by",
                "reviewed_by",
                "reviewed_at",
                "published_at",
                "final_version_hash",
                "human_changes",
                "created_at",
            },
        ),
        "artifacts": _stable_rows(artifact_payloads),
        "version": _content_columns(
            version,
            {
                "id",
                "rubric_id",
                "organization_id",
                "compilation_id",
                "version",
                "version_hash",
                "hash_scheme",
                "business_profile_key",
                "created_by",
                "created_at",
            },
        ),
        "rules": _stable_rows(rule_payloads),
    }
    if hash_scheme == "rubric-content-v2":
        graph_profile = version.business_profile_key
        if not isinstance(graph_profile, str) or not graph_profile.strip():
            raise RubricLifecycleError("rubric-content-v2 必须绑定非空 business profile")
        payload["version"]["business_profile_key"] = graph_profile
    serialized = json.dumps(
        _canonical_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


_EDITABLE_RULE_FIELDS = frozenset(
    {
        "name",
        "rule_text",
        "direction",
        "effect_type",
        "max_points",
        "repeat_policy",
        "cap_points",
        "judge_type",
        "checker_key",
        "checker_params",
        "evidence_policy",
        "positive_example",
        "negative_example",
        "boundary_example",
        "strictness",
        "applies_to",
        "mutex_group",
        "depends_on_rule_codes",
    }
)

_RULE_ENUM_FIELDS = {
    "direction": frozenset({"band", "deduct", "bonus", "none"}),
    "effect_type": frozenset(
        {"score", "review", "block_submission", "report_only"}
    ),
    "repeat_policy": frozenset({"once", "per_occurrence", "capped"}),
    "judge_type": frozenset({"deterministic", "semantic"}),
    "strictness": frozenset({"required", "preferred", "unknown"}),
}


def _json_mapping(value, *, field: str) -> dict:
    if not isinstance(value, Mapping):
        raise RubricLifecycleError(f"{field} 必须是 JSON object")
    try:
        # Round-trip both validates nested JSON values and detaches caller-owned
        # mutable objects before they reach SQLAlchemy's MutableDict.
        normalized = json.loads(
            json.dumps(dict(value), ensure_ascii=False, allow_nan=False)
        )
    except (TypeError, ValueError) as exc:
        raise RubricLifecycleError(f"{field} 包含非法 JSON 值") from exc
    if not isinstance(normalized, dict):
        raise RubricLifecycleError(f"{field} 必须是 JSON object")
    return normalized


def _normalize_rule_changes(rule_code: str, changes: dict) -> dict:
    normalized = {}
    for key, value in changes.items():
        if key in _RULE_ENUM_FIELDS:
            if value is None and key == "repeat_policy":
                normalized[key] = None
            elif not isinstance(value, str) or value not in _RULE_ENUM_FIELDS[key]:
                raise RubricLifecycleError(f"{key} 不是受支持的枚举值")
            else:
                normalized[key] = value
        elif key in {"max_points", "cap_points"}:
            if value is None:
                normalized[key] = None
                continue
            if isinstance(value, bool):
                raise RubricLifecycleError(f"{key} 必须是非负有限数值")
            try:
                number = Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                raise RubricLifecycleError(f"{key} 必须是非负有限数值") from exc
            if not number.is_finite() or number < 0:
                raise RubricLifecycleError(f"{key} 必须是非负有限数值")
            normalized[key] = number
        elif key in {"checker_params", "evidence_policy"}:
            normalized[key] = _json_mapping(value, field=key)
        elif key == "depends_on_rule_codes":
            if not isinstance(value, list):
                raise RubricLifecycleError("depends_on_rule_codes 必须是字符串列表")
            dependencies = []
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    raise RubricLifecycleError(
                        "depends_on_rule_codes 必须是非空字符串列表"
                    )
                dependencies.append(item.strip())
            if len(dependencies) != len(set(dependencies)):
                raise RubricLifecycleError("depends_on_rule_codes 不能重复")
            if rule_code in dependencies:
                raise RubricLifecycleError("原子规则不能依赖自身")
            normalized[key] = dependencies
        elif key in {"positive_example", "negative_example", "boundary_example"}:
            if value is not None and not isinstance(value, str):
                raise RubricLifecycleError(f"{key} 必须是字符串或 null")
            normalized[key] = value
        elif key in {"mutex_group", "checker_key"}:
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise RubricLifecycleError(f"{key} 必须是非空字符串或 null")
            normalized[key] = value.strip() if isinstance(value, str) else None
        elif key in {"name", "rule_text", "applies_to"}:
            if not isinstance(value, str) or not value.strip():
                raise RubricLifecycleError(f"{key} 必须是非空字符串")
            normalized[key] = value.strip()
        else:  # protected by the public allowlist; fail closed if it evolves.
            raise RubricLifecycleError(f"规则字段 {key} 缺少值校验器")
    return normalized


def _require_reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise RubricLifecycleError("操作原因不能为空")
    return reason.strip()


def _require_not_before(
    occurred_at: datetime,
    *lower_bounds: datetime | None,
    operation: str,
) -> None:
    for lower_bound in lower_bounds:
        if lower_bound is None:
            continue
        normalized = _publication_time(lower_bound)
        if occurred_at < normalized:
            raise RubricLifecycleError(
                f"{operation}时间不能早于被审核对象或其前置事件的创建时间"
            )


def _latest_audit_time(
    compilation: models.RubricCompilation,
    *,
    rule_code: str,
    action: str,
) -> datetime | None:
    values = []
    for event in compilation.human_changes or []:
        if not isinstance(event, dict):
            continue
        if event.get("rule_code") != rule_code or event.get("action") != action:
            continue
        raw = event.get("occurred_at")
        if not isinstance(raw, str):
            raise RubricLifecycleError("审核事件缺少合法 occurred_at")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RubricLifecycleError("审核事件 occurred_at 格式非法") from exc
        values.append(_publication_time(parsed))
    return max(values) if values else None


def _event(
    *,
    rule_code: str,
    field_path: str,
    action: str,
    before,
    after,
    actor_id: str,
    occurred_at: datetime,
    reason: str,
) -> dict:
    return {
        "change_id": str(uuid4()),
        "rule_code": rule_code,
        "field_path": field_path,
        "action": action,
        "before": deepcopy(_canonical_value(before)),
        "after": deepcopy(_canonical_value(after)),
        "actor_id": actor_id,
        "occurred_at": occurred_at.isoformat(timespec="microseconds"),
        "reason": reason,
    }


def _append_event(compilation: models.RubricCompilation, event: dict) -> None:
    # Reassignment is intentional: it makes SQLAlchemy's mutable JSON tracking
    # unambiguous and prevents caller-owned nested mappings mutating history.
    compilation.human_changes = [
        *deepcopy(list(compilation.human_changes or [])),
        deepcopy(event),
    ]


def _require_rule_context(
    session: Session,
    rubric_id: str,
    rule_code: str,
    *,
    for_update: bool = False,
):
    statement = (
        select(
            models.AtomicRule,
            models.RubricVersion,
            models.RubricCompilation,
            models.RubricCriterion,
        )
        .join(
            models.RubricVersion,
            models.AtomicRule.rubric_version_id == models.RubricVersion.id,
        )
        .join(
            models.RubricCompilation,
            models.RubricVersion.compilation_id == models.RubricCompilation.id,
        )
        .join(
            models.RubricCriterion,
            models.AtomicRule.criterion_id == models.RubricCriterion.id,
        )
        .where(
            models.RubricVersion.rubric_id == rubric_id,
            models.RubricCompilation.rubric_id == rubric_id,
            models.RubricCompilation.status != "superseded",
            models.RubricCriterion.rubric_id == rubric_id,
            models.AtomicRule.rule_code == rule_code,
        )
    )
    if for_update:
        statement = statement.with_for_update()
    matches = session.execute(statement).all()
    if len(matches) != 1:
        raise RubricLifecycleError("原子规则不存在或版本归属不唯一")
    return matches[0]


def _project_deduction_rules(
    session: Session,
    version_id: str,
    criterion: models.RubricCriterion,
) -> None:
    rules = session.scalars(
        select(models.AtomicRule)
        .where(
            models.AtomicRule.rubric_version_id == version_id,
            models.AtomicRule.criterion_id == criterion.id,
            models.AtomicRule.direction == "deduct",
        )
        .order_by(models.AtomicRule.rule_code)
    ).all()
    criterion.deduction_rules_structured = [
        {
            "rule_code": rule.rule_code,
            "rule_text": rule.rule_text,
            "points": _canonical_value(rule.max_points),
            "repeat_policy": rule.repeat_policy,
            "cap_points": _canonical_value(rule.cap_points),
            "checker_key": rule.checker_key,
            "checker_params": deepcopy(rule.checker_params or {}),
            "source": "atomic_rule",
        }
        for rule in rules
    ]


def submit_atomic_rule_for_review(
    session: Session,
    rubric_id: str,
    rule_code: str,
    actor_id: str,
    reason: str,
    *,
    now: datetime | None = None,
) -> models.AtomicRule:
    """Move one editable draft rule into human review and audit it."""

    with session.no_autoflush:
        rubric = _require_rubric(session, rubric_id, for_update=True)
        _require_state(rubric, "draft", "submit_atomic_rule_for_review")
        _require_user(session, actor_id, label="操作者")
        reason = _require_reason(reason)
        rule, _version, compilation, _criterion = _require_rule_context(
            session, rubric_id, rule_code, for_update=True
        )
        if rule.status != "draft":
            raise RubricLifecycleError("只有 draft 原子规则可以提交审核")
        occurred_at = _publication_time(now)
        _require_not_before(
            occurred_at,
            compilation.created_at,
            rule.created_at,
            operation="规则提交审核",
        )
        rule.status = "review"
        rule.reviewed_by = None
        rule.reviewed_at = None
        _append_event(
            compilation,
            _event(
                rule_code=rule.rule_code,
                field_path=f"/atomic_rules/{rule.rule_code}/status",
                action="submit_review",
                before="draft",
                after="review",
                actor_id=actor_id,
                occurred_at=occurred_at,
                reason=reason,
            ),
        )
        session.flush()
    return rule


def edit_atomic_rule(
    session: Session,
    rubric_id: str,
    rule_code: str,
    changes: dict,
    actor_id: str,
    reason: str,
    *,
    now: datetime | None = None,
) -> models.AtomicRule:
    """Edit an allowlisted set of content fields on a draft rule."""

    if not isinstance(changes, dict) or not changes:
        raise RubricLifecycleError("规则修改内容不能为空")
    unknown = set(changes) - _EDITABLE_RULE_FIELDS
    if unknown:
        raise RubricLifecycleError(
            "规则修改包含禁止字段: " + ", ".join(sorted(unknown))
        )
    normalized_changes = _normalize_rule_changes(rule_code, deepcopy(changes))
    with session.no_autoflush:
        rubric = _require_rubric(session, rubric_id, for_update=True)
        _require_state(rubric, "draft", "edit_atomic_rule")
        _require_user(session, actor_id, label="操作者")
        reason = _require_reason(reason)
        rule, version, compilation, criterion = _require_rule_context(
            session, rubric_id, rule_code, for_update=True
        )
        if rule.status != "draft":
            raise RubricLifecycleError("只有 draft 原子规则可以修改")
        occurred_at = _publication_time(now)
        _require_not_before(
            occurred_at,
            compilation.created_at,
            rule.created_at,
            operation="规则编辑",
        )
        before = {
            key: deepcopy(_canonical_value(getattr(rule, key)))
            for key in sorted(normalized_changes)
        }
        for key, value in normalized_changes.items():
            setattr(rule, key, value)
        after = {
            key: deepcopy(_canonical_value(getattr(rule, key)))
            for key in sorted(normalized_changes)
        }
        _project_deduction_rules(session, version.id, criterion)
        _append_event(
            compilation,
            _event(
                rule_code=rule.rule_code,
                field_path=f"/atomic_rules/{rule.rule_code}",
                action="edit",
                before=before,
                after=after,
                actor_id=actor_id,
                occurred_at=occurred_at,
                reason=reason,
            ),
        )
        session.flush()
    return rule


def _review_atomic_rule(
    session: Session,
    rubric_id: str,
    rule_code: str,
    reviewer_id: str,
    reason: str,
    *,
    decision: str,
    now: datetime | None,
) -> models.AtomicRule:
    with session.no_autoflush:
        rubric = _require_rubric(session, rubric_id, for_update=True)
        _require_state(rubric, "draft", f"{decision}_atomic_rule")
        _require_user(session, reviewer_id, label="审核人")
        reason = _require_reason(reason)
        rule, _version, compilation, _criterion = _require_rule_context(
            session, rubric_id, rule_code, for_update=True
        )
        if rule.status != "review":
            raise RubricLifecycleError("只有 review 原子规则可以完成审核")
        occurred_at = _publication_time(now)
        submitted_at = _latest_audit_time(
            compilation,
            rule_code=rule.rule_code,
            action="submit_review",
        )
        _require_not_before(
            occurred_at,
            compilation.created_at,
            rule.created_at,
            submitted_at,
            operation="规则审核",
        )
        rule.status = decision
        rule.reviewed_by = reviewer_id
        rule.reviewed_at = occurred_at
        _append_event(
            compilation,
            _event(
                rule_code=rule.rule_code,
                field_path=f"/atomic_rules/{rule.rule_code}/status",
                action="approve" if decision == "approved" else "reject",
                before="review",
                after=decision,
                actor_id=reviewer_id,
                occurred_at=occurred_at,
                reason=reason,
            ),
        )
        session.flush()
    return rule


def approve_atomic_rule(
    session: Session,
    rubric_id: str,
    rule_code: str,
    reviewer_id: str,
    reason: str,
    *,
    now: datetime | None = None,
) -> models.AtomicRule:
    return _review_atomic_rule(
        session,
        rubric_id,
        rule_code,
        reviewer_id,
        reason,
        decision="approved",
        now=now,
    )


def reject_atomic_rule(
    session: Session,
    rubric_id: str,
    rule_code: str,
    reviewer_id: str,
    reason: str,
    *,
    now: datetime | None = None,
) -> models.AtomicRule:
    return _review_atomic_rule(
        session,
        rubric_id,
        rule_code,
        reviewer_id,
        reason,
        decision="rejected",
        now=now,
    )


def reopen_atomic_rule(
    session: Session,
    rubric_id: str,
    rule_code: str,
    actor_id: str,
    reason: str,
    *,
    now: datetime | None = None,
) -> models.AtomicRule:
    """Reopen a rejected rule as an editable draft without erasing review history."""

    with session.no_autoflush:
        rubric = _require_rubric(session, rubric_id, for_update=True)
        _require_state(rubric, "draft", "reopen_atomic_rule")
        _require_user(session, actor_id, label="操作者")
        reason = _require_reason(reason)
        rule, _version, compilation, _criterion = _require_rule_context(
            session, rubric_id, rule_code, for_update=True
        )
        if rule.status != "rejected":
            raise RubricLifecycleError("只有 rejected 原子规则可以重新打开")
        occurred_at = _publication_time(now)
        rejected_at = _latest_audit_time(
            compilation,
            rule_code=rule.rule_code,
            action="reject",
        )
        if rejected_at is None:
            raise RubricLifecycleError("rejected 原子规则缺少不可变驳回审计事件")
        _require_not_before(
            occurred_at,
            compilation.created_at,
            rule.created_at,
            rule.reviewed_at,
            rejected_at,
            operation="规则重新打开",
        )
        previous_review = {
            "status": rule.status,
            "reviewed_by": rule.reviewed_by,
            "reviewed_at": _canonical_value(rule.reviewed_at),
        }
        rule.status = "draft"
        rule.reviewed_by = None
        rule.reviewed_at = None
        _append_event(
            compilation,
            _event(
                rule_code=rule.rule_code,
                field_path=f"/atomic_rules/{rule.rule_code}/status",
                action="reopen",
                before=previous_review,
                after={
                    "status": "draft",
                    "reviewed_by": None,
                    "reviewed_at": None,
                },
                actor_id=actor_id,
                occurred_at=occurred_at,
                reason=reason,
            ),
        )
        session.flush()
    return rule


def review_template_link(
    session: Session,
    rubric_id: str,
    link_id: str,
    reviewer_id: str,
    decision: str,
    reason: str,
    *,
    now: datetime | None = None,
) -> models.RuleTemplateLink:
    if decision not in {"confirmed", "rejected"}:
        raise RubricLifecycleError("模板映射审核结论必须为 confirmed 或 rejected")
    with session.no_autoflush:
        rubric = _require_rubric(session, rubric_id, for_update=True)
        _require_state(rubric, "draft", "review_template_link")
        _require_user(session, reviewer_id, label="审核人")
        reason = _require_reason(reason)
        statement = (
            select(
                models.RuleTemplateLink,
                models.AtomicRule,
                models.RubricCompilation,
            )
            .join(models.AtomicRule, models.RuleTemplateLink.rule_id == models.AtomicRule.id)
            .join(
                models.RubricVersion,
                models.AtomicRule.rubric_version_id == models.RubricVersion.id,
            )
            .join(
                models.RubricCompilation,
                models.RubricVersion.compilation_id == models.RubricCompilation.id,
            )
            .where(
                models.RuleTemplateLink.id == link_id,
                models.RubricVersion.rubric_id == rubric_id,
                models.RubricCompilation.rubric_id == rubric_id,
                models.RubricCompilation.status != "superseded",
            )
            .with_for_update()
        )
        row = session.execute(statement).one_or_none()
        if row is None:
            raise RubricLifecycleError("模板映射不存在或不属于评分标准")
        link, rule, compilation = row
        if link.review_status != "pending":
            raise RubricLifecycleError("只有 pending 模板映射可以审核")
        occurred_at = _publication_time(now)
        _require_not_before(
            occurred_at,
            compilation.created_at,
            link.created_at,
            operation="模板映射审核",
        )
        link.review_status = decision
        link.reviewed_by = reviewer_id
        link.reviewed_at = occurred_at
        _append_event(
            compilation,
            _event(
                rule_code=rule.rule_code,
                field_path=f"/rule_template_links/{link.id}/review_status",
                action=(
                    "confirm_template_link"
                    if decision == "confirmed"
                    else "reject_template_link"
                ),
                before="pending",
                after=decision,
                actor_id=reviewer_id,
                occurred_at=occurred_at,
                reason=reason,
            ),
        )
        session.flush()
    return link


def upgrade_legacy_draft(
    session: Session,
    rubric_id: str,
    actor_id: str,
    *,
    reason: str,
    now: datetime | None = None,
) -> models.Rubric:
    """Explicitly convert an unversioned draft into an auditable M4 graph.

    Legacy direct-score criteria are intentionally preserved as publication
    blockers.  The upgrade creates provenance and a review-only atomic rule for
    each criterion; a later human migration can replace direct scoring before
    publication without losing the original intent.
    """

    with session.no_autoflush:
        rubric = _require_rubric(session, rubric_id, for_update=True)
        _require_state(rubric, "draft", "upgrade_legacy_draft")
        _require_user(session, actor_id, label="升级操作者")
        reason = _require_reason(reason)
        existing = session.scalar(
            select(models.RubricCompilation.id)
            .where(models.RubricCompilation.rubric_id == rubric_id)
            .limit(1)
        )
        if existing is not None:
            raise RubricLifecycleError("评分标准已经具有来源图，无需重复升级")
        criteria = session.scalars(
            select(models.RubricCriterion)
            .where(models.RubricCriterion.rubric_id == rubric_id)
            .order_by(models.RubricCriterion.display_order, models.RubricCriterion.code)
        ).all()
        if not criteria:
            raise RubricLifecycleError("旧草稿没有可升级的评分项")

        occurred_at = _publication_time(now)
        _require_not_before(
            occurred_at,
            rubric.created_at,
            *(criterion.created_at for criterion in criteria),
            operation="旧草稿升级",
        )
        source_payload = {
            "rubric": {
                "name": rubric.name,
                "version": rubric.version,
                "total_score": _canonical_value(rubric.total_score),
            },
            "criteria": [
                {
                    "code": item.code,
                    "name": item.name,
                    "max_score": _canonical_value(item.max_score),
                    "criterion_type": item.criterion_type,
                    "scoring_mode": item.scoring_mode,
                }
                for item in criteria
            ],
        }
        source_bytes = json.dumps(
            source_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        version_hash = hashlib.sha256(b"legacy-upgrade-v1:" + source_bytes).hexdigest()
        blockers = [
            {
                "code": "llm_direct_not_publishable",
                "field_path": f"/criteria/{item.code}/scoring_mode",
                "message": "legacy llm_direct criterion requires explicit migration",
            }
            for item in criteria
            if item.scoring_mode == "llm_direct"
        ]
        compilation = models.RubricCompilation(
            id=models.new_id(),
            rubric_id=rubric.id,
            status="validated",
            parser_version="legacy-draft-parser@1",
            compiler_version="legacy-upgrade@1",
            sampling_params={},
            prompt_version="legacy-upgrade@1",
            raw_parse_output=deepcopy(source_payload),
            raw_model_output={},
            validation_result={"valid": True, "upgrade": "legacy-draft@1"},
            blockers=deepcopy(blockers),
            warnings=[],
            human_changes=[
                _event(
                    rule_code="__rubric__",
                    field_path="/rubric/provenance",
                    action="legacy_upgrade",
                    before="legacy_unversioned",
                    after="versioned_draft",
                    actor_id=actor_id,
                    occurred_at=occurred_at,
                    reason=reason,
                )
            ],
            created_by=actor_id,
            final_version_hash=version_hash,
        )
        artifact = models.SourceArtifact(
            id=models.new_id(),
            compilation_id=compilation.id,
            artifact_type="legacy_draft",
            file_name=f"legacy-draft-{rubric.id}.json",
            file_hash=source_hash,
            file_size_bytes=len(source_bytes),
            uploaded_by=actor_id,
        )
        version = models.RubricVersion(
            id=models.new_id(),
            rubric_id=rubric.id,
            organization_id=rubric.organization_id,
            compilation_id=compilation.id,
            version=rubric.version,
            workflow_profile="manual_json",
            global_policy=build_corrected_thesis_policy(
                rubric.total_score, "points"
            ).to_mapping(),
            version_hash=version_hash,
            business_profile_key="thesis",
            hash_scheme="rubric-content-v2",
            created_by=actor_id,
        )

        # Explicit flush layers are required by the composite
        # compilation/version foreign keys.
        session.add(compilation)
        session.flush()
        session.add_all([artifact, version])
        session.flush()
        for index, criterion in enumerate(criteria, start=1):
            source_rule = models.SourceRule(
                id=models.new_id(),
                source_artifact_id=artifact.id,
                source_rule_code=f"LEGACY-{index:03d}",
                sheet_name="legacy_draft",
                row_number=index,
                cell_locator=f"criteria[{index - 1}]",
                raw_text=criterion.description or criterion.name,
            )
            session.add(source_rule)
            session.flush()
            rule = models.AtomicRule(
                id=models.new_id(),
                rubric_version_id=version.id,
                criterion_id=criterion.id,
                rule_code=f"{criterion.code}-LEGACY-REVIEW",
                name=f"{criterion.name}（旧草稿复核）",
                rule_text=criterion.description or criterion.name,
                direction="none",
                effect_type="review",
                max_points=None,
                repeat_policy=None,
                cap_points=None,
                judge_type="semantic",
                checker_key=None,
                checker_params={},
                evidence_policy={
                    "mode": "review_only",
                    "requirement": "required",
                    "minimum_coverage": 1,
                },
                strictness="unknown",
                applies_to=criterion.applies_to or "global",
                depends_on_rule_codes=[],
                status="draft",
                creation_method="legacy_upgrade",
            )
            session.add(rule)
            session.flush()
            rule.source_rules.append(source_rule)
        session.flush()
    return rubric


def submit_for_review(session: Session, rubric_id: str) -> models.Rubric:
    """将 provenance 评分标准由 ``draft`` 提交为 ``review``。"""

    with session.no_autoflush:
        rubric = _require_rubric(session, rubric_id, for_update=True)
        _require_state(rubric, "draft", "submit_for_review")
        rubric.status = "review"
        with _lifecycle_operation(session, "submit_for_review"):
            session.flush()
    return rubric


def return_to_draft(session: Session, rubric_id: str) -> models.Rubric:
    """将审核中的评分标准退回 ``draft``，不伪造签核信息。"""

    with session.no_autoflush:
        rubric = _require_rubric(session, rubric_id, for_update=True)
        _require_state(rubric, "review", "return_to_draft")
        rubric.status = "draft"
        with _lifecycle_operation(session, "return_to_draft"):
            session.flush()
    return rubric


def publish_rubric(
    session: Session,
    rubric_id: str,
    compilation_id: str,
    reviewer_id: str,
    *,
    now: datetime | None = None,
) -> models.RubricVersion:
    """签核审核中的评分标准，并冻结指定 compilation 的唯一版本。"""

    with session.no_autoflush:
        rubric = _require_rubric(session, rubric_id, for_update=True)
        _require_state(rubric, "review", "publish")

        compilation = session.scalar(
            select(models.RubricCompilation)
            .where(models.RubricCompilation.id == compilation_id)
            .with_for_update()
        )
        if compilation is None:
            raise RubricLifecycleError("评分标准编译记录不存在")
        if compilation.rubric_id != rubric.id:
            raise RubricLifecycleError("编译记录不属于待发布的评分标准")
        if compilation.status != "validated":
            raise RubricLifecycleError("只有 validated 编译记录可以发布")

        _require_user(session, reviewer_id, label="审核人")
        versions = session.scalars(
            select(models.RubricVersion).where(
                models.RubricVersion.compilation_id == compilation.id
            ).with_for_update()
        ).all()
        if len(versions) != 1:
            raise RubricLifecycleError(
                "待发布编译记录必须且只能对应一个 RubricVersion"
            )
        version = versions[0]
        if version.rubric_id != rubric.id:
            raise RubricLifecycleError("RubricVersion 不属于待发布的评分标准")
        if not compilation.final_version_hash:
            raise RubricLifecycleError("编译记录缺少 final_version_hash")
        if version.version_hash != compilation.final_version_hash:
            raise RubricLifecycleError("RubricVersion 哈希与编译记录不一致")

        published_at = _publication_time(now)
        rules = session.scalars(
            select(models.AtomicRule).where(
                models.AtomicRule.rubric_version_id == version.id
            )
        ).all()
        rule_ids = [rule.id for rule in rules]
        links = (
            session.scalars(
                select(models.RuleTemplateLink).where(
                    models.RuleTemplateLink.rule_id.in_(rule_ids)
                )
            ).all()
            if rule_ids
            else []
        )
        _require_not_before(
            published_at,
            compilation.created_at,
            version.created_at,
            *(rule.reviewed_at for rule in rules),
            *(link.reviewed_at for link in links),
            operation="评分标准发布",
        )

        blockers = validate_publishable_rubric(
            session,
            rubric.id,
            compilation.id,
            # ``atomic-v1`` is the already-frozen P1/M3 contract.  Keep this a
            # positive allowlist so an unknown compiler identity can never turn
            # validation off.
            allow_pre_m4_provenance=(compilation.compiler_version == "atomic-v1"),
        )
        if blockers:
            codes = ", ".join(sorted({item["code"] for item in blockers}))
            raise RubricLifecycleError(f"评分标准尚不可发布：{codes}")

        content_hash = _canonical_version_hash(
            session,
            rubric,
            compilation,
            version,
        )
    rubric.status = "published"
    rubric.published_at = published_at
    rubric.published_by = reviewer_id
    compilation.reviewed_by = reviewer_id
    compilation.reviewed_at = published_at
    compilation.published_at = published_at
    compilation.final_version_hash = content_hash
    # 数据库由复合外键 ON UPDATE CASCADE 同步子行；这里同步 identity map，
    # 但不再为子行排一个会先于父行执行、从而短暂破坏外键的 UPDATE。
    set_committed_value(version, "version_hash", content_hash)

    with _lifecycle_operation(session, "publish"):
        session.flush()
        # SQLite 连接若由外部调用者创建且未开启 foreign_keys，数据库不会执行
        # ON UPDATE CASCADE；父行成功后补一条幂等 UPDATE，保证持久化结果一致。
        session.execute(
            update(models.RubricVersion)
            .where(models.RubricVersion.id == version.id)
            .values(version_hash=content_hash)
            .execution_options(synchronize_session=False)
        )
    return version


def clone_published_rubric(
    session: Session,
    rubric_id: str,
    new_version: str,
    actor_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
) -> models.Rubric:
    """把已发布的完整版本图深克隆为可编辑草稿。

    所有领域行使用新 ID；来源规则、模板映射和规则来源多对多关系均重建到
    新图内部。签核字段被清空，原始来源文件的上传者信息仍作为 provenance
    保留。源图在整个操作中只读。
    """

    source = _require_rubric(session, rubric_id)
    _require_state(source, "published", "clone")
    _require_user(session, actor_id, label="克隆操作者")

    if not isinstance(new_version, str) or not new_version.strip():
        raise RubricLifecycleError("新版本号不能为空")
    clone_name = source.name if name is None else name
    if not isinstance(clone_name, str) or not clone_name.strip():
        raise RubricLifecycleError("评分标准名称不能为空")
    clone_description = source.description if description is None else description

    collision_query = select(models.Rubric.id).where(
        models.Rubric.name == clone_name,
        models.Rubric.version == new_version,
        models.Rubric.visibility == source.visibility,
    )
    if source.visibility == "organization":
        collision_query = collision_query.where(
            models.Rubric.organization_id == source.organization_id
        )
    elif source.visibility == "private":
        collision_query = collision_query.where(models.Rubric.owner_id == actor_id)
    collision = session.scalar(collision_query)
    pending_collision = any(
        isinstance(item, models.Rubric)
        and item.name == clone_name
        and item.version == new_version
        for item in session.new
    )
    if collision is not None or pending_collision:
        raise RubricLifecycleError("评分标准名称和版本号已存在")

    graph = _load_source_graph(session, source.id)
    if not graph.compilations:
        raise RubricLifecycleError("已发布评分标准缺少编译记录")
    if not graph.versions:
        raise RubricLifecycleError("已发布评分标准缺少 RubricVersion")

    published_versions = [
        version
        for version in graph.versions
        if graph.compilation_by_id[version.compilation_id].published_at is not None
    ]
    if len(published_versions) != 1:
        raise RubricLifecycleError("已发布评分标准必须且只能有一个已签核版本")
    active_version = published_versions[0]
    active_compilation = graph.compilation_by_id[active_version.compilation_id]
    active_rules = [
        item for item in graph.rules if item.rubric_version_id == active_version.id
    ]
    active_rule_ids = {item.id for item in active_rules}
    active_criterion_ids = {item.criterion_id for item in active_rules}
    active_criteria = [
        item for item in graph.criteria if item.id in active_criterion_ids
    ]
    active_artifacts = [
        item
        for item in graph.artifacts
        if item.compilation_id == active_compilation.id
    ]
    active_artifact_ids = {item.id for item in active_artifacts}
    active_source_rules = [
        item
        for item in graph.source_rules
        if item.source_artifact_id in active_artifact_ids
    ]
    active_source_rule_ids = {item.id for item in active_source_rules}
    active_templates = [
        item
        for item in graph.templates
        if item.source_artifact_id in active_artifact_ids
    ]
    active_template_ids = {item.id for item in active_templates}
    active_levels = [
        item for item in graph.levels if item.atomic_rule_id in active_rule_ids
    ]
    active_links = [
        item
        for item in graph.template_links
        if item.rule_id in active_rule_ids
        and item.template_item_id in active_template_ids
    ]
    active_source_links = [
        (rule_id, source_rule_id)
        for rule_id, source_rule_id in graph.source_links
        if rule_id in active_rule_ids and source_rule_id in active_source_rule_ids
    ]

    cloned = models.Rubric(
        id=models.new_id(),
        owner_id=actor_id,
        organization_id=source.organization_id,
        visibility=source.visibility,
        name=clone_name,
        version=new_version,
        total_score=source.total_score,
        status="draft",
        description=clone_description,
        format_spec=deepcopy(source.format_spec or {}),
        created_by=actor_id,
        published_at=None,
    )

    criterion_map: dict[str, models.RubricCriterion] = {}
    for original in active_criteria:
        copied = models.RubricCriterion(
            id=models.new_id(),
            rubric_id=cloned.id,
            code=original.code,
            name=original.name,
            max_score=original.max_score,
            weight=original.weight,
            description=original.description,
            evidence_hints=deepcopy(original.evidence_hints or []),
            deduction_rules=deepcopy(original.deduction_rules or []),
            display_order=original.display_order,
            criterion_type=original.criterion_type,
            scoring_mode=original.scoring_mode,
            applies_to=original.applies_to,
            rubric_levels=deepcopy(original.rubric_levels or []),
            sub_checks=deepcopy(original.sub_checks or []),
            dimension=original.dimension,
            deduction_rules_structured=deepcopy(
                original.deduction_rules_structured or []
            ),
        )
        criterion_map[original.id] = copied

    compilation_map: dict[str, models.RubricCompilation] = {}
    for original in [active_compilation]:
        copied = models.RubricCompilation(
            id=models.new_id(),
            rubric_id=cloned.id,
            status=original.status,
            parser_version=original.parser_version,
            compiler_version=original.compiler_version,
            model_provider=original.model_provider,
            model_name=original.model_name,
            sampling_params=deepcopy(original.sampling_params or {}),
            prompt_version=original.prompt_version,
            raw_parse_output=deepcopy(original.raw_parse_output or {}),
            raw_model_output=deepcopy(original.raw_model_output or {}),
            validation_result=deepcopy(original.validation_result or {}),
            blockers=deepcopy(original.blockers or []),
            warnings=deepcopy(original.warnings or []),
            human_changes=[],
            created_by=actor_id,
            reviewed_by=None,
            reviewed_at=None,
            published_at=None,
            final_version_hash=original.final_version_hash,
        )
        compilation_map[original.id] = copied

    artifact_map: dict[str, models.SourceArtifact] = {}
    for original in active_artifacts:
        copied = models.SourceArtifact(
            id=models.new_id(),
            compilation_id=compilation_map[original.compilation_id].id,
            artifact_type=original.artifact_type,
            file_name=original.file_name,
            file_hash=original.file_hash,
            file_size_bytes=original.file_size_bytes,
            uploaded_by=original.uploaded_by,
        )
        artifact_map[original.id] = copied

    source_rule_map: dict[str, models.SourceRule] = {}
    for original in active_source_rules:
        copied = models.SourceRule(
            id=models.new_id(),
            source_artifact_id=artifact_map[original.source_artifact_id].id,
            source_rule_code=original.source_rule_code,
            sheet_name=original.sheet_name,
            row_number=original.row_number,
            cell_locator=original.cell_locator,
            raw_text=original.raw_text,
        )
        source_rule_map[original.id] = copied

    template_map: dict[str, models.TemplateItem] = {}
    for original in active_templates:
        copied = models.TemplateItem(
            id=models.new_id(),
            source_artifact_id=artifact_map[original.source_artifact_id].id,
            item_code=original.item_code,
            kind=original.kind,
            section_path=deepcopy(original.section_path or []),
            raw_text=original.raw_text,
            normalized_constraint=deepcopy(original.normalized_constraint),
            strictness=original.strictness,
            source_locator=deepcopy(original.source_locator or {}),
            source_hash=original.source_hash,
            parse_confidence=original.parse_confidence,
        )
        template_map[original.id] = copied

    version_map: dict[str, models.RubricVersion] = {}
    for original in [active_version]:
        copied = models.RubricVersion(
            id=models.new_id(),
            rubric_id=cloned.id,
            organization_id=cloned.organization_id,
            compilation_id=compilation_map[original.compilation_id].id,
            version=new_version,
            workflow_profile=original.workflow_profile,
            global_policy=deepcopy(original.global_policy or {}),
            version_hash=original.version_hash,
            business_profile_key=original.business_profile_key,
            hash_scheme=original.hash_scheme,
            created_by=actor_id,
        )
        version_map[original.id] = copied

    rule_map: dict[str, models.AtomicRule] = {}
    for original in active_rules:
        copied = models.AtomicRule(
            id=models.new_id(),
            rubric_version_id=version_map[original.rubric_version_id].id,
            criterion_id=criterion_map[original.criterion_id].id,
            rule_code=original.rule_code,
            name=original.name,
            rule_text=original.rule_text,
            direction=original.direction,
            effect_type=original.effect_type,
            max_points=original.max_points,
            repeat_policy=original.repeat_policy,
            cap_points=original.cap_points,
            judge_type=original.judge_type,
            checker_key=original.checker_key,
            checker_params=deepcopy(original.checker_params or {}),
            evidence_policy=deepcopy(original.evidence_policy or {}),
            positive_example=original.positive_example,
            negative_example=original.negative_example,
            boundary_example=original.boundary_example,
            strictness=original.strictness,
            applies_to=original.applies_to,
            mutex_group=original.mutex_group,
            depends_on_rule_codes=deepcopy(original.depends_on_rule_codes or []),
            status="draft",
            creation_method=original.creation_method,
            reviewed_by=None,
            reviewed_at=None,
        )
        rule_map[original.id] = copied

    copied_levels = [
        models.RuleLevel(
            id=models.new_id(),
            atomic_rule_id=rule_map[original.atomic_rule_id].id,
            level_code=original.level_code,
            points=original.points,
            descriptor=original.descriptor,
            positive_example=original.positive_example,
            negative_example=original.negative_example,
            display_order=original.display_order,
        )
        for original in active_levels
    ]
    copied_links = [
        models.RuleTemplateLink(
            id=models.new_id(),
            rule_id=rule_map[original.rule_id].id,
            template_item_id=template_map[original.template_item_id].id,
            relationship_type=original.relationship_type,
            match_method=original.match_method,
            match_confidence=original.match_confidence,
            rationale=original.rationale,
            review_status="pending",
            reviewed_by=None,
            reviewed_at=None,
        )
        for original in active_links
    ]
    copied_source_links = []
    for original_rule_id, original_source_rule_id in active_source_links:
        copied_rule = rule_map.get(original_rule_id)
        copied_source_rule = source_rule_map.get(original_source_rule_id)
        if copied_rule is None or copied_source_rule is None:
            raise RubricLifecycleError("来源规则映射指向评分标准图之外的对象")
        copied_source_links.append((copied_rule, copied_source_rule))

    try:
        with _lifecycle_operation(session, "clone"):
            # RubricVersion 使用指向 compilation 的复合外键，但两者之间没有
            # ORM relationship 可供 unit-of-work 推导插入顺序。因此按依赖层级
            # flush；所有层仍处于调用者的同一事务内，任一失败均可整体回滚。
            session.add_all(
                [cloned, *criterion_map.values(), *compilation_map.values()]
            )
            session.flush()
            session.add_all([*artifact_map.values(), *version_map.values()])
            session.flush()
            session.add_all([*source_rule_map.values(), *template_map.values()])
            session.flush()
            session.add_all(list(rule_map.values()))
            session.flush()

            for copied_rule, copied_source_rule in copied_source_links:
                copied_rule.source_rules.append(copied_source_rule)

            session.add_all([*copied_levels, *copied_links])
            session.flush()
    except IntegrityError as exc:
        raise RubricLifecycleError("深克隆评分标准失败：数据约束冲突") from exc
    return cloned


class _SourceGraph:
    def __init__(
        self,
        *,
        criteria,
        compilations,
        artifacts,
        source_rules,
        templates,
        versions,
        rules,
        levels,
        template_links,
        source_links,
    ):
        self.criteria = criteria
        self.compilations = compilations
        self.compilation_by_id = {item.id: item for item in compilations}
        self.artifacts = artifacts
        self.source_rules = source_rules
        self.templates = templates
        self.versions = versions
        self.rules = rules
        self.levels = levels
        self.template_links = template_links
        self.source_links = source_links


def _where_ids(column, values: list[str]):
    if not values:
        return column.in_(["__p103_no_rows__"])
    return column.in_(values)


def _load_source_graph(session: Session, rubric_id: str) -> _SourceGraph:
    """显式读取完整图，避免克隆期间依赖隐式级联或旧 ID。"""

    criteria = session.scalars(
        select(models.RubricCriterion)
        .where(models.RubricCriterion.rubric_id == rubric_id)
        .order_by(models.RubricCriterion.code)
    ).all()
    compilations = session.scalars(
        select(models.RubricCompilation)
        .where(models.RubricCompilation.rubric_id == rubric_id)
        .order_by(models.RubricCompilation.created_at, models.RubricCompilation.id)
    ).all()
    compilation_ids = [item.id for item in compilations]
    artifacts = session.scalars(
        select(models.SourceArtifact)
        .where(_where_ids(models.SourceArtifact.compilation_id, compilation_ids))
        .order_by(models.SourceArtifact.artifact_type, models.SourceArtifact.file_name)
    ).all()
    artifact_ids = [item.id for item in artifacts]
    source_rules = session.scalars(
        select(models.SourceRule)
        .where(_where_ids(models.SourceRule.source_artifact_id, artifact_ids))
        .order_by(models.SourceRule.source_rule_code, models.SourceRule.id)
    ).all()
    templates = session.scalars(
        select(models.TemplateItem)
        .where(_where_ids(models.TemplateItem.source_artifact_id, artifact_ids))
        .order_by(models.TemplateItem.item_code, models.TemplateItem.id)
    ).all()
    versions = session.scalars(
        select(models.RubricVersion)
        .where(_where_ids(models.RubricVersion.compilation_id, compilation_ids))
        .order_by(models.RubricVersion.version, models.RubricVersion.id)
    ).all()
    version_ids = [item.id for item in versions]
    rules = session.scalars(
        select(models.AtomicRule)
        .where(_where_ids(models.AtomicRule.rubric_version_id, version_ids))
        .order_by(models.AtomicRule.rule_code, models.AtomicRule.id)
    ).all()
    rule_ids = [item.id for item in rules]
    levels = session.scalars(
        select(models.RuleLevel)
        .where(_where_ids(models.RuleLevel.atomic_rule_id, rule_ids))
        .order_by(models.RuleLevel.atomic_rule_id, models.RuleLevel.display_order)
    ).all()
    template_links = session.scalars(
        select(models.RuleTemplateLink)
        .where(_where_ids(models.RuleTemplateLink.rule_id, rule_ids))
        .order_by(
            models.RuleTemplateLink.rule_id,
            models.RuleTemplateLink.template_item_id,
        )
    ).all()
    source_link_table = models.atomic_rule_source_rules
    source_links = session.execute(
        select(
            source_link_table.c.atomic_rule_id,
            source_link_table.c.source_rule_id,
        ).where(_where_ids(source_link_table.c.atomic_rule_id, rule_ids))
    ).all()
    return _SourceGraph(
        criteria=criteria,
        compilations=compilations,
        artifacts=artifacts,
        source_rules=source_rules,
        templates=templates,
        versions=versions,
        rules=rules,
        levels=levels,
        template_links=template_links,
        source_links=[(row.atomic_rule_id, row.source_rule_id) for row in source_links],
    )
