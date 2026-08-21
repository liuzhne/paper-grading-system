from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.session import get_db
from backend.app.db.sqlite import enable_sqlite_foreign_keys
from backend.app.main import app
from backend.app.cli.main import _resolve_cli_frozen_version
from backend.app.services.rubric_import import pipeline
from backend.app.services.rubrics import lifecycle


EVENT_FIELDS = {
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


@pytest.fixture()
def recovery_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    enable_sqlite_foreign_keys(engine)
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    actor = models.User(
        id="00000000-0000-0000-0000-000000000481",
        username="recovery-author",
        display_name="Recovery author",
        role="rubric_editor",
    )
    reviewer = models.User(
        id="00000000-0000-0000-0000-000000000482",
        username="recovery-reviewer",
        display_name="Recovery reviewer",
        role="rubric_reviewer",
    )
    session.add_all([actor, reviewer])
    session.commit()
    try:
        yield session, actor, reviewer
    finally:
        session.close()
        engine.dispose()


def _criterion(*, mode: str, points: str = "2") -> dict:
    value = {
        "code": "C01",
        "name": "论证质量",
        "max_score": "10",
        "weight": None,
        "description": "检查论证质量。",
        "criterion_type": "llm_judgment",
        "scoring_mode": mode,
        "applies_to": "global",
        "evidence_hints": [],
        "deduction_rules": [],
        "rubric_levels": [],
        "sub_checks": [],
        "dimension": "content",
        "deduction_rules_structured": [],
    }
    if mode == "banded":
        value["rubric_levels"] = [
            {"level_code": "HIGH", "points": "10", "descriptor": "充分"},
            {"level_code": "LOW", "points": "5", "descriptor": "部分充分"},
        ]
    if mode == "deductive":
        value["criterion_type"] = "deterministic"
        value["deduction_rules_structured"] = [
            {
                "points": points,
                "reason": "论证链缺失",
                "match": {"finding_code": "MISSING_ARGUMENT"},
                "repeat_policy": "once",
            }
        ]
    return value


def _command(
    *,
    mode: str,
    version: str,
    predecessor_id: str | None = None,
    points: str = "2",
) -> dict:
    command = {
        "schema_version": pipeline.IMPORT_SCHEMA_VERSION,
        "source_kind": "manual_json",
        "rubric": {
            "name": "Recovery rubric",
            "version": "v1",
            "total_score": "10",
            "criteria": [_criterion(mode=mode, points=points)],
            "business_profile_key": "thesis",
            "workflow_profile": "manual_json",
            "global_policy": {},
        },
        "compiler": {
            "parser_version": "manual-json-parser@1",
            "compiler_version": "atomic-rule-compiler@1",
            "prompt_version": "m4-recovery-test@1",
            "model_provider": None,
            "model_name": None,
            "sampling_params": {},
        },
        "version": {"version": version, "hash_scheme": "rubric-content-v2"},
    }
    if predecessor_id is not None:
        command["draft_recompile"] = {
            "mode": "supersede_unpublished",
            "supersedes_compilation_id": predecessor_id,
        }
    return command


def _initial_blocked(session, actor):
    prepared = pipeline.prepare_manual_json_import(
        command=_command(mode="llm_direct", version="blocked-v1")
    )
    identity = pipeline.persist_prepared_import(
        session=session,
        prepared=prepared,
        actor_id=actor.id,
    )
    return identity


def _recompile(session, actor, identity, *, mode="review_only", points="2"):
    prepared = pipeline.prepare_manual_json_recompile(
        command=_command(
            mode=mode,
            version=f"recovered-{mode}-v2",
            predecessor_id=identity.compilation_id,
            points=points,
        )
    )
    return pipeline.persist_prepared_import(
        session=session,
        prepared=prepared,
        actor_id=actor.id,
        target_rubric_id=identity.rubric_id,
        reason="人工明确选择恢复策略。",
    )


def test_superseding_recompile_preserves_history_and_resolves_active_rule(recovery_db):
    session, actor, _reviewer = recovery_db
    blocked = _initial_blocked(session, actor)
    old_compilation = session.get(models.RubricCompilation, blocked.compilation_id)
    old_blockers = deepcopy(old_compilation.blockers)
    old_version_id = blocked.rubric_version_id
    old_rule_id = session.scalar(
        select(models.AtomicRule.id).where(
            models.AtomicRule.rubric_version_id == old_version_id
        )
    )

    recovered = _recompile(session, actor, blocked)
    old_compilation = session.get(models.RubricCompilation, blocked.compilation_id)
    new_compilation = session.get(models.RubricCompilation, recovered.compilation_id)
    assert old_compilation.status == "superseded"
    assert old_compilation.blockers == old_blockers
    assert session.get(models.RubricVersion, old_version_id) is not None
    assert session.get(models.AtomicRule, old_rule_id) is not None
    assert new_compilation.status == "validated"
    assert new_compilation.validation_result["supersedes_compilation_id"] == blocked.compilation_id
    for event in (old_compilation.human_changes[-1], new_compilation.human_changes[-1]):
        assert set(event) == EVENT_FIELDS
        assert event["reason"] == "人工明确选择恢复策略。"

    active_rule = session.scalar(
        select(models.AtomicRule).where(
            models.AtomicRule.rubric_version_id == recovered.rubric_version_id
        )
    )
    # The predecessor has the same rule_code.  Lifecycle lookup must select
    # only the non-superseded graph rather than reporting ambiguous ownership.
    lifecycle.submit_atomic_rule_for_review(
        session,
        recovered.rubric_id,
        active_rule.rule_code,
        actor.id,
        "提交恢复后的规则。",
        now=max(active_rule.created_at, new_compilation.created_at)
        + timedelta(minutes=1),
    )
    assert active_rule.status == "review"
    assert session.get(models.AtomicRule, old_rule_id).status == "draft"


def test_wrong_predecessor_rolls_back_without_superseding_active_graph(recovery_db):
    session, actor, _reviewer = recovery_db
    blocked = _initial_blocked(session, actor)
    prepared = pipeline.prepare_manual_json_recompile(
        command=_command(
            mode="review_only",
            version="wrong-parent-v2",
            predecessor_id="00000000-0000-0000-0000-000000000499",
        )
    )
    with pytest.raises(ValueError, match="predecessor"):
        pipeline.persist_prepared_import(
            session=session,
            prepared=prepared,
            actor_id=actor.id,
            target_rubric_id=blocked.rubric_id,
        )
    assert session.get(models.RubricCompilation, blocked.compilation_id).status == "blocked"
    remaining = session.scalars(
        select(models.RubricCompilation).where(
            models.RubricCompilation.rubric_id == blocked.rubric_id
        )
    ).all()
    assert len(remaining) == 1


def test_rejected_rule_can_be_reopened_edited_and_reaudited(recovery_db):
    session, actor, reviewer = recovery_db
    blocked = _initial_blocked(session, actor)
    recovered = _recompile(session, actor, blocked)
    compilation = session.get(models.RubricCompilation, recovered.compilation_id)
    rule = session.scalar(
        select(models.AtomicRule).where(
            models.AtomicRule.rubric_version_id == recovered.rubric_version_id
        )
    )
    base = max(rule.created_at, compilation.created_at)
    lifecycle.submit_atomic_rule_for_review(
        session, recovered.rubric_id, rule.rule_code, actor.id, "提交", now=base + timedelta(minutes=1)
    )
    lifecycle.reject_atomic_rule(
        session, recovered.rubric_id, rule.rule_code, reviewer.id, "需修订", now=base + timedelta(minutes=2)
    )
    lifecycle.reopen_atomic_rule(
        session, recovered.rubric_id, rule.rule_code, actor.id, "按驳回意见修订", now=base + timedelta(minutes=3)
    )
    assert rule.status == "draft"
    assert rule.reviewed_by is None and rule.reviewed_at is None
    reject_event, reopen_event = compilation.human_changes[-2:]
    assert reject_event["action"] == "reject"
    assert reopen_event["action"] == "reopen"
    assert set(reopen_event) == EVENT_FIELDS
    lifecycle.edit_atomic_rule(
        session,
        recovered.rubric_id,
        rule.rule_code,
        {"rule_text": "已按审核意见补充复核要求。"},
        actor.id,
        "完成修订",
        now=base + timedelta(minutes=4),
    )
    assert rule.rule_text == "已按审核意见补充复核要求。"


@pytest.mark.parametrize(
    "changes",
    [
        {"direction": "arbitrary"},
        {"effect_type": "award_anything"},
        {"repeat_policy": "forever"},
        {"judge_type": "human"},
        {"strictness": "maybe"},
        {"max_points": -1},
        {"cap_points": "NaN"},
        {"checker_params": []},
        {"evidence_policy": {"bad": {1, 2}}},
        {"depends_on_rule_codes": "C01"},
        {"depends_on_rule_codes": ["same", "same"]},
        {"name": ""},
    ],
)
def test_invalid_rule_edit_values_fail_before_mutation_or_audit(recovery_db, changes):
    session, actor, _reviewer = recovery_db
    blocked = _initial_blocked(session, actor)
    recovered = _recompile(session, actor, blocked)
    compilation = session.get(models.RubricCompilation, recovered.compilation_id)
    rule = session.scalar(
        select(models.AtomicRule).where(
            models.AtomicRule.rubric_version_id == recovered.rubric_version_id
        )
    )
    before = {key: deepcopy(getattr(rule, key)) for key in changes}
    events = deepcopy(compilation.human_changes)
    with pytest.raises(lifecycle.RubricLifecycleError):
        lifecycle.edit_atomic_rule(
            session,
            recovered.rubric_id,
            rule.rule_code,
            changes,
            actor.id,
            "非法编辑必须原子拒绝",
        )
    assert {key: getattr(rule, key) for key in changes} == before
    assert compilation.human_changes == events


def test_rebuild_projection_uses_current_atomic_rule_not_raw_parse_output(recovery_db):
    session, actor, _reviewer = recovery_db
    blocked = _initial_blocked(session, actor)
    recovered = _recompile(session, actor, blocked, mode="deductive", points="2")
    rule = session.scalar(
        select(models.AtomicRule).where(
            models.AtomicRule.rubric_version_id == recovered.rubric_version_id
        )
    )
    criterion = session.get(models.RubricCriterion, rule.criterion_id)
    compilation = session.get(models.RubricCompilation, recovered.compilation_id)
    assert compilation.raw_parse_output["criteria"][0]["deduction_rules_structured"][0]["points"] == "2"
    lifecycle.edit_atomic_rule(
        session,
        recovered.rubric_id,
        rule.rule_code,
        {"max_points": "3"},
        actor.id,
        "调整已编译扣分值",
        now=max(rule.created_at, compilation.created_at) + timedelta(minutes=1),
    )
    criterion.deduction_rules_structured = [{"points": "2", "source": "stale"}]
    session.flush()
    pipeline.rebuild_legacy_projection(session=session, rubric_id=recovered.rubric_id)
    assert criterion.scoring_mode == "deductive"
    assert criterion.deduction_rules_structured[0]["points"] == "3"
    assert criterion.deduction_rules_structured[0]["source"] == "atomic_rule"


def test_clone_copies_only_signed_active_graph_not_superseded_history(recovery_db):
    session, actor, reviewer = recovery_db
    blocked = _initial_blocked(session, actor)
    recovered = _recompile(session, actor, blocked)
    compilation = session.get(models.RubricCompilation, recovered.compilation_id)
    rule = session.scalar(
        select(models.AtomicRule).where(
            models.AtomicRule.rubric_version_id == recovered.rubric_version_id
        )
    )
    base = max(rule.created_at, compilation.created_at)
    lifecycle.submit_atomic_rule_for_review(
        session, recovered.rubric_id, rule.rule_code, actor.id, "提交", now=base + timedelta(minutes=1)
    )
    lifecycle.approve_atomic_rule(
        session, recovered.rubric_id, rule.rule_code, reviewer.id, "批准", now=base + timedelta(minutes=2)
    )
    lifecycle.submit_for_review(session, recovered.rubric_id)
    lifecycle.publish_rubric(
        session,
        recovered.rubric_id,
        recovered.compilation_id,
        reviewer.id,
        now=base + timedelta(minutes=3),
    )
    session.commit()
    published_rubric = session.get(models.Rubric, recovered.rubric_id)
    assert (
        _resolve_cli_frozen_version(session, published_rubric).id
        == recovered.rubric_version_id
    )

    cloned = lifecycle.clone_published_rubric(
        session,
        recovered.rubric_id,
        "clone-v2",
        actor.id,
    )
    session.flush()
    clone_compilations = session.scalars(
        select(models.RubricCompilation).where(
            models.RubricCompilation.rubric_id == cloned.id
        )
    ).all()
    clone_versions = session.scalars(
        select(models.RubricVersion).where(models.RubricVersion.rubric_id == cloned.id)
    ).all()
    assert len(clone_compilations) == len(clone_versions) == 1
    clone_rules = session.scalars(
        select(models.AtomicRule).where(
            models.AtomicRule.rubric_version_id == clone_versions[0].id
        )
    ).all()
    assert [item.rule_code for item in clone_rules] == [rule.rule_code]
    clone_artifacts = session.scalars(
        select(models.SourceArtifact).where(
            models.SourceArtifact.compilation_id == clone_compilations[0].id
        )
    ).all()
    assert len(clone_artifacts) == 1


@pytest.fixture()
def recovery_api():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    enable_sqlite_foreign_keys(engine)
    models.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False)

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client, session_factory
    app.dependency_overrides.clear()
    engine.dispose()


