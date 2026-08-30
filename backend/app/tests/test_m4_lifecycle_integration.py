"""M4 lifecycle, API and CLI integration contracts.

This module specifies the production wiring without implementing it.  Missing
M4 symbols and endpoints are strict XFAIL gates.  After a capability appears,
service delegation, audit consistency, atomicity and external response behavior
are all asserted normally.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import inspect
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from backend.app.cli import db as cli_db
from backend.app.cli import main as cli_main
from backend.app.core.config import settings
from backend.app.db import models
from backend.app.db.session import get_db
from backend.app.db.sqlite import enable_sqlite_foreign_keys
from backend.app.main import app
from backend.app.schemas.batch import BatchCreate, BatchRead
from backend.app.services.rubrics import lifecycle
from backend.app.tests.conftest import make_rules_xlsx, make_template_docx
from backend.app.tests.test_rubric_version_lifecycle import _make_full_graph


VALIDATOR_MODULE = "backend.app.services.rubrics.executable_validator"
PIPELINE_MODULE = "backend.app.services.rubric_import.pipeline"
class M4CapabilityUnavailable(RuntimeError):
    """The sole exception allowed to turn an exact missing capability into XFAIL."""


class _DelegationObserved(RuntimeError):
    pass


def _probe_symbol(module_name: str, symbol: str):
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        target_missing = exc.name == module_name or (
            exc.name is not None and module_name.startswith(exc.name + ".")
        )
        if not target_missing:
            raise
        return None
    return vars(module).get(symbol)


_VALIDATE_PUBLISHABLE = _probe_symbol(
    VALIDATOR_MODULE, "validate_publishable_rubric"
)
_PREPARE_MANUAL_IMPORT = _probe_symbol(
    PIPELINE_MODULE, "prepare_manual_json_import"
)
_PREPARE_FILE_IMPORT = _probe_symbol(PIPELINE_MODULE, "prepare_file_import")
_PERSIST_PREPARED_IMPORT = _probe_symbol(
    PIPELINE_MODULE, "persist_prepared_import"
)
_LIFECYCLE_SYMBOLS = {
    name: vars(lifecycle).get(name)
    for name in (
        "submit_atomic_rule_for_review",
        "edit_atomic_rule",
        "approve_atomic_rule",
        "reject_atomic_rule",
        "review_template_link",
        "upgrade_legacy_draft",
    )
}


def _requires(label: str, condition: bool):
    return pytest.mark.xfail(
        condition=condition,
        reason=f"M4 capability is not implemented: {label}",
        raises=M4CapabilityUnavailable,
        strict=True,
    )


requires_rule_submit = _requires(
    "services.rubrics.lifecycle.submit_atomic_rule_for_review",
    not callable(_LIFECYCLE_SYMBOLS["submit_atomic_rule_for_review"]),
)
requires_rule_edit = _requires(
    "services.rubrics.lifecycle.edit_atomic_rule",
    not callable(_LIFECYCLE_SYMBOLS["edit_atomic_rule"]),
)
requires_rule_approve = _requires(
    "services.rubrics.lifecycle.approve_atomic_rule",
    not callable(_LIFECYCLE_SYMBOLS["approve_atomic_rule"]),
)
requires_rule_reject = _requires(
    "services.rubrics.lifecycle.reject_atomic_rule",
    not callable(_LIFECYCLE_SYMBOLS["reject_atomic_rule"]),
)
requires_link_review = _requires(
    "services.rubrics.lifecycle.review_template_link",
    not callable(_LIFECYCLE_SYMBOLS["review_template_link"]),
)
requires_legacy_upgrade = _requires(
    "services.rubrics.lifecycle.upgrade_legacy_draft",
    not callable(_LIFECYCLE_SYMBOLS["upgrade_legacy_draft"]),
)
requires_validator = _requires(
    f"{VALIDATOR_MODULE}.validate_publishable_rubric",
    not callable(_VALIDATE_PUBLISHABLE),
)


def _api_paths(method: str = "POST") -> set[str]:
    return {
        route.path
        for route in app.routes
        if method in getattr(route, "methods", set())
    }


def _route_endpoint(path: str, method: str = "POST"):
    matches = [
        route.endpoint
        for route in app.routes
        if route.path == path and method in getattr(route, "methods", set())
    ]
    return matches[0] if len(matches) == 1 else None


def _endpoint_delegates(
    path: str, service_symbol: str, method: str = "POST"
) -> bool:
    endpoint = _route_endpoint(path, method)
    original = vars(lifecycle).get(service_symbol)
    if endpoint is None or not callable(original):
        return False
    source = inspect.getsource(endpoint)
    if (
        service_symbol in source
        and ("services.rubrics" in source or "rubrics import lifecycle" in source)
    ):
        return True
    for name, value in endpoint.__globals__.items():
        if value is lifecycle and f"{name}.{service_symbol}(" in source:
            return True
        if value is original and value is not endpoint and f"{name}(" in source:
            return True
    return False


_API_PUBLISH_PATH = "/api/rubrics/{rubric_id}/publish"
_API_CLONE_PATH = "/api/rubrics/{rubric_id}/clone"
_API_PUBLISH_DELEGATES = _endpoint_delegates(_API_PUBLISH_PATH, "publish_rubric")
_API_CLONE_DELEGATES = _endpoint_delegates(
    _API_CLONE_PATH, "clone_published_rubric"
)
_LIFECYCLE_API_DELEGATIONS = {
    "/api/rubrics/{rubric_id}/submit-review": "submit_for_review",
    "/api/rubrics/{rubric_id}/return-to-draft": "return_to_draft",
    "/api/rubrics/{rubric_id}/rules/{rule_code}/submit-review": "submit_atomic_rule_for_review",
    "/api/rubrics/{rubric_id}/rules/{rule_code}/approve": "approve_atomic_rule",
    "/api/rubrics/{rubric_id}/rules/{rule_code}/reject": "reject_atomic_rule",
    "/api/rubrics/{rubric_id}/template-links/{link_id}/review": "review_template_link",
}
_RULE_EDIT_API_PATH = "/api/rubrics/{rubric_id}/rules/{rule_code}"


def _cli_capability_present(function_name: str, service_symbol: str) -> bool:
    callback = vars(cli_main).get(function_name)
    original = vars(lifecycle).get(service_symbol)
    return callable(callback) and callable(original)


def _lifecycle_publish_uses_validator() -> bool:
    if not callable(_VALIDATE_PUBLISHABLE):
        return False
    source = inspect.getsource(lifecycle.publish_rubric)
    return (
        "validate_publishable_rubric" in source
        and ("executable_validator" in source or "validate_publishable_rubric(" in source)
    )


def _api_delegations_missing(paths) -> bool:
    return any(
        path not in _api_paths()
        or not _endpoint_delegates(path, _LIFECYCLE_API_DELEGATIONS[path])
        for path in paths
    )


_RUBRIC_REVIEW_PATHS = (
    "/api/rubrics/{rubric_id}/submit-review",
    "/api/rubrics/{rubric_id}/return-to-draft",
)
_RULE_SUBMIT_APPROVE_PATHS = (
    "/api/rubrics/{rubric_id}/rules/{rule_code}/submit-review",
    "/api/rubrics/{rubric_id}/rules/{rule_code}/approve",
)
_LINK_REVIEW_PATHS = (
    "/api/rubrics/{rubric_id}/template-links/{link_id}/review",
)
requires_rubric_review_api = _requires(
    "API rubric submit-review/return-to-draft lifecycle facade",
    _api_delegations_missing(_RUBRIC_REVIEW_PATHS),
)
requires_rule_submit_approve_api = _requires(
    "API AtomicRule submit-review/approve lifecycle facade",
    _api_delegations_missing(_RULE_SUBMIT_APPROVE_PATHS),
)
requires_link_review_api = _requires(
    "API RuleTemplateLink review lifecycle facade",
    _api_delegations_missing(_LINK_REVIEW_PATHS),
)
_API_DELEGATION_MARKS = {
    path: _requires(
        f"API {path} delegation to lifecycle.{symbol}",
        _api_delegations_missing((path,)),
    )
    for path, symbol in _LIFECYCLE_API_DELEGATIONS.items()
}
requires_rule_edit_api = _requires(
    "PATCH AtomicRule facade delegation to lifecycle.edit_atomic_rule",
    not callable(_LIFECYCLE_SYMBOLS["edit_atomic_rule"])
    or not _endpoint_delegates(
        _RULE_EDIT_API_PATH, "edit_atomic_rule", method="PATCH"
    ),
)
requires_api_publish_lifecycle = _requires(
    (
        f"{VALIDATOR_MODULE}.validate_publishable_rubric and API /publish "
        "delegation to lifecycle.publish_rubric"
    ),
    not callable(_VALIDATE_PUBLISHABLE) or not _API_PUBLISH_DELEGATES,
)
requires_lifecycle_validator_wiring = _requires(
    "lifecycle.publish_rubric delegation to executable validator",
    not _lifecycle_publish_uses_validator(),
)
requires_api_clone_lifecycle = _requires(
    "API /clone delegation to lifecycle.clone_published_rubric",
    not _API_CLONE_DELEGATES,
)
requires_manual_create_pipeline = _requires(
    (
        f"{PIPELINE_MODULE}.prepare_manual_json_import and "
        f"{PIPELINE_MODULE}.persist_prepared_import"
    ),
    not callable(_PREPARE_MANUAL_IMPORT) or not callable(_PERSIST_PREPARED_IMPORT),
)
requires_file_import_pipeline = _requires(
    (
        f"{PIPELINE_MODULE}.prepare_file_import and "
        f"{PIPELINE_MODULE}.persist_prepared_import"
    ),
    not callable(_PREPARE_FILE_IMPORT) or not callable(_PERSIST_PREPARED_IMPORT),
)
_MANUAL_SIGNOFF_API_PATHS = (
    "/api/rubrics/{rubric_id}/rules/{rule_code}/submit-review",
    "/api/rubrics/{rubric_id}/rules/{rule_code}/approve",
    "/api/rubrics/{rubric_id}/submit-review",
)
requires_manual_signoff_chain = _requires(
    "manual_json pipeline plus shared AtomicRule/Rubric signoff API",
    not callable(_PREPARE_MANUAL_IMPORT)
    or not callable(_PERSIST_PREPARED_IMPORT)
    or _api_delegations_missing(_MANUAL_SIGNOFF_API_PATHS),
)
requires_legacy_signoff_chain = _requires(
    "legacy draft upgrade plus shared AtomicRule/Rubric signoff lifecycle",
    not callable(_LIFECYCLE_SYMBOLS["upgrade_legacy_draft"])
    or not callable(_LIFECYCLE_SYMBOLS["submit_atomic_rule_for_review"])
    or not callable(_LIFECYCLE_SYMBOLS["approve_atomic_rule"]),
)
requires_batch_version_pin = _requires(
    "BatchCreate/BatchRead.rubric_version_id",
    "rubric_version_id" not in BatchCreate.model_fields
    or "rubric_version_id" not in BatchRead.model_fields,
)
requires_cli_publish_lifecycle = _requires(
    "CLI publish delegation to lifecycle.publish_rubric and executable validator",
    not callable(_VALIDATE_PUBLISHABLE)
    or not _cli_capability_present("publish", "publish_rubric"),
)
requires_cli_rule_submit = _requires(
    "CLI rule-submit delegation to lifecycle.submit_atomic_rule_for_review",
    not _cli_capability_present("rule_submit", "submit_atomic_rule_for_review"),
)
requires_cli_rule_edit = _requires(
    "CLI rule-edit delegation to lifecycle.edit_atomic_rule",
    not _cli_capability_present("rule_edit", "edit_atomic_rule"),
)
requires_cli_rule_approve = _requires(
    "CLI rule-approve delegation to lifecycle.approve_atomic_rule",
    not _cli_capability_present("rule_approve", "approve_atomic_rule"),
)
requires_cli_rule_reject = _requires(
    "CLI rule-reject delegation to lifecycle.reject_atomic_rule",
    not _cli_capability_present("rule_reject", "reject_atomic_rule"),
)
requires_cli_link_review = _requires(
    "CLI template-link-review delegation to lifecycle.review_template_link",
    not _cli_capability_present("template_link_review", "review_template_link"),
)


def _require_lifecycle(name: str):
    value = _LIFECYCLE_SYMBOLS[name]
    if not callable(value):
        raise M4CapabilityUnavailable(
            f"services.rubrics.lifecycle.{name} is not implemented"
        )
    return value


def _require_validator():
    if not callable(_VALIDATE_PUBLISHABLE):
        raise M4CapabilityUnavailable(
            f"{VALIDATOR_MODULE}.validate_publishable_rubric is not implemented"
        )


def _require_api_delegations(paths):
    missing = [
        f"{path}->{_LIFECYCLE_API_DELEGATIONS[path]}"
        for path in paths
        if path not in _api_paths()
        or not _endpoint_delegates(path, _LIFECYCLE_API_DELEGATIONS[path])
    ]
    if missing:
        raise M4CapabilityUnavailable(
            "M4 lifecycle API capabilities are not implemented: "
            + ", ".join(missing)
        )


def _require_api_delegation(
    path: str, service_symbol: str, method: str = "POST"
):
    if not _endpoint_delegates(path, service_symbol, method):
        raise M4CapabilityUnavailable(
            f"{path} does not delegate lifecycle.{service_symbol}"
        )


def _require_import_pipeline(*symbols: str):
    values = {
        "prepare_manual_json_import": _PREPARE_MANUAL_IMPORT,
        "prepare_file_import": _PREPARE_FILE_IMPORT,
        "persist_prepared_import": _PERSIST_PREPARED_IMPORT,
    }
    missing = [name for name in symbols if not callable(values[name])]
    if missing:
        raise M4CapabilityUnavailable(
            f"{PIPELINE_MODULE} is missing: " + ", ".join(missing)
        )


def _require_cli_delegation(function_name: str, service_symbol: str):
    if not _cli_capability_present(function_name, service_symbol):
        raise M4CapabilityUnavailable(
            f"CLI {function_name} or lifecycle.{service_symbol} is not implemented"
        )


def _require_batch_pin_schema():
    if (
        "rubric_version_id" not in BatchCreate.model_fields
        or "rubric_version_id" not in BatchRead.model_fields
    ):
        raise M4CapabilityUnavailable(
            "BatchCreate and BatchRead must expose rubric_version_id"
        )


@pytest.fixture()
def lifecycle_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    enable_sqlite_foreign_keys(engine)
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def m4_api_env():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    enable_sqlite_foreign_keys(engine)
    models.Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, session_factory=SessionLocal)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _audit_event(
    compilation,
    *,
    rule_code: str,
    action: str,
    field_path: str,
) -> dict:
    matches = [
        deepcopy(item)
        for item in compilation.human_changes
        if item.get("rule_code") == rule_code and item.get("action") == action
    ]
    assert len(matches) == 1
    event = matches[0]
    assert set(event) == {
        "change_id",
        "rule_code",
        "field_path",
        "action",
        "before",
        "after",
        "actor_id",
        "occurred_at",
        "reason",
    }
    assert isinstance(event["change_id"], str) and event["change_id"]
    assert event["field_path"] == field_path
    assert event["reason"].strip()
    return event


def _rule_event(compilation, rule_code: str, action: str) -> dict:
    return _audit_event(
        compilation,
        rule_code=rule_code,
        action=action,
        field_path=f"/atomic_rules/{rule_code}/status",
    )


def _iso_naive_utc(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


@requires_rule_submit
def test_atomic_rule_draft_can_enter_review_without_fabricated_signoff(lifecycle_db):
    graph = _make_full_graph(lifecycle_db, "m4-rule-submit")
    graph.deterministic.status = "draft"
    graph.deterministic.reviewed_by = None
    graph.deterministic.reviewed_at = None
    graph.compilation.human_changes = []
    lifecycle_db.commit()
    now = graph.compilation.created_at + timedelta(minutes=10)

    returned = _require_lifecycle("submit_atomic_rule_for_review")(
        lifecycle_db,
        graph.rubric.id,
        graph.deterministic.rule_code,
        graph.user.id,
        "规则已准备好进入人工审核",
        now=now,
    )
    lifecycle_db.commit()
    lifecycle_db.refresh(graph.deterministic)

    assert returned.id == graph.deterministic.id
    assert graph.deterministic.status == "review"
    assert graph.deterministic.reviewed_by is None
    assert graph.deterministic.reviewed_at is None
    lifecycle_db.refresh(graph.compilation)
    event = _rule_event(
        graph.compilation, graph.deterministic.rule_code, "submit_review"
    )
    assert event["before"] == "draft"
    assert event["after"] == "review"
    assert event["actor_id"] == graph.user.id
    assert event["occurred_at"] == _iso_naive_utc(now)
    assert event["reason"] == "规则已准备好进入人工审核"


@requires_rule_submit
def test_atomic_rule_review_rejects_an_event_before_the_graph_existed_atomically(
    lifecycle_db,
):
    graph = _make_full_graph(lifecycle_db, "m4-rule-review-past-clock")
    graph.deterministic.status = "draft"
    graph.deterministic.reviewed_by = None
    graph.deterministic.reviewed_at = None
    graph.compilation.human_changes = []
    lifecycle_db.commit()

    with pytest.raises(lifecycle.RubricLifecycleError, match="时间不能早"):
        _require_lifecycle("submit_atomic_rule_for_review")(
            lifecycle_db,
            graph.rubric.id,
            graph.deterministic.rule_code,
            graph.user.id,
            "不能把过去时间写成当前审核事实",
            now=graph.compilation.created_at - timedelta(seconds=1),
        )

    lifecycle_db.rollback()
    lifecycle_db.refresh(graph.deterministic)
    lifecycle_db.refresh(graph.compilation)
    assert graph.deterministic.status == "draft"
    assert graph.compilation.human_changes == []


@requires_rule_edit
def test_edit_atomic_rule_is_draft_only_audited_and_rebuilds_legacy_projection(
    lifecycle_db,
):
    graph = _make_full_graph(lifecycle_db, "m4-rule-edit")
    graph.deterministic.status = "draft"
    graph.deterministic.reviewed_by = None
    graph.deterministic.reviewed_at = None
    graph.compilation.human_changes = []
    lifecycle_db.commit()
    now = graph.compilation.created_at + timedelta(minutes=15)
    changes = {
        "rule_text": "必须完整说明数据准备、训练、验证与复现实验步骤。",
        "max_points": 1,
        "checker_params": {
            "required_labels": ["数据准备", "训练", "验证", "复现实验"]
        },
    }

    returned = _require_lifecycle("edit_atomic_rule")(
        lifecycle_db,
        graph.rubric.id,
        graph.deterministic.rule_code,
        changes,
        graph.user.id,
        "根据复核意见补充复现实验要求",
        now=now,
    )
    # A caller-owned mapping cannot mutate the persisted audit snapshot later.
    changes["checker_params"]["required_labels"].append("恶意后改")
    lifecycle_db.commit()
    lifecycle_db.refresh(graph.deterministic)
    lifecycle_db.refresh(graph.criterion)
    lifecycle_db.refresh(graph.compilation)

    assert returned.id == graph.deterministic.id
    assert graph.deterministic.rule_text.endswith("复现实验步骤。")
    assert float(graph.deterministic.max_points) == 1
    assert graph.deterministic.checker_params["required_labels"] == [
        "数据准备",
        "训练",
        "验证",
        "复现实验",
    ]
    event = _audit_event(
        graph.compilation,
        rule_code=graph.deterministic.rule_code,
        action="edit",
        field_path=f"/atomic_rules/{graph.deterministic.rule_code}",
    )
    assert event["actor_id"] == graph.user.id
    assert event["occurred_at"] == _iso_naive_utc(now)
    assert event["reason"] == "根据复核意见补充复现实验要求"
    assert isinstance(event["before"], dict) and isinstance(event["after"], dict)
    assert event["before"]["max_points"] != event["after"]["max_points"]
    assert "恶意后改" not in repr(event)

    # The legacy criterion is a derived compatibility projection, not a second
    # editable truth.  It must carry the edited rule identity and values.
    projection = repr(graph.criterion.deduction_rules_structured)
    assert graph.deterministic.rule_code in projection
    assert "复现实验" in projection
    assert "1" in projection


@pytest.mark.parametrize(
    ("rule_status", "rubric_status"),
    [("review", "draft"), ("approved", "draft"), ("approved", "published")],
)
@requires_rule_edit
def test_edit_atomic_rule_rejects_review_approved_and_published_graphs_atomically(
    lifecycle_db, rule_status, rubric_status
):
    graph = _make_full_graph(
        lifecycle_db, f"m4-rule-edit-reject-{rule_status}-{rubric_status}"
    )
    graph.deterministic.status = rule_status
    graph.compilation.human_changes = []
    lifecycle_db.commit()
    if rubric_status == "published":
        lifecycle_db.execute(
            update(models.Rubric)
            .where(models.Rubric.id == graph.rubric.id)
            .values(status="published", published_at=datetime(2026, 7, 19, 12, 0, 0))
        )
        lifecycle_db.commit()
        lifecycle_db.expire_all()
    rule = lifecycle_db.get(models.AtomicRule, graph.deterministic.id)
    compilation = lifecycle_db.get(models.RubricCompilation, graph.compilation.id)
    criterion = lifecycle_db.get(models.RubricCriterion, graph.criterion.id)
    before = (
        rule.rule_text,
        deepcopy(rule.checker_params),
        deepcopy(criterion.deduction_rules_structured),
        deepcopy(compilation.human_changes),
    )

    with pytest.raises((ValueError, getattr(lifecycle, "RubricLifecycleError", ValueError))):
        _require_lifecycle("edit_atomic_rule")(
            lifecycle_db,
            graph.rubric.id,
            rule.rule_code,
            {"rule_text": "不应被保存"},
            graph.user.id,
            "非法状态编辑",
            now=graph.compilation.created_at + timedelta(minutes=16),
        )
        lifecycle_db.commit()
    lifecycle_db.rollback()
    lifecycle_db.refresh(rule)
    lifecycle_db.refresh(criterion)
    lifecycle_db.refresh(compilation)
    assert (
        rule.rule_text,
        rule.checker_params,
        criterion.deduction_rules_structured,
        compilation.human_changes,
    ) == before


@pytest.mark.parametrize(
    "forbidden_changes",
    [
        {"rule_code": "FORGED-RULE-CODE"},
        {"criterion_id": "forged-criterion-id"},
        {"rubric_version_id": "forged-version-id"},
        {"status": "approved"},
        {"reviewed_by": "forged-reviewer", "reviewed_at": "2026-07-19T12:00:00"},
        {"source_rule_ids": []},
    ],
)
@requires_rule_edit
def test_edit_atomic_rule_allowlist_cannot_bypass_identity_review_or_source_lineage(
    lifecycle_db, forbidden_changes
):
    graph = _make_full_graph(
        lifecycle_db,
        "m4-rule-edit-forbidden-" + next(iter(forbidden_changes)).replace("_", "-"),
    )
    graph.deterministic.status = "draft"
    graph.deterministic.reviewed_by = None
    graph.deterministic.reviewed_at = None
    graph.compilation.human_changes = []
    lifecycle_db.commit()
    rule = graph.deterministic
    before = {
        "rule_code": rule.rule_code,
        "criterion_id": rule.criterion_id,
        "rubric_version_id": rule.rubric_version_id,
        "status": rule.status,
        "reviewed_by": rule.reviewed_by,
        "reviewed_at": rule.reviewed_at,
        "source_rule_ids": sorted(item.id for item in rule.source_rules),
        "projection": deepcopy(graph.criterion.deduction_rules_structured),
        "events": deepcopy(graph.compilation.human_changes),
    }

    with pytest.raises((ValueError, getattr(lifecycle, "RubricLifecycleError", ValueError))):
        _require_lifecycle("edit_atomic_rule")(
            lifecycle_db,
            graph.rubric.id,
            rule.rule_code,
            forbidden_changes,
            graph.user.id,
            "试图修改禁止字段",
            now=graph.compilation.created_at + timedelta(minutes=17),
        )
        lifecycle_db.commit()
    lifecycle_db.rollback()
    lifecycle_db.refresh(rule)
    lifecycle_db.refresh(graph.criterion)
    lifecycle_db.refresh(graph.compilation)
    assert {
        "rule_code": rule.rule_code,
        "criterion_id": rule.criterion_id,
        "rubric_version_id": rule.rubric_version_id,
        "status": rule.status,
        "reviewed_by": rule.reviewed_by,
        "reviewed_at": rule.reviewed_at,
        "source_rule_ids": sorted(item.id for item in rule.source_rules),
        "projection": graph.criterion.deduction_rules_structured,
        "events": graph.compilation.human_changes,
    } == before


@requires_rule_approve
def test_approve_atomic_rule_writes_exactly_matching_audit_event(lifecycle_db):
    graph = _make_full_graph(lifecycle_db, "m4-rule-approve")
    graph.compilation.human_changes = []
    lifecycle_db.commit()
    now = graph.compilation.created_at + timedelta(minutes=20)

    returned = _require_lifecycle("approve_atomic_rule")(
        lifecycle_db,
        graph.rubric.id,
        graph.deterministic.rule_code,
        graph.user.id,
        "检查器参数、证据范围与来源均已复核",
        now=now,
    )
    lifecycle_db.commit()
    lifecycle_db.refresh(graph.deterministic)
    lifecycle_db.refresh(graph.compilation)

    assert returned.id == graph.deterministic.id
    assert graph.deterministic.status == "approved"
    assert graph.deterministic.reviewed_by == graph.user.id
    assert graph.deterministic.reviewed_at == now
    event = _rule_event(
        graph.compilation, graph.deterministic.rule_code, "approve"
    )
    assert event["before"] == "review"
    assert event["after"] == "approved"
    assert event["actor_id"] == graph.deterministic.reviewed_by
    assert event["occurred_at"] == _iso_naive_utc(graph.deterministic.reviewed_at)


@requires_rule_reject
def test_reject_atomic_rule_writes_exactly_matching_audit_event(lifecycle_db):
    graph = _make_full_graph(lifecycle_db, "m4-rule-reject")
    graph.compilation.human_changes = []
    lifecycle_db.commit()
    now = graph.compilation.created_at + timedelta(minutes=30)

    returned = _require_lifecycle("reject_atomic_rule")(
        lifecycle_db,
        graph.rubric.id,
        graph.semantic.rule_code,
        graph.user.id,
        "证据策略没有覆盖跨章节引文",
        now=now,
    )
    lifecycle_db.commit()
    lifecycle_db.refresh(graph.semantic)
    lifecycle_db.refresh(graph.compilation)

    assert returned.id == graph.semantic.id
    assert graph.semantic.status == "rejected"
    assert graph.semantic.reviewed_by == graph.user.id
    assert graph.semantic.reviewed_at == now
    event = _rule_event(graph.compilation, graph.semantic.rule_code, "reject")
    assert event["before"] == "review"
    assert event["after"] == "rejected"
    assert event["actor_id"] == graph.semantic.reviewed_by
    assert event["occurred_at"] == _iso_naive_utc(graph.semantic.reviewed_at)


def _assert_illegal_rule_review_is_atomic(lifecycle_db, symbol, initial_status):
    graph = _make_full_graph(lifecycle_db, f"m4-illegal-{symbol}-{initial_status}")
    rule = graph.deterministic
    rule.status = initial_status
    graph.compilation.human_changes = []
    lifecycle_db.commit()
    before = (rule.status, rule.reviewed_by, rule.reviewed_at, [])

    with pytest.raises((ValueError, getattr(lifecycle, "RubricLifecycleError", ValueError))):
        _require_lifecycle(symbol)(
            lifecycle_db,
            graph.rubric.id,
            rule.rule_code,
            graph.user.id,
            "非法状态不应留下事件",
            now=graph.compilation.created_at + timedelta(minutes=40),
        )
        lifecycle_db.commit()
    lifecycle_db.rollback()
    lifecycle_db.refresh(rule)
    lifecycle_db.refresh(graph.compilation)
    assert (rule.status, rule.reviewed_by, rule.reviewed_at, graph.compilation.human_changes) == before


@pytest.mark.parametrize("initial_status", ["draft", "approved"])
@requires_rule_approve
def test_approve_rejects_illegal_state_without_partial_audit(
    lifecycle_db, initial_status
):
    _require_lifecycle("approve_atomic_rule")
    _assert_illegal_rule_review_is_atomic(
        lifecycle_db, "approve_atomic_rule", initial_status
    )


@pytest.mark.parametrize("initial_status", ["draft", "rejected"])
@requires_rule_reject
def test_reject_rejects_illegal_state_without_partial_audit(
    lifecycle_db, initial_status
):
    _require_lifecycle("reject_atomic_rule")
    _assert_illegal_rule_review_is_atomic(
        lifecycle_db, "reject_atomic_rule", initial_status
    )


@pytest.mark.parametrize(
    ("decision", "expected_action"),
    [("confirmed", "confirm_template_link"), ("rejected", "reject_template_link")],
)
@requires_link_review
def test_template_link_review_records_reviewer_time_reason_and_event(
    lifecycle_db, decision, expected_action
):
    graph = _make_full_graph(lifecycle_db, f"m4-link-{decision}")
    graph.link.review_status = "pending"
    graph.link.reviewed_by = None
    graph.link.reviewed_at = None
    graph.compilation.human_changes = []
    lifecycle_db.commit()
    now = graph.compilation.created_at + timedelta(minutes=50)

    returned = _require_lifecycle("review_template_link")(
        lifecycle_db,
        graph.rubric.id,
        graph.link.id,
        graph.user.id,
        decision,
        "已逐字核对 Word 模板定位",
        now=now,
    )
    lifecycle_db.commit()
    lifecycle_db.refresh(graph.link)
    lifecycle_db.refresh(graph.compilation)

    assert returned.id == graph.link.id
    assert graph.link.review_status == decision
    assert graph.link.reviewed_by == graph.user.id
    assert graph.link.reviewed_at == now
    event = _audit_event(
        graph.compilation,
        rule_code=graph.deterministic.rule_code,
        action=expected_action,
        field_path=f"/rule_template_links/{graph.link.id}/review_status",
    )
    assert event["before"] == "pending"
    assert event["after"] == decision
    assert event["actor_id"] == graph.link.reviewed_by
    assert event["occurred_at"] == _iso_naive_utc(graph.link.reviewed_at)
    assert event["reason"] == "已逐字核对 Word 模板定位"


@requires_rubric_review_api
def test_rubric_api_supports_draft_review_draft_cycle(m4_api_env):
    _require_api_delegations(_RUBRIC_REVIEW_PATHS)
    with m4_api_env.session_factory() as session:
        graph = _make_full_graph(session, "m4-api-review-cycle")
        rubric_id = graph.rubric.id

    submitted = m4_api_env.client.post(f"/api/rubrics/{rubric_id}/submit-review")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "review"

    returned = m4_api_env.client.post(f"/api/rubrics/{rubric_id}/return-to-draft")
    assert returned.status_code == 200, returned.text
    assert returned.json()["status"] == "draft"


@requires_rule_submit_approve_api
def test_api_rule_submit_then_approve_persists_matching_reviewer_and_audit(
    m4_api_env,
):
    _require_api_delegations(_RULE_SUBMIT_APPROVE_PATHS)
    with m4_api_env.session_factory() as session:
        graph = _make_full_graph(session, "m4-api-rule-review")
        graph.deterministic.status = "draft"
        graph.deterministic.reviewed_by = None
        graph.deterministic.reviewed_at = None
        graph.compilation.human_changes = []
        session.commit()
        rubric_id = graph.rubric.id
        rule_id = graph.deterministic.id
        rule_code = graph.deterministic.rule_code
        compilation_id = graph.compilation.id

    submitted = m4_api_env.client.post(
        f"/api/rubrics/{rubric_id}/rules/{rule_code}/submit-review",
        json={"reason": "API 提交规则审核"},
    )
    assert submitted.status_code == 200, submitted.text
    with m4_api_env.session_factory() as session:
        rule = session.get(models.AtomicRule, rule_id)
        compilation = session.get(models.RubricCompilation, compilation_id)
        assert rule.status == "review"
        assert rule.reviewed_by is None and rule.reviewed_at is None
        submit_event = _rule_event(compilation, rule_code, "submit_review")
        assert submit_event["before"] == "draft"
        assert submit_event["after"] == "review"
        assert submit_event["actor_id"] == settings.DEFAULT_DEV_USER_ID
        assert submit_event["reason"] == "API 提交规则审核"

    approved = m4_api_env.client.post(
        f"/api/rubrics/{rubric_id}/rules/{rule_code}/approve",
        json={"reason": "API 审核确认规则可执行"},
    )
    assert approved.status_code == 200, approved.text
    with m4_api_env.session_factory() as session:
        rule = session.get(models.AtomicRule, rule_id)
        compilation = session.get(models.RubricCompilation, compilation_id)
        assert rule.status == "approved"
        assert rule.reviewed_by == settings.DEFAULT_DEV_USER_ID
        assert rule.reviewed_at is not None
        event = _rule_event(compilation, rule_code, "approve")
        assert event["actor_id"] == rule.reviewed_by
        assert event["occurred_at"] == _iso_naive_utc(rule.reviewed_at)
        assert event["reason"] == "API 审核确认规则可执行"


@requires_link_review_api
def test_api_template_link_confirm_persists_matching_reviewer_and_audit(m4_api_env):
    _require_api_delegations(_LINK_REVIEW_PATHS)
    with m4_api_env.session_factory() as session:
        graph = _make_full_graph(session, "m4-api-link-review")
        graph.link.review_status = "pending"
        graph.link.reviewed_by = None
        graph.link.reviewed_at = None
        graph.compilation.human_changes = []
        session.commit()
        rubric_id = graph.rubric.id
        link_id = graph.link.id
        compilation_id = graph.compilation.id

    response = m4_api_env.client.post(
        f"/api/rubrics/{rubric_id}/template-links/{link_id}/review",
        json={
            "decision": "confirmed",
            "reason": "API 已核对模板定位与规则关系",
        },
    )
    assert response.status_code == 200, response.text
    with m4_api_env.session_factory() as session:
        link = session.get(models.RuleTemplateLink, link_id)
        compilation = session.get(models.RubricCompilation, compilation_id)
        assert link.review_status == "confirmed"
        assert link.reviewed_by == settings.DEFAULT_DEV_USER_ID
        assert link.reviewed_at is not None
        rule_code = session.get(models.AtomicRule, link.rule_id).rule_code
        event = _audit_event(
            compilation,
            rule_code=rule_code,
            action="confirm_template_link",
            field_path=f"/rule_template_links/{link_id}/review_status",
        )
        assert event["before"] == "pending"
        assert event["after"] == "confirmed"
        assert event["actor_id"] == link.reviewed_by
        assert event["occurred_at"] == _iso_naive_utc(link.reviewed_at)
        assert event["reason"] == "API 已核对模板定位与规则关系"


@requires_rule_edit_api
def test_api_rule_patch_delegates_edit_atomic_rule(m4_api_env, monkeypatch):
    _require_lifecycle("edit_atomic_rule")
    _require_api_delegation(
        _RULE_EDIT_API_PATH, "edit_atomic_rule", method="PATCH"
    )
    with m4_api_env.session_factory() as session:
        graph = _make_full_graph(session, "m4-api-rule-edit-delegation")
        graph.deterministic.status = "draft"
        graph.deterministic.reviewed_by = None
        graph.deterministic.reviewed_at = None
        session.commit()
        rubric_id = graph.rubric.id
        rule_code = graph.deterministic.rule_code
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        raise _DelegationObserved("rule edit delegated")

    endpoint = _route_endpoint(_RULE_EDIT_API_PATH, method="PATCH")
    assert endpoint is not None
    _install_endpoint_service_spy(monkeypatch, endpoint, "edit_atomic_rule", spy)
    with pytest.raises(_DelegationObserved, match="rule edit delegated"):
        m4_api_env.client.patch(
            f"/api/rubrics/{rubric_id}/rules/{rule_code}",
            json={
                "changes": {"rule_text": "API 修改后的规则文本"},
                "reason": "API 编辑规则",
            },
        )
    assert len(calls) == 1
    serialized = repr(calls[0])
    assert rubric_id in serialized
    assert rule_code in serialized
    assert "API 修改后的规则文本" in serialized


@pytest.mark.parametrize(
    ("path_template", "service_symbol", "body"),
    [
        pytest.param(
            "/api/rubrics/{rubric_id}/submit-review",
            "submit_for_review",
            None,
            marks=_API_DELEGATION_MARKS[
                "/api/rubrics/{rubric_id}/submit-review"
            ],
        ),
        pytest.param(
            "/api/rubrics/{rubric_id}/return-to-draft",
            "return_to_draft",
            None,
            marks=_API_DELEGATION_MARKS[
                "/api/rubrics/{rubric_id}/return-to-draft"
            ],
        ),
        pytest.param(
            "/api/rubrics/{rubric_id}/rules/{rule_code}/submit-review",
            "submit_atomic_rule_for_review",
            {"reason": "API delegation submit"},
            marks=_API_DELEGATION_MARKS[
                "/api/rubrics/{rubric_id}/rules/{rule_code}/submit-review"
            ],
        ),
        pytest.param(
            "/api/rubrics/{rubric_id}/rules/{rule_code}/approve",
            "approve_atomic_rule",
            {"reason": "API delegation approve"},
            marks=_API_DELEGATION_MARKS[
                "/api/rubrics/{rubric_id}/rules/{rule_code}/approve"
            ],
        ),
        pytest.param(
            "/api/rubrics/{rubric_id}/rules/{rule_code}/reject",
            "reject_atomic_rule",
            {"reason": "API delegation reject"},
            marks=_API_DELEGATION_MARKS[
                "/api/rubrics/{rubric_id}/rules/{rule_code}/reject"
            ],
        ),
        pytest.param(
            "/api/rubrics/{rubric_id}/template-links/{link_id}/review",
            "review_template_link",
            {"decision": "confirmed", "reason": "API delegation link"},
            marks=_API_DELEGATION_MARKS[
                "/api/rubrics/{rubric_id}/template-links/{link_id}/review"
            ],
        ),
    ],
)
def test_each_lifecycle_api_endpoint_delegates_the_matching_service(
    m4_api_env,
    monkeypatch,
    path_template,
    service_symbol,
    body,
):
    _require_api_delegations((path_template,))
    with m4_api_env.session_factory() as session:
        graph = _make_full_graph(
            session, f"m4-api-delegate-{service_symbol}"
        )
        rubric_id = graph.rubric.id
        rule_code = graph.deterministic.rule_code
        link_id = graph.link.id
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        raise _DelegationObserved(f"{service_symbol} delegated")

    endpoint = _route_endpoint(path_template)
    assert endpoint is not None
    _install_endpoint_service_spy(monkeypatch, endpoint, service_symbol, spy)
    url = path_template.format(
        rubric_id=rubric_id, rule_code=rule_code, link_id=link_id
    )
    with pytest.raises(_DelegationObserved, match=f"{service_symbol} delegated"):
        m4_api_env.client.post(url, json=body)
    assert len(calls) == 1
    assert rubric_id in repr(calls[0])


def _post_endpoint(path: str):
    matches = [
        route.endpoint
        for route in app.routes
        if route.path == path and "POST" in getattr(route, "methods", set())
    ]
    assert len(matches) == 1, f"expected exactly one POST endpoint for {path}"
    return matches[0]


def _install_endpoint_service_spy(monkeypatch, endpoint, service_symbol, spy):
    original = vars(lifecycle).get(service_symbol)
    assert callable(original), f"lifecycle.{service_symbol} must exist"
    installed = False
    for name, value in list(endpoint.__globals__.items()):
        if value is lifecycle:
            monkeypatch.setattr(lifecycle, service_symbol, spy)
            installed = True
        elif value is original and value is not endpoint:
            monkeypatch.setitem(endpoint.__globals__, name, spy)
            installed = True
    if not installed:
        raise M4CapabilityUnavailable(
            f"API endpoint does not delegate lifecycle.{service_symbol}"
        )


def _install_cli_service_spy(monkeypatch, service_symbol, spy):
    original = vars(lifecycle).get(service_symbol)
    assert callable(original), f"lifecycle.{service_symbol} must exist"
    monkeypatch.setattr(lifecycle, service_symbol, spy)
    for name, value in list(vars(cli_main).items()):
        if value is original:
            monkeypatch.setattr(cli_main, name, spy)


def _install_lifecycle_validator_spy(monkeypatch, spy):
    validator_module = importlib.import_module(VALIDATOR_MODULE)
    original = vars(validator_module).get("validate_publishable_rubric")
    assert callable(original)
    monkeypatch.setattr(validator_module, "validate_publishable_rubric", spy)
    for name, value in list(lifecycle.publish_rubric.__globals__.items()):
        if value is original:
            monkeypatch.setitem(lifecycle.publish_rubric.__globals__, name, spy)


@requires_lifecycle_validator_wiring
def test_lifecycle_publish_uses_validator_and_blockers_reject_atomically(
    lifecycle_db,
    monkeypatch,
):
    _require_validator()
    if not _lifecycle_publish_uses_validator():
        raise M4CapabilityUnavailable(
            "lifecycle.publish_rubric does not use validate_publishable_rubric"
        )
    graph = _make_full_graph(lifecycle_db, "m4-publish-validator-wiring")
    lifecycle.submit_for_review(lifecycle_db, graph.rubric.id)
    lifecycle_db.commit()
    before = {
        "rubric_status": graph.rubric.status,
        "rubric_published_at": graph.rubric.published_at,
        "reviewed_by": graph.compilation.reviewed_by,
        "reviewed_at": graph.compilation.reviewed_at,
        "published_at": graph.compilation.published_at,
        "version_hash": graph.version.version_hash,
        "final_version_hash": graph.compilation.final_version_hash,
    }
    calls = []

    def blocked_validator(*args, **kwargs):
        calls.append((args, kwargs))
        return (
            {
                "code": "checker_not_registered",
                "field_path": "/atomic_rules/METHOD-01-D/checker_key",
                "identity": {
                    "rubric_id": graph.rubric.id,
                    "compilation_id": graph.compilation.id,
                    "rule_code": graph.deterministic.rule_code,
                },
                "message": "checker is unavailable",
            },
        )

    _install_lifecycle_validator_spy(monkeypatch, blocked_validator)
    with pytest.raises((ValueError, getattr(lifecycle, "RubricLifecycleError", ValueError))):
        lifecycle.publish_rubric(
            lifecycle_db,
            graph.rubric.id,
            graph.compilation.id,
            graph.user.id,
            now=graph.compilation.created_at + timedelta(hours=2),
        )
        lifecycle_db.commit()
    lifecycle_db.rollback()
    lifecycle_db.expire_all()
    assert len(calls) == 1
    assert before == {
        "rubric_status": lifecycle_db.get(models.Rubric, graph.rubric.id).status,
        "rubric_published_at": lifecycle_db.get(models.Rubric, graph.rubric.id).published_at,
        "reviewed_by": lifecycle_db.get(models.RubricCompilation, graph.compilation.id).reviewed_by,
        "reviewed_at": lifecycle_db.get(models.RubricCompilation, graph.compilation.id).reviewed_at,
        "published_at": lifecycle_db.get(models.RubricCompilation, graph.compilation.id).published_at,
        "version_hash": lifecycle_db.get(models.RubricVersion, graph.version.id).version_hash,
        "final_version_hash": lifecycle_db.get(models.RubricCompilation, graph.compilation.id).final_version_hash,
    }


def test_publish_rejects_a_signoff_before_compilation_creation_atomically(
    lifecycle_db,
):
    graph = _make_full_graph(lifecycle_db, "m4-publish-past-clock")
    lifecycle.submit_for_review(lifecycle_db, graph.rubric.id)
    lifecycle_db.commit()
    original_hash = graph.version.version_hash

    with pytest.raises(lifecycle.RubricLifecycleError, match="时间不能早"):
        lifecycle.publish_rubric(
            lifecycle_db,
            graph.rubric.id,
            graph.compilation.id,
            graph.user.id,
            now=graph.compilation.created_at - timedelta(seconds=1),
        )

    lifecycle_db.rollback()
    lifecycle_db.expire_all()
    rubric = lifecycle_db.get(models.Rubric, graph.rubric.id)
    compilation = lifecycle_db.get(
        models.RubricCompilation, graph.compilation.id
    )
    version = lifecycle_db.get(models.RubricVersion, graph.version.id)
    assert rubric.status == "review"
    assert rubric.published_at is None
    assert compilation.reviewed_by is None
    assert compilation.reviewed_at is None
    assert compilation.published_at is None
    assert version.version_hash == original_hash


@requires_api_publish_lifecycle
def test_api_publish_delegates_the_shared_lifecycle_service(
    m4_api_env, monkeypatch
):
    _require_validator()
    _require_api_delegation(_API_PUBLISH_PATH, "publish_rubric")
    with m4_api_env.session_factory() as session:
        graph = _make_full_graph(session, "m4-api-publish-delegation")
        lifecycle.submit_for_review(session, graph.rubric.id)
        session.commit()
        rubric_id = graph.rubric.id
        compilation_id = graph.compilation.id
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        raise _DelegationObserved("publish delegated")

    endpoint = _post_endpoint("/api/rubrics/{rubric_id}/publish")
    _install_endpoint_service_spy(monkeypatch, endpoint, "publish_rubric", spy)
    with pytest.raises(_DelegationObserved, match="publish delegated"):
        m4_api_env.client.post(
            f"/api/rubrics/{rubric_id}/publish",
            json={"compilation_id": compilation_id, "reason": "M4 API publication"},
        )

    assert len(calls) == 1
    serialized = repr(calls[0])
    assert rubric_id in serialized
    assert compilation_id in serialized


@requires_api_clone_lifecycle
def test_api_clone_delegates_deep_clone_lifecycle_service(m4_api_env, monkeypatch):
    _require_api_delegation(_API_CLONE_PATH, "clone_published_rubric")
    with m4_api_env.session_factory() as session:
        graph = _make_full_graph(session, "m4-api-clone-delegation")
        session.execute(
            update(models.Rubric)
            .where(models.Rubric.id == graph.rubric.id)
            .values(status="published", published_at=datetime(2026, 7, 19, 12, 0, 0))
        )
        session.commit()
        rubric_id = graph.rubric.id
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        raise _DelegationObserved("clone delegated")

    endpoint = _post_endpoint("/api/rubrics/{rubric_id}/clone")
    _install_endpoint_service_spy(
        monkeypatch, endpoint, "clone_published_rubric", spy
    )
    with pytest.raises(_DelegationObserved, match="clone delegated"):
        m4_api_env.client.post(
            f"/api/rubrics/{rubric_id}/clone",
            json={"new_version": "2.0.0"},
        )
    assert len(calls) == 1
    assert rubric_id in repr(calls[0])


@requires_api_clone_lifecycle
def test_api_clone_preserves_external_contract_and_deep_clones_provenance_graph(
    m4_api_env,
):
    _require_api_delegation(_API_CLONE_PATH, "clone_published_rubric")
    published_at = datetime(2026, 7, 19, 12, 0, 0)
    with m4_api_env.session_factory() as session:
        source = _make_full_graph(session, "m4-api-clone-deep")
        session.execute(
            update(models.Rubric)
            .where(models.Rubric.id == source.rubric.id)
            .values(status="published", published_at=published_at)
        )
        session.execute(
            update(models.RubricCompilation)
            .where(models.RubricCompilation.id == source.compilation.id)
            .values(
                reviewed_by=source.user.id,
                reviewed_at=published_at,
                published_at=published_at,
            )
        )
        session.commit()
        rubric_id = source.rubric.id
        source_counts = {
            "criteria": len(source.rubric.criteria),
            "rules": session.query(models.AtomicRule)
            .filter_by(rubric_version_id=source.version.id)
            .count(),
            "links": session.query(models.RuleTemplateLink)
            .join(models.AtomicRule)
            .filter(models.AtomicRule.rubric_version_id == source.version.id)
            .count(),
        }

    response = m4_api_env.client.post(
        f"/api/rubrics/{rubric_id}/clone",
        json={"new_version": "2.0.0"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "draft"
    assert payload["version"] == "2.0.0"
    assert len(payload["criteria"]) == source_counts["criteria"]

    with m4_api_env.session_factory() as session:
        clone = session.get(models.Rubric, payload["id"])
        compilations = session.scalars(
            select(models.RubricCompilation).where(
                models.RubricCompilation.rubric_id == clone.id
            )
        ).all()
        versions = session.scalars(
            select(models.RubricVersion).where(models.RubricVersion.rubric_id == clone.id)
        ).all()
        assert len(compilations) == 1 and len(versions) == 1
        rules = session.scalars(
            select(models.AtomicRule).where(
                models.AtomicRule.rubric_version_id == versions[0].id
            )
        ).all()
        rule_ids = [item.id for item in rules]
        links = session.scalars(
            select(models.RuleTemplateLink).where(
                models.RuleTemplateLink.rule_id.in_(rule_ids)
            )
        ).all()
        assert len(rules) == source_counts["rules"]
        assert len(links) == source_counts["links"]
        assert all(item.status == "draft" for item in rules)
        assert all(item.reviewed_by is None and item.reviewed_at is None for item in rules)
        assert all(item.review_status == "pending" for item in links)
        assert compilations[0].published_at is None


@requires_cli_publish_lifecycle
def test_cli_publish_delegates_the_same_lifecycle_service(tmp_path, monkeypatch):
    _require_validator()
    _require_cli_delegation("publish", "publish_rubric")
    saved_settings = (settings.DATABASE_URL, settings.STORAGE_ROOT)
    try:
        db_path = tmp_path / "m4-cli.db"
        storage_path = tmp_path / "storage"
        cli_main._bootstrap(db_path, storage_path)
        with cli_db.cli_session() as session:
            graph = _make_full_graph(session, "m4-cli-publish-delegation")
            lifecycle.submit_for_review(session, graph.rubric.id)
            session.commit()
            rubric_id = graph.rubric.id
            compilation_id = graph.compilation.id
        calls = []

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            raise _DelegationObserved("CLI publish delegated")

        _install_cli_service_spy(monkeypatch, "publish_rubric", spy)
        result = CliRunner().invoke(
            cli_main.app,
            [
                "publish",
                rubric_id,
                "--db",
                str(db_path),
                "--storage",
                str(storage_path),
            ],
        )

        assert isinstance(result.exception, _DelegationObserved), result.output
        assert len(calls) == 1
        serialized = repr(calls[0])
        assert rubric_id in serialized
        assert compilation_id in serialized
    finally:
        settings.DATABASE_URL, settings.STORAGE_ROOT = saved_settings


@pytest.mark.parametrize(
    ("command", "service_symbol", "target_kind", "extra_args"),
    [
        pytest.param(
            "rule-submit",
            "submit_atomic_rule_for_review",
            "rule",
            ["--reason", "CLI 提交规则审核"],
            marks=requires_cli_rule_submit,
        ),
        pytest.param(
            "rule-edit",
            "edit_atomic_rule",
            "rule",
            [
                "--changes-json",
                '{"rule_text":"CLI 修改后的规则文本"}',
                "--reason",
                "CLI 编辑规则",
            ],
            marks=requires_cli_rule_edit,
        ),
        pytest.param(
            "rule-approve",
            "approve_atomic_rule",
            "rule",
            ["--reason", "CLI 确认规则可执行"],
            marks=requires_cli_rule_approve,
        ),
        pytest.param(
            "rule-reject",
            "reject_atomic_rule",
            "rule",
            ["--reason", "CLI 驳回规则"],
            marks=requires_cli_rule_reject,
        ),
        pytest.param(
            "template-link-review",
            "review_template_link",
            "link",
            ["--decision", "confirmed", "--reason", "CLI 确认模板映射"],
            marks=requires_cli_link_review,
        ),
    ],
)
def test_cli_rule_and_template_review_commands_delegate_lifecycle_services(
    tmp_path,
    monkeypatch,
    command,
    service_symbol,
    target_kind,
    extra_args,
):
    function_name = command.replace("-", "_")
    _require_lifecycle(service_symbol)
    _require_cli_delegation(function_name, service_symbol)
    saved_settings = (settings.DATABASE_URL, settings.STORAGE_ROOT)
    try:
        db_path = tmp_path / f"m4-cli-{command}.db"
        storage_path = tmp_path / f"storage-{command}"
        cli_main._bootstrap(db_path, storage_path)
        with cli_db.cli_session() as session:
            graph = _make_full_graph(session, f"m4-cli-{command}")
            if command in {"rule-submit", "rule-edit"}:
                graph.deterministic.status = "draft"
                graph.deterministic.reviewed_by = None
                graph.deterministic.reviewed_at = None
            if target_kind == "link":
                graph.link.review_status = "pending"
                graph.link.reviewed_by = None
                graph.link.reviewed_at = None
            session.commit()
            rubric_id = graph.rubric.id
            target_id = (
                graph.deterministic.rule_code if target_kind == "rule" else graph.link.id
            )
        calls = []

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            raise _DelegationObserved(f"CLI {command} delegated")

        _install_cli_service_spy(monkeypatch, service_symbol, spy)
        result = CliRunner().invoke(
            cli_main.app,
            [
                command,
                rubric_id,
                target_id,
                *extra_args,
                "--db",
                str(db_path),
                "--storage",
                str(storage_path),
            ],
        )

        assert isinstance(result.exception, _DelegationObserved), result.output
        assert len(calls) == 1
        serialized = repr(calls[0])
        assert rubric_id in serialized
        assert target_id in serialized
    finally:
        settings.DATABASE_URL, settings.STORAGE_ROOT = saved_settings


@requires_legacy_upgrade
def test_legacy_draft_requires_explicit_upgrade_to_full_provenance_graph(lifecycle_db):
    user = models.User(
        username="m4-legacy-upgrader",
        display_name="M4 legacy upgrader",
        role="reviewer",
    )
    lifecycle_db.add(user)
    lifecycle_db.flush()
    rubric = models.Rubric(
        name="M4 legacy draft",
        version="legacy-v1",
        total_score=10,
        status="draft",
        created_by=user.id,
    )
    criterion = models.RubricCriterion(
        rubric=rubric,
        code="LEGACY-DIRECT",
        name="旧直接评分项",
        max_score=10,
        criterion_type="llm_judgment",
        scoring_mode="llm_direct",
    )
    lifecycle_db.add_all([rubric, criterion])
    lifecycle_db.commit()

    upgraded = _require_lifecycle("upgrade_legacy_draft")(
        lifecycle_db,
        rubric.id,
        user.id,
        reason="显式升级旧草稿并进入统一签核链",
    )
    lifecycle_db.commit()

    compilation = lifecycle_db.scalar(
        select(models.RubricCompilation).where(
            models.RubricCompilation.rubric_id == rubric.id
        )
    )
    version = lifecycle_db.scalar(
        select(models.RubricVersion).where(models.RubricVersion.rubric_id == rubric.id)
    )
    artifacts = lifecycle_db.scalars(
        select(models.SourceArtifact).where(
            models.SourceArtifact.compilation_id == compilation.id
        )
    ).all()
    rules = lifecycle_db.scalars(
        select(models.AtomicRule).where(
            models.AtomicRule.rubric_version_id == version.id
        )
    ).all()

    assert upgraded.id == rubric.id
    assert rubric.status == "draft"
    assert compilation.published_at is None
    assert any(item.artifact_type == "legacy_draft" for item in artifacts)
    assert len(rules) == 1 and rules[0].status == "draft"
    assert any(
        item.get("code") == "llm_direct_not_publishable"
        for item in compilation.blockers
    )
    assert any(
        item.get("action") == "legacy_upgrade"
        and item.get("actor_id") == user.id
        for item in compilation.human_changes
    )


@requires_legacy_signoff_chain
def test_upgraded_legacy_draft_uses_the_same_rule_and_rubric_signoff_services(
    lifecycle_db,
):
    upgrade = _require_lifecycle("upgrade_legacy_draft")
    submit_rule = _require_lifecycle("submit_atomic_rule_for_review")
    approve_rule = _require_lifecycle("approve_atomic_rule")
    user = models.User(
        username="m4-legacy-shared-signoff",
        display_name="M4 legacy shared signoff",
        role="reviewer",
    )
    lifecycle_db.add(user)
    lifecycle_db.flush()
    rubric = models.Rubric(
        name="M4 legacy shared signoff",
        version="legacy-v1",
        total_score=10,
        status="draft",
        created_by=user.id,
    )
    rubric.criteria.append(
        models.RubricCriterion(
            code="LEGACY",
            name="旧评分项",
            max_score=10,
            criterion_type="llm_judgment",
            scoring_mode="llm_direct",
        )
    )
    lifecycle_db.add(rubric)
    lifecycle_db.commit()
    upgrade(
        lifecycle_db,
        rubric.id,
        user.id,
        reason="旧草稿显式升级后进入统一签核链",
    )
    lifecycle_db.commit()
    version = lifecycle_db.scalar(
        select(models.RubricVersion).where(models.RubricVersion.rubric_id == rubric.id)
    )
    rules = lifecycle_db.scalars(
        select(models.AtomicRule)
        .where(models.AtomicRule.rubric_version_id == version.id)
        .order_by(models.AtomicRule.rule_code)
    ).all()
    assert rules
    compilation = lifecycle_db.get(
        models.RubricCompilation, version.compilation_id
    )
    base_time = max(
        [compilation.created_at, *(rule.created_at for rule in rules)]
    ) + timedelta(minutes=1)
    for index, rule in enumerate(rules):
        submit_rule(
            lifecycle_db,
            rubric.id,
            rule.rule_code,
            user.id,
            "旧草稿规则提交统一审核",
            now=base_time + timedelta(minutes=index),
        )
        approve_rule(
            lifecycle_db,
            rubric.id,
            rule.rule_code,
            user.id,
            "旧草稿规则完成统一审核",
            now=base_time + timedelta(minutes=10 + index),
        )
    lifecycle_db.commit()
    with pytest.raises(lifecycle.RubricLifecycleError, match="仍有阻断项"):
        lifecycle.submit_for_review(lifecycle_db, rubric.id)
    lifecycle_db.rollback()
    lifecycle_db.refresh(rubric)
    compilation = lifecycle_db.get(models.RubricCompilation, version.compilation_id)
    assert rubric.status == "draft"
    for rule in rules:
        lifecycle_db.refresh(rule)
        assert rule.status == "approved"
        event = _rule_event(compilation, rule.rule_code, "approve")
        assert event["actor_id"] == rule.reviewed_by
        assert event["occurred_at"] == _iso_naive_utc(rule.reviewed_at)


@requires_api_publish_lifecycle
def test_api_refuses_direct_publish_of_unupgraded_legacy_draft(m4_api_env):
    _require_validator()
    _require_api_delegation(_API_PUBLISH_PATH, "publish_rubric")
    with m4_api_env.session_factory() as session:
        user = models.User(
            username="m4-api-legacy",
            display_name="M4 API legacy",
            role="reviewer",
        )
        session.add(user)
        session.flush()
        rubric = models.Rubric(
            name="M4 API unupgraded legacy draft",
            version="legacy-v1",
            total_score=10,
            status="draft",
            created_by=user.id,
        )
        rubric.criteria.append(
            models.RubricCriterion(
                code="LEGACY",
                name="旧评分项",
                max_score=10,
                criterion_type="llm_judgment",
                scoring_mode="llm_direct",
            )
        )
        session.add(rubric)
        session.commit()
        rubric_id = rubric.id

    response = m4_api_env.client.post(
        f"/api/rubrics/{rubric_id}/publish",
        json={"reason": "legacy draft must not use the old direct publish path"},
    )
    assert response.status_code in {400, 409, 422}, response.text
    detail = response.text.lower()
    assert any(token in detail for token in ("upgrade", "provenance", "升级", "来源"))
    with m4_api_env.session_factory() as session:
        assert session.get(models.Rubric, rubric_id).status == "draft"


MANUAL_RUBRIC_PAYLOAD = {
    "name": "M4 manual JSON lifecycle",
    "version": "1.0.0",
    "total_score": 4,
    "criteria": [
        {
            "code": "METHOD",
            "name": "研究方法",
            "max_score": 4,
            "criterion_type": "deterministic",
            "scoring_mode": "deductive",
            "applies_to": "第三章",
            "deduction_rules_structured": [
                {
                    "match": {"missing": "验证"},
                    "points": 2,
                    "reason": "缺少验证步骤",
                }
            ],
        }
    ],
}


def _assert_full_draft_graph(session, rubric_id: str, artifact_type: str):
    rubric = session.get(models.Rubric, rubric_id)
    compilations = session.scalars(
        select(models.RubricCompilation).where(
            models.RubricCompilation.rubric_id == rubric_id
        )
    ).all()
    versions = session.scalars(
        select(models.RubricVersion).where(models.RubricVersion.rubric_id == rubric_id)
    ).all()
    assert rubric.status == "draft"
    assert len(compilations) == 1
    assert len(versions) == 1
    artifacts = session.scalars(
        select(models.SourceArtifact).where(
            models.SourceArtifact.compilation_id == compilations[0].id
        )
    ).all()
    artifact_ids = [item.id for item in artifacts]
    source_rules = session.scalars(
        select(models.SourceRule).where(
            models.SourceRule.source_artifact_id.in_(artifact_ids)
        )
    ).all()
    rules = session.scalars(
        select(models.AtomicRule).where(
            models.AtomicRule.rubric_version_id == versions[0].id
        )
    ).all()
    assert artifact_type in {item.artifact_type for item in artifacts}
    assert source_rules, "every create/import graph must preserve source-located rules"
    assert rules and all(item.status == "draft" for item in rules)
    assert all(item.source_rules for item in rules)
    assert compilations[0].reviewed_by is None
    assert compilations[0].published_at is None


@requires_manual_create_pipeline
def test_manual_json_create_keeps_external_response_and_enters_provenance_chain(
    m4_api_env,
):
    _require_import_pipeline(
        "prepare_manual_json_import", "persist_prepared_import"
    )
    response = m4_api_env.client.post("/api/rubrics", json=MANUAL_RUBRIC_PAYLOAD)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["name"] == MANUAL_RUBRIC_PAYLOAD["name"]
    assert payload["version"] == MANUAL_RUBRIC_PAYLOAD["version"]
    assert payload["status"] == "draft"
    assert [item["code"] for item in payload["criteria"]] == ["METHOD"]
    with m4_api_env.session_factory() as session:
        _assert_full_draft_graph(session, payload["id"], "manual_json")


@requires_manual_signoff_chain
def test_manual_json_graph_uses_the_same_rule_and_rubric_signoff_api(m4_api_env):
    _require_import_pipeline(
        "prepare_manual_json_import", "persist_prepared_import"
    )
    _require_api_delegations(_MANUAL_SIGNOFF_API_PATHS)
    created = m4_api_env.client.post(
        "/api/rubrics",
        json={**MANUAL_RUBRIC_PAYLOAD, "name": "M4 manual JSON shared signoff"},
    )
    assert created.status_code == 200, created.text
    rubric_id = created.json()["id"]
    with m4_api_env.session_factory() as session:
        version = session.scalar(
            select(models.RubricVersion).where(
                models.RubricVersion.rubric_id == rubric_id
            )
        )
        rules = session.scalars(
            select(models.AtomicRule)
            .where(models.AtomicRule.rubric_version_id == version.id)
            .order_by(models.AtomicRule.rule_code)
        ).all()
        rule_codes = [item.rule_code for item in rules]
        compilation_id = version.compilation_id
    assert rule_codes

    for rule_code in rule_codes:
        submitted = m4_api_env.client.post(
            f"/api/rubrics/{rubric_id}/rules/{rule_code}/submit-review",
            json={"reason": "manual_json 规则进入统一审核"},
        )
        assert submitted.status_code == 200, submitted.text
        approved = m4_api_env.client.post(
            f"/api/rubrics/{rubric_id}/rules/{rule_code}/approve",
            json={"reason": "manual_json 规则完成统一审核"},
        )
        assert approved.status_code == 200, approved.text
    submitted_rubric = m4_api_env.client.post(
        f"/api/rubrics/{rubric_id}/submit-review"
    )
    assert submitted_rubric.status_code == 200, submitted_rubric.text

    with m4_api_env.session_factory() as session:
        rubric = session.get(models.Rubric, rubric_id)
        compilation = session.get(models.RubricCompilation, compilation_id)
        rules = session.scalars(
            select(models.AtomicRule)
            .join(models.RubricVersion)
            .where(models.RubricVersion.rubric_id == rubric_id)
        ).all()
        assert rubric.status == "review"
        assert all(item.status == "approved" for item in rules)
        assert all(item.reviewed_by == settings.DEFAULT_DEV_USER_ID for item in rules)
        for rule in rules:
            event = _rule_event(compilation, rule.rule_code, "approve")
            assert event["actor_id"] == rule.reviewed_by
            assert event["occurred_at"] == _iso_naive_utc(rule.reviewed_at)


@requires_file_import_pipeline
def test_file_import_keeps_external_response_and_enters_provenance_chain(m4_api_env):
    _require_import_pipeline("prepare_file_import", "persist_prepared_import")
    response = m4_api_env.client.post(
        "/api/rubrics/import-files",
        data={"name": "M4 imported lifecycle", "version": "1.0.0"},
        files={
            "rules_file": (
                "rules.xlsx",
                make_rules_xlsx().getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            "template_file": (
                "template.docx",
                make_template_docx().getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["rubric"]["status"] == "draft"
    assert payload["warnings"] is not None
    with m4_api_env.session_factory() as session:
        _assert_full_draft_graph(session, payload["rubric"]["id"], "excel")


def _freeze_consistent_published_graph(session, graph):
    published_at = graph.compilation.created_at + timedelta(hours=1)
    audit_events = []
    for rule in (graph.deterministic, graph.semantic):
        audit_events.append(
            {
                "change_id": f"m4-batch-approve-{rule.rule_code}",
                "rule_code": rule.rule_code,
                "field_path": f"/atomic_rules/{rule.rule_code}/status",
                "action": "approve",
                "before": "review",
                "after": "approved",
                "actor_id": graph.user.id,
                "occurred_at": _iso_naive_utc(published_at),
                "reason": "M4 consistent published batch fixture",
            }
        )
    session.execute(
        update(models.AtomicRule)
        .where(models.AtomicRule.rubric_version_id == graph.version.id)
        .values(
            status="approved",
            reviewed_by=graph.user.id,
            reviewed_at=published_at,
        )
    )
    session.execute(
        update(models.RuleTemplateLink)
        .where(models.RuleTemplateLink.rule_id.in_([graph.deterministic.id, graph.semantic.id]))
        .values(
            review_status="confirmed",
            reviewed_by=graph.user.id,
            reviewed_at=published_at,
        )
    )
    session.execute(
        update(models.RubricCompilation)
        .where(models.RubricCompilation.id == graph.compilation.id)
        .values(
            status="validated",
            validation_result={"valid": True},
            blockers=[],
            human_changes=audit_events,
            reviewed_by=graph.user.id,
            reviewed_at=published_at,
            published_at=published_at,
        )
    )
    session.execute(
        update(models.Rubric)
        .where(models.Rubric.id == graph.rubric.id)
        .values(status="published", published_at=published_at)
    )
    session.commit()
    session.expire_all()
    rubric = session.get(models.Rubric, graph.rubric.id)
    compilation = session.get(models.RubricCompilation, graph.compilation.id)
    version = session.get(models.RubricVersion, graph.version.id)
    assert rubric.status == "published" and rubric.published_at == published_at
    assert compilation.status == "validated"
    assert compilation.published_at == published_at
    assert compilation.reviewed_by == graph.user.id
    assert compilation.final_version_hash == version.version_hash
    assert compilation.blockers == []
    assert len(compilation.human_changes) == 2


@requires_batch_version_pin
def test_batch_creation_pins_the_selected_published_rubric_version(m4_api_env):
    _require_batch_pin_schema()
    with m4_api_env.session_factory() as session:
        graph = _make_full_graph(session, "m4-batch-published")
        _freeze_consistent_published_graph(session, graph)
        rubric_id = graph.rubric.id
        version_id = graph.version.id

    response = m4_api_env.client.post(
        "/api/batches",
        json={
            "name": "M4 pinned batch",
            "rubric_id": rubric_id,
            "rubric_version_id": version_id,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["rubric_version_id"] == version_id
    with m4_api_env.session_factory() as session:
        batch = session.get(models.GradingBatch, response.json()["id"])
        assert batch.rubric_id == rubric_id
        assert batch.rubric_version_id == version_id


@requires_batch_version_pin
def test_batch_creation_rejects_draft_cross_rubric_or_unfrozen_version(m4_api_env):
    _require_batch_pin_schema()
    with m4_api_env.session_factory() as session:
        draft = _make_full_graph(session, "m4-batch-draft")
        other = _make_full_graph(session, "m4-batch-other")
        unfrozen = _make_full_graph(session, "m4-batch-unfrozen")
        session.execute(
            update(models.Rubric)
            .where(models.Rubric.id == unfrozen.rubric.id)
            .values(status="published", published_at=datetime(2026, 7, 19, 12, 0, 0))
        )
        session.commit()
        draft_id = draft.rubric.id
        draft_version_id = draft.version.id
        other_version_id = other.version.id
        unfrozen_id = unfrozen.rubric.id
        unfrozen_version_id = unfrozen.version.id

    for rubric_id, version_id in (
        (draft_id, draft_version_id),
        (draft_id, other_version_id),
        (unfrozen_id, unfrozen_version_id),
    ):
        response = m4_api_env.client.post(
            "/api/batches",
            json={
                "name": f"M4 invalid pinned batch {version_id}",
                "rubric_id": rubric_id,
                "rubric_version_id": version_id,
            },
        )
        assert response.status_code in {400, 409, 422}, response.text