def test_execution_draft_api_makes_recompile_identifiers_discoverable(recovery_api):
    client, session_factory = recovery_api
    created = client.post(
        "/api/rubrics",
        json={
            "name": "API recovery",
            "version": "v1",
            "total_score": 10,
            "criteria": [_criterion(mode="llm_direct")],
        },
    )
    assert created.status_code == 200, created.text
    rubric_id = created.json()["id"]
    draft = client.get(f"/api/rubrics/{rubric_id}/execution-draft")
    assert draft.status_code == 200, draft.text
    before = draft.json()
    predecessor_id = before["active_compilation"]["id"]
    assert before["active_compilation"]["blockers"]
    serialized = json.dumps(before, ensure_ascii=False)
    assert "raw_text" not in serialized
    assert "rule_text" not in serialized
    assert "raw_model_output" not in serialized

    recovered = client.post(
        f"/api/rubrics/{rubric_id}/recompile",
        json={
            "supersedes_compilation_id": predecessor_id,
            "version": "api-recovered-v2",
            "reason": "API 明确恢复",
            "criteria": [_criterion(mode="review_only")],
        },
    )
    assert recovered.status_code == 200, recovered.text
    after = client.get(f"/api/rubrics/{rubric_id}/execution-draft").json()
    assert after["active_compilation"]["id"] != predecessor_id
    assert after["active_compilation"]["status"] == "validated"
    rule_code = after["active_compilation"]["rules"][0]["rule_code"]
    submitted = client.post(
        f"/api/rubrics/{rubric_id}/rules/{rule_code}/submit-review",
        json={"reason": "API submit"},
    )
    assert submitted.status_code == 200, submitted.text
    rejected = client.post(
        f"/api/rubrics/{rubric_id}/rules/{rule_code}/reject",
        json={"reason": "API reject"},
    )
    assert rejected.status_code == 200, rejected.text
    reopened = client.post(
        f"/api/rubrics/{rubric_id}/rules/{rule_code}/reopen",
        json={"reason": "API revise"},
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "draft"
    assert {item["status"] for item in after["compilations"]} == {
        "superseded",
        "validated",
    }
    with session_factory() as session:
        predecessor = session.get(models.RubricCompilation, predecessor_id)
        assert predecessor.blockers
        assert predecessor.status == "superseded"
