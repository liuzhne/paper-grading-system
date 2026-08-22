"""P1-03 评分标准状态机、发布冻结和深克隆合同测试。

本文件只约束 draft/review/published 生命周期、签核原子性、发布版本图
不可变和 clone-for-edit。发布 blocker、映射审核 API、RBAC 与审计事件属于后续任务。
"""

import importlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import false
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.sqlite import enable_sqlite_foreign_keys
from backend.app.tests.test_atomic_rule_models import _make_p1_graph
from backend.app.tests.test_atomic_rule_models import _new_atomic
from backend.app.tests.test_atomic_rule_models import _new_template
from backend.app.tests.test_atomic_rule_models import _source_rules_collection


LIFECYCLE_MODULE = "backend.app.services.rubrics.lifecycle"
LIFECYCLE_FUNCTIONS = (
    "submit_for_review",
    "return_to_draft",
    "publish_rubric",
    "clone_published_rubric",
)
SOURCE_LINK_TABLE = models.Base.metadata.tables["atomic_rule_source_rules"]


def _lifecycle_contract():
    try:
        module = importlib.import_module(LIFECYCLE_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name and (LIFECYCLE_MODULE.startswith(exc.name) or exc.name.startswith(LIFECYCLE_MODULE)):
            pytest.fail(
                "P1-03 缺少生命周期服务 backend.app.services.rubrics.lifecycle",
                pytrace=False,
            )
        raise
    missing = [name for name in LIFECYCLE_FUNCTIONS if not callable(getattr(module, name, None))]
    assert not missing, f"P1-03 生命周期服务缺少函数：{', '.join(missing)}"
    return SimpleNamespace(
        **{name: getattr(module, name) for name in LIFECYCLE_FUNCTIONS},
        error_type=getattr(module, "RubricLifecycleError", ValueError),
    )


def _rejection_errors(lifecycle):
    if lifecycle.error_type is ValueError:
        return ValueError
    return (ValueError, lifecycle.error_type)


def _runtime_failure_errors(lifecycle):
    rejection_errors = _rejection_errors(lifecycle)
    if not isinstance(rejection_errors, tuple):
        rejection_errors = (rejection_errors,)
    return (*rejection_errors, RuntimeError)


@pytest.fixture()
def db():
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


def _make_full_graph(db, suffix):
    graph = _make_p1_graph(db, suffix)
    review_time = graph.compilation.created_at + timedelta(minutes=1)
    # 夹具自身保持总分与评分项分值守恒，避免后续发布预检接入后干扰 P1-03。
    graph.rubric.total_score = 4
    graph.criterion.max_score = 4
    graph.rubric.format_spec = {
        "body": {"font_name": "宋体", "font_size_pt": 12},
        "heading_1": {"bold": True, "alignment": "center"},
    }
    graph.criterion.evidence_hints = ["实验步骤", "数据来源"]
    graph.criterion.deduction_rules = ["缺少关键步骤扣 2 分"]
    graph.criterion.rubric_levels = [
        {"code": "GOOD", "points": 4, "descriptor": "步骤完整"},
        {"code": "WEAK", "points": 2, "descriptor": "步骤有缺失"},
    ]
    graph.criterion.sub_checks = [{"code": "METHOD-STEPS", "max_points": 4}]
    graph.criterion.deduction_rules_structured = [
        {"match": {"missing": "验证"}, "points": 2, "reason": "缺少验证步骤"}
    ]
    graph.compilation.sampling_params = {"temperature": 0, "seed": 7}
    graph.compilation.raw_parse_output = {"sections": ["第三章 研究方法"]}
    graph.compilation.raw_model_output = {"suggestions": [{"code": "METHOD-01-D"}]}
    graph.compilation.validation_result = {"valid": True}
    graph.compilation.warnings = [{"code": "REVIEWED_WARNING"}]
    graph.compilation.human_changes = [{"path": "/rules/METHOD-01-D", "reason": "人工确认"}]
    graph.version.global_policy = {
        "rounding": {"mode": "half_up", "digits": 1},
        "grade_boundaries": [{"code": "PASS", "min": 60}],
    }

    template = _new_template(
        models.TemplateItem,
        graph,
        item_code="TPL-METHOD-PRIMARY",
    )
    deterministic = _new_atomic(
        models.AtomicRule,
        graph,
        rule_code="METHOD-01-D",
        status="review",
        reviewed_by=graph.user.id,
        reviewed_at=review_time,
    )
    semantic = _new_atomic(
        models.AtomicRule,
        graph,
        rule_code="METHOD-01-S",
        name="研究过程连贯性",
        rule_text="研究步骤的输入、输出和选择理由应连贯。",
        direction="band",
        max_points=4,
        repeat_policy=None,
        cap_points=None,
        judge_type="semantic",
        checker_key=None,
        checker_params={},
        mutex_group=None,
        status="review",
        reviewed_by=graph.user.id,
        reviewed_at=review_time + timedelta(minutes=1),
    )
    db.add_all([template, deterministic, semantic])
    db.flush()

    levels = [
        models.RuleLevel(
            atomic_rule_id=semantic.id,
            level_code="L0",
            points=0,
            descriptor="过程断裂。",
            positive_example=None,
            negative_example="缺少输入输出说明。",
            display_order=0,
        ),
        models.RuleLevel(
            atomic_rule_id=semantic.id,
            level_code="L1",
            points=4,
            descriptor="过程完整连贯。",
            positive_example="步骤和选择依据完整。",
            negative_example=None,
            display_order=1,
        ),
    ]
    link = models.RuleTemplateLink(
        rule_id=deterministic.id,
        template_item_id=template.id,
        relationship_type="constraint",
        match_method="manual",
        match_confidence=1,
        rationale="审核者确认模板约束直接支持此规则。",
        review_status="confirmed",
        reviewed_by=graph.user.id,
        reviewed_at=review_time + timedelta(minutes=2),
    )
    db.add_all([*levels, link])
    _source_rules_collection(deterministic).append(graph.source_rule_1)
    _source_rules_collection(semantic).append(graph.source_rule_2)
    db.commit()
    return SimpleNamespace(
        **vars(graph),
        template=template,
        deterministic=deterministic,
        semantic=semantic,
        levels=levels,
        link=link,
    )


def _ids_filter(column, ids):
    return column.in_(ids) if ids else false()


def _graph_entities(db, rubric_id):
    rubric = db.get(models.Rubric, rubric_id)
    criteria = db.scalars(
        select(models.RubricCriterion)
        .where(models.RubricCriterion.rubric_id == rubric_id)
        .order_by(models.RubricCriterion.code)
    ).all()
    compilations = db.scalars(
        select(models.RubricCompilation)
        .where(models.RubricCompilation.rubric_id == rubric_id)
        .order_by(models.RubricCompilation.created_at)
    ).all()
    compilation_ids = [item.id for item in compilations]
    artifacts = db.scalars(
        select(models.SourceArtifact)
        .where(_ids_filter(models.SourceArtifact.compilation_id, compilation_ids))
        .order_by(models.SourceArtifact.artifact_type, models.SourceArtifact.file_name)
    ).all()
    artifact_ids = [item.id for item in artifacts]
    source_rules = db.scalars(
        select(models.SourceRule)
        .where(_ids_filter(models.SourceRule.source_artifact_id, artifact_ids))
        .order_by(models.SourceRule.source_rule_code)
    ).all()
    templates = db.scalars(
        select(models.TemplateItem)
        .where(_ids_filter(models.TemplateItem.source_artifact_id, artifact_ids))
        .order_by(models.TemplateItem.item_code)
    ).all()
    versions = db.scalars(
        select(models.RubricVersion)
        .where(_ids_filter(models.RubricVersion.compilation_id, compilation_ids))
        .order_by(models.RubricVersion.version)
    ).all()
    version_ids = [item.id for item in versions]
    rules = db.scalars(
        select(models.AtomicRule)
        .where(_ids_filter(models.AtomicRule.rubric_version_id, version_ids))
        .order_by(models.AtomicRule.rule_code)
    ).all()
    rule_ids = [item.id for item in rules]
    levels = db.scalars(
        select(models.RuleLevel)
        .where(_ids_filter(models.RuleLevel.atomic_rule_id, rule_ids))
        .order_by(models.RuleLevel.atomic_rule_id, models.RuleLevel.display_order)
    ).all()
    links = db.scalars(
        select(models.RuleTemplateLink)
        .where(_ids_filter(models.RuleTemplateLink.rule_id, rule_ids))
        .order_by(models.RuleTemplateLink.rule_id, models.RuleTemplateLink.template_item_id)
    ).all()
    source_links = db.execute(
        select(
            SOURCE_LINK_TABLE.c.atomic_rule_id,
            SOURCE_LINK_TABLE.c.source_rule_id,
        ).where(_ids_filter(SOURCE_LINK_TABLE.c.atomic_rule_id, rule_ids))
    ).all()
    return SimpleNamespace(
        rubric=rubric,
        criteria=criteria,
        compilations=compilations,
        artifacts=artifacts,
        source_rules=source_rules,
        templates=templates,
        versions=versions,
        rules=rules,
        levels=levels,
        links=links,
        source_links=source_links,
    )


def _freeze(value):
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _row_snapshot(instance):
    return tuple(
        (column.name, _freeze(getattr(instance, column.name)))
        for column in instance.__table__.columns
    )


def _graph_snapshot(db, rubric_id):
    graph = _graph_entities(db, rubric_id)
    return {
        "rubric": _row_snapshot(graph.rubric),
        "criteria": tuple(sorted((_row_snapshot(item) for item in graph.criteria), key=repr)),
        "compilations": tuple(sorted((_row_snapshot(item) for item in graph.compilations), key=repr)),
        "artifacts": tuple(sorted((_row_snapshot(item) for item in graph.artifacts), key=repr)),
        "source_rules": tuple(sorted((_row_snapshot(item) for item in graph.source_rules), key=repr)),
        "templates": tuple(sorted((_row_snapshot(item) for item in graph.templates), key=repr)),
        "versions": tuple(sorted((_row_snapshot(item) for item in graph.versions), key=repr)),
        "rules": tuple(sorted((_row_snapshot(item) for item in graph.rules), key=repr)),
        "levels": tuple(sorted((_row_snapshot(item) for item in graph.levels), key=repr)),
        "links": tuple(sorted((_row_snapshot(item) for item in graph.links), key=repr)),
        "source_links": tuple(sorted((row.atomic_rule_id, row.source_rule_id) for row in graph.source_links)),
    }


def _graph_id_sets(db, rubric_id):
    graph = _graph_entities(db, rubric_id)
    return {
        "rubrics": {graph.rubric.id},
        "criteria": {item.id for item in graph.criteria},
        "compilations": {item.id for item in graph.compilations},
        "artifacts": {item.id for item in graph.artifacts},
        "source_rules": {item.id for item in graph.source_rules},
        "templates": {item.id for item in graph.templates},
        "versions": {item.id for item in graph.versions},
        "rules": {item.id for item in graph.rules},
        "levels": {item.id for item in graph.levels},
        "links": {item.id for item in graph.links},
    }


def _column_content(instance, excluded):
    return tuple(
        (column.name, _freeze(getattr(instance, column.name)))
        for column in instance.__table__.columns
        if column.name not in excluded
    )


def _normalized_graph_content(db, rubric_id):
    graph = _graph_entities(db, rubric_id)
    criterion_code = {item.id: item.code for item in graph.criteria}
    artifact_key = {
        item.id: (item.artifact_type, item.file_name, item.file_hash)
        for item in graph.artifacts
    }
    source_key = {
        item.id: (artifact_key[item.source_artifact_id], item.source_rule_code, item.row_number)
        for item in graph.source_rules
    }
    template_key = {
        item.id: (artifact_key[item.source_artifact_id], item.item_code)
        for item in graph.templates
    }
    rule_code = {item.id: item.rule_code for item in graph.rules}
    rubric_content = _column_content(
        graph.rubric,
        {
            "id",
            "version",
            "status",
            "owner_id",
            "created_by",
            "created_at",
            "published_at",
            "published_by",
            "archived_by",
        },
    )
    compilation_content = tuple(
        sorted(
            (
                _column_content(
                    item,
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
                )
                for item in graph.compilations
            ),
            key=repr,
        )
    )
    source_rule_content = tuple(
        sorted(
            (
                artifact_key[item.source_artifact_id],
                _column_content(item, {"id", "source_artifact_id", "created_at"}),
            )
            for item in graph.source_rules
        )
    )
    template_content = tuple(
        sorted(
            (
                artifact_key[item.source_artifact_id],
                _column_content(item, {"id", "source_artifact_id", "created_at"}),
            )
            for item in graph.templates
        )
    )
    rule_content = tuple(
        sorted(
            (
                criterion_code[item.criterion_id],
                _column_content(
                    item,
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
            )
            for item in graph.rules
        )
    )
    level_content = tuple(
        sorted(
            (
                rule_code[item.atomic_rule_id],
                _column_content(item, {"id", "atomic_rule_id", "created_at"}),
            )
            for item in graph.levels
        )
    )
    link_content = tuple(
        sorted(
            (
                rule_code[item.rule_id],
                template_key[item.template_item_id],
                _column_content(
                    item,
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
            )
            for item in graph.links
        )
    )
    association_content = tuple(
        sorted(
            (
                rule_code[row.atomic_rule_id],
                source_key.get(row.source_rule_id, ("EXTERNAL_SOURCE_RULE", row.source_rule_id)),
            )
            for row in graph.source_links
        )
    )
    return {
        "rubric": rubric_content,
        "criteria": tuple(
            sorted(
                (
                    _column_content(item, {"id", "rubric_id", "created_at"})
                    for item in graph.criteria
                ),
                key=repr,
            )
        ),
        "compilations": compilation_content,
        "artifacts": tuple(
            sorted(
                (
                    _column_content(item, {"id", "compilation_id", "created_at"})
                    for item in graph.artifacts
                ),
                key=repr,
            )
        ),
        "source_rules": source_rule_content,
        "templates": template_content,
        "versions": tuple(
            sorted(
                (
                    _column_content(
                        item,
                        {
                            "id",
                            "rubric_id",
                            "compilation_id",
                            "version",
                            "version_hash",
                            "created_by",
                            "created_at",
                        },
                    )
                    for item in graph.versions
                ),
                key=repr,
            )
        ),
        "rules": rule_content,
        "levels": level_content,
        "links": link_content,
        "source_links": association_content,
    }


def _database_counts(db):
    tables = (
        models.Rubric.__table__,
        models.RubricCriterion.__table__,
        models.RubricCompilation.__table__,
        models.SourceArtifact.__table__,
        models.SourceRule.__table__,
        models.TemplateItem.__table__,
        models.RubricVersion.__table__,
        models.AtomicRule.__table__,
        models.RuleLevel.__table__,
        models.RuleTemplateLink.__table__,
        SOURCE_LINK_TABLE,
    )
    return {
        table.name: db.scalar(select(func.count()).select_from(table))
        for table in tables
    }


def _time_after_created(graph, minutes=5):
    return graph.compilation.created_at + timedelta(minutes=minutes)


def _publish_graph(db, lifecycle, graph, *, reviewer_id=None, now=None):
    now = now or _time_after_created(graph)
    lifecycle.submit_for_review(db, graph.rubric.id)
    db.commit()
    version = lifecycle.publish_rubric(
        db,
        graph.rubric.id,
        graph.compilation.id,
        reviewer_id or graph.user.id,
        now=now,
    )
    db.commit()
    return version


def _move_to_state(db, lifecycle, graph, state):
    if state == "draft":
        return
    lifecycle.submit_for_review(db, graph.rubric.id)
    db.commit()
    if state == "published":
        lifecycle.publish_rubric(
            db,
            graph.rubric.id,
            graph.compilation.id,
            graph.user.id,
            now=_time_after_created(graph),
        )
        db.commit()


def _add_criterion(db, graph):
    db.add(
        models.RubricCriterion(
            rubric_id=graph.rubric.id,
            code="P103-NEW",
            name="发布后新增评分项",
            max_score=1,
            display_order=99,
        )
    )


def _delete_criterion(db, graph):
    db.delete(graph.criterion)


def _add_artifact(db, graph):
    db.add(
        models.SourceArtifact(
            compilation_id=graph.compilation.id,
            artifact_type="docx",
            file_name="发布后新增.docx",
            file_hash="f" * 64,
            file_size_bytes=128,
            uploaded_by=graph.user.id,
        )
    )


def _delete_artifact(db, graph):
    db.delete(graph.docx)


def _add_source_rule(db, graph):
    db.add(
        models.SourceRule(
            source_artifact_id=graph.excel.id,
            source_rule_code="METHOD-PUBLISHED-NEW",
            sheet_name="原始规则",
            row_number=99,
            cell_locator="原始规则!A99:N99",
            raw_text="发布后新增来源规则。",
        )
    )


def _delete_source_rule(db, graph):
    db.delete(graph.source_rule_1)


def _add_template_item(db, graph):
    db.add(
        models.TemplateItem(
            source_artifact_id=graph.docx.id,
            item_code="TPL-PUBLISHED-NEW",
            kind="content",
            section_path=["第三章"],
            raw_text="发布后新增模板要求。",
            normalized_constraint={},
            strictness="preferred",
            source_locator={"paragraph_id": "w:p-new"},
            source_hash="1" * 64,
            parse_confidence=1,
        )
    )


def _delete_template_item(db, graph):
    db.delete(graph.template)


def _add_atomic_rule(db, graph):
    db.add(
        _new_atomic(
            models.AtomicRule,
            graph,
            rule_code="METHOD-PUBLISHED-NEW",
            status="draft",
            reviewed_by=None,
            reviewed_at=None,
        )
    )


def _delete_atomic_rule(db, graph):
    db.delete(graph.deterministic)


def _add_rule_level(db, graph):
    db.add(
        models.RuleLevel(
            atomic_rule_id=graph.semantic.id,
            level_code="L-NEW",
            points=2,
            descriptor="发布后新增档位。",
            display_order=2,
        )
    )


def _delete_rule_level(db, graph):
    db.delete(graph.levels[0])


def _add_template_link(db, graph):
    db.add(
        models.RuleTemplateLink(
            rule_id=graph.semantic.id,
            template_item_id=graph.template.id,
            relationship_type="support",
            match_method="manual",
            match_confidence=1,
            rationale="发布后新增映射。",
            review_status="pending",
        )
    )


def _delete_template_link(db, graph):
    db.delete(graph.link)


def _append_source_mapping(_db, graph):
    _source_rules_collection(graph.deterministic).append(graph.source_rule_2)


def _remove_source_mapping(_db, graph):
    _source_rules_collection(graph.deterministic).remove(graph.source_rule_1)


def _delete_version(db, graph):
    db.delete(graph.version)


def _add_version(db, graph):
    db.add(
        models.RubricVersion(
            rubric_id=graph.rubric.id,
            compilation_id=graph.compilation.id,
            version="9.9.9",
            workflow_profile=graph.version.workflow_profile,
            global_policy={},
            version_hash=graph.compilation.final_version_hash,
            created_by=graph.user.id,
        )
    )


def _add_compilation(db, graph):
    db.add(
        models.RubricCompilation(
            rubric_id=graph.rubric.id,
            status="created",
            parser_version="p103-after-publish",
            compiler_version="p103-after-publish",
            prompt_version="p103-after-publish",
            created_by=graph.user.id,
        )
    )


def _delete_compilation(db, graph):
    db.delete(graph.compilation)


def _delete_rubric(db, graph):
    db.delete(graph.rubric)


def test_draft_can_enter_review_and_return_to_draft_without_signoff(db):
    lifecycle = _lifecycle_contract()
    graph = _make_full_graph(db, "review-cycle")
    original_hash = graph.compilation.final_version_hash

    submitted = lifecycle.submit_for_review(db, graph.rubric.id)
    db.commit()
    assert submitted.id == graph.rubric.id
    assert graph.rubric.status == "review"
    assert graph.compilation.status == "validated"
    assert graph.compilation.reviewed_by is None
    assert graph.compilation.reviewed_at is None
    assert graph.compilation.published_at is None
    assert graph.compilation.final_version_hash == original_hash
    assert graph.rubric.published_at is None

    returned = lifecycle.return_to_draft(db, graph.rubric.id)
    db.commit()
    assert returned.id == graph.rubric.id
    assert graph.rubric.status == "draft"
    assert graph.compilation.status == "validated"
    assert graph.compilation.reviewed_by is None
    assert graph.compilation.reviewed_at is None
    assert graph.compilation.published_at is None
    assert graph.rubric.published_at is None


def test_review_can_publish_once_with_atomic_signoff_metadata(db):
    lifecycle = _lifecycle_contract()
    graph = _make_full_graph(db, "publish")
    lifecycle.submit_for_review(db, graph.rubric.id)
    db.commit()

    published_at = _time_after_created(graph)
    published_version = lifecycle.publish_rubric(
        db,
        graph.rubric.id,
        graph.compilation.id,
        graph.user.id,
        now=published_at,
    )
    db.commit()
    db.expire_all()

    rubric = db.get(models.Rubric, graph.rubric.id)
    compilation = db.get(models.RubricCompilation, graph.compilation.id)
    version = db.get(models.RubricVersion, published_version.id)
    assert rubric.status == "published"
    assert rubric.published_at == published_at
    assert compilation.status == "validated"
    assert compilation.reviewed_by == graph.user.id
    assert compilation.reviewed_at == published_at
    assert compilation.published_at == published_at
    assert compilation.reviewed_at >= compilation.created_at
    assert compilation.final_version_hash
    assert version.rubric_id == rubric.id
    assert version.compilation_id == compilation.id
    assert version.version_hash == compilation.final_version_hash
    assert db.query(models.RubricVersion).filter_by(compilation_id=compilation.id).count() == 1


@pytest.mark.parametrize(
    ("initial_state", "operation"),
    [
        ("draft", "publish"),
        ("published", "submit_for_review"),
        ("published", "return_to_draft"),
    ],
)
def test_service_rejects_illegal_transitions_without_partial_changes(db, initial_state, operation):
    lifecycle = _lifecycle_contract()
    graph = _make_full_graph(db, f"illegal-{initial_state}-{operation}")
    _move_to_state(db, lifecycle, graph, initial_state)
    rubric_id = graph.rubric.id
    before = _graph_snapshot(db, rubric_id)

    with pytest.raises(_rejection_errors(lifecycle)):
        if operation == "publish":
            lifecycle.publish_rubric(
                db,
                graph.rubric.id,
                graph.compilation.id,
                graph.user.id,
                now=_time_after_created(graph, minutes=6),
            )
        else:
            getattr(lifecycle, operation)(db, graph.rubric.id)
        db.commit()
    db.rollback()
    db.expire_all()

    assert _graph_snapshot(db, rubric_id) == before


def test_publish_rejects_compilation_from_another_rubric_atomically(db):
    lifecycle = _lifecycle_contract()
    target = _make_full_graph(db, "cross-compilation-target")
    other = _make_full_graph(db, "cross-compilation-other")
    lifecycle.submit_for_review(db, target.rubric.id)
    db.commit()
    before_target = _graph_snapshot(db, target.rubric.id)
    before_other = _graph_snapshot(db, other.rubric.id)

    with pytest.raises(_rejection_errors(lifecycle)):
        lifecycle.publish_rubric(
            db,
            target.rubric.id,
            other.compilation.id,
            target.user.id,
            now=_time_after_created(target),
        )
        db.commit()
    db.rollback()
    db.expire_all()

    assert _graph_snapshot(db, target.rubric.id) == before_target
    assert _graph_snapshot(db, other.rubric.id) == before_other


@pytest.mark.parametrize("reviewer_id", [None, "missing-reviewer-id"])
def test_publish_requires_an_existing_reviewer_and_leaves_no_partial_signoff(db, reviewer_id):
    lifecycle = _lifecycle_contract()
    graph = _make_full_graph(db, f"invalid-reviewer-{reviewer_id}")
    lifecycle.submit_for_review(db, graph.rubric.id)
    db.commit()
    before = _graph_snapshot(db, graph.rubric.id)

    with pytest.raises(_rejection_errors(lifecycle)):
        lifecycle.publish_rubric(
            db,
            graph.rubric.id,
            graph.compilation.id,
            reviewer_id,
            now=_time_after_created(graph),
        )
        db.commit()
    db.rollback()
    db.expire_all()

    assert _graph_snapshot(db, graph.rubric.id) == before


@pytest.mark.parametrize(
    ("initial_state", "target_status"),
    [
        ("draft", "published"),
        ("draft", "archived"),
        ("draft", "__invalid__"),
        ("review", "published"),
        ("published", "draft"),
        ("published", "review"),
    ],
)
def test_direct_status_assignment_cannot_bypass_state_machine(db, initial_state, target_status):
    lifecycle = _lifecycle_contract()
    graph = _make_full_graph(db, f"direct-{initial_state}-{target_status}")
    _move_to_state(db, lifecycle, graph, initial_state)
    rubric_id = graph.rubric.id
    before = _graph_snapshot(db, rubric_id)

    with pytest.raises(_rejection_errors(lifecycle)):
        graph.rubric.status = target_status
        db.commit()
    db.rollback()
    db.expire_all()

    assert _graph_snapshot(db, rubric_id) == before


def test_republishing_preserves_original_signoff_and_version(db):
    lifecycle = _lifecycle_contract()
    graph = _make_full_graph(db, "republish")
    second_reviewer = models.User(
        username="p103-second-reviewer",
        display_name="第二审核人",
        role="reviewer",
    )
    db.add(second_reviewer)
    db.commit()
    first_publish_time = _time_after_created(graph)
    _publish_graph(db, lifecycle, graph, now=first_publish_time)
    rubric_id = graph.rubric.id
    before = _graph_snapshot(db, rubric_id)

    with pytest.raises(_rejection_errors(lifecycle)):
        lifecycle.publish_rubric(
            db,
            graph.rubric.id,
            graph.compilation.id,
            second_reviewer.id,
            now=_time_after_created(graph, minutes=6),
        )
        db.commit()
    db.rollback()
    db.expire_all()

    assert _graph_snapshot(db, rubric_id) == before
    compilation = db.get(models.RubricCompilation, graph.compilation.id)
    assert compilation.reviewed_by == graph.user.id
    assert compilation.reviewed_at == first_publish_time
    assert db.query(models.RubricVersion).filter_by(compilation_id=compilation.id).count() == 1


def test_publish_cannot_smuggle_content_changes_inside_authorized_transition(db):
    lifecycle = _lifecycle_contract()
    graph = _make_full_graph(db, "publish-smuggled-edit")
    lifecycle.submit_for_review(db, graph.rubric.id)
    db.commit()
    rubric_id = graph.rubric.id
    before = _graph_snapshot(db, rubric_id)

    graph.deterministic.rule_text = "企图随发布一起写入的规则改动"
    with pytest.raises(_rejection_errors(lifecycle)):
        lifecycle.publish_rubric(
            db,
            graph.rubric.id,
            graph.compilation.id,
            graph.user.id,
            now=_time_after_created(graph),
        )
        db.commit()
    db.rollback()
    db.expire_all()

    assert _graph_snapshot(db, rubric_id) == before


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda _db, g: setattr(g.rubric, "description", "发布后修改"), id="rubric-scalar"),
        pytest.param(
            lambda _db, g: g.rubric.format_spec.update({"body": {"font_name": "黑体"}}),
            id="rubric-json",
        ),
        pytest.param(lambda _db, g: setattr(g.criterion, "name", "发布后改名"), id="criterion-scalar"),
        pytest.param(
            lambda _db, g: g.criterion.evidence_hints.append("发布后证据"),
            id="criterion-json",
        ),
        pytest.param(
            lambda _db, g: setattr(g.compilation, "compiler_version", "changed"),
            id="compilation-scalar",
        ),
        pytest.param(
            lambda _db, g: g.compilation.warnings.append({"code": "AFTER_PUBLISH"}),
            id="compilation-json",
        ),
        pytest.param(lambda _db, g: setattr(g.excel, "file_name", "改名.xlsx"), id="artifact"),
        pytest.param(lambda _db, g: setattr(g.source_rule_1, "raw_text", "改写来源"), id="source-rule"),
        pytest.param(lambda _db, g: setattr(g.template, "raw_text", "改写模板"), id="template"),
        pytest.param(
            lambda _db, g: g.template.source_locator.update({"paragraph_id": "changed"}),
            id="template-json",
        ),
        pytest.param(
            lambda _db, g: g.version.global_policy.update({"rounding": "changed"}),
            id="version-json",
        ),
        pytest.param(lambda _db, g: setattr(g.deterministic, "rule_text", "改写规则"), id="atomic-rule"),
        pytest.param(
            lambda _db, g: g.deterministic.checker_params.update({"changed": True}),
            id="atomic-rule-json",
        ),
        pytest.param(lambda _db, g: setattr(g.levels[0], "points", 1), id="rule-level"),
        pytest.param(lambda _db, g: setattr(g.link, "rationale", "改写映射理由"), id="template-link"),
    ],
)
def test_published_snapshot_rejects_scalar_and_json_mutations(db, mutation):
    lifecycle = _lifecycle_contract()
    graph = _make_full_graph(db, f"immutable-value-{mutation.__name__}")
    _publish_graph(db, lifecycle, graph)
    rubric_id = graph.rubric.id
    before = _graph_snapshot(db, rubric_id)

    with pytest.raises(_rejection_errors(lifecycle)):
        mutation(db, graph)
        db.commit()
    db.rollback()
    db.expire_all()

    assert _graph_snapshot(db, rubric_id) == before


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(_add_criterion, id="add-criterion"),
        pytest.param(_delete_criterion, id="delete-criterion"),
        pytest.param(_add_compilation, id="add-compilation"),
        pytest.param(_add_artifact, id="add-artifact"),
        pytest.param(_delete_artifact, id="delete-artifact"),
        pytest.param(_add_source_rule, id="add-source-rule"),
        pytest.param(_delete_source_rule, id="delete-source-rule"),
        pytest.param(_add_template_item, id="add-template"),
        pytest.param(_delete_template_item, id="delete-template"),
        pytest.param(_add_version, id="add-version"),
        pytest.param(_add_atomic_rule, id="add-atomic-rule"),
        pytest.param(_delete_atomic_rule, id="delete-atomic-rule"),
        pytest.param(_add_rule_level, id="add-rule-level"),
        pytest.param(_delete_rule_level, id="delete-rule-level"),
        pytest.param(_add_template_link, id="add-template-link"),
        pytest.param(_delete_template_link, id="delete-template-link"),
        pytest.param(_append_source_mapping, id="append-source-mapping"),
        pytest.param(_remove_source_mapping, id="remove-source-mapping"),
        pytest.param(_delete_version, id="delete-version"),
        pytest.param(_delete_compilation, id="delete-compilation"),
        pytest.param(_delete_rubric, id="delete-rubric"),
    ],
)
def test_published_snapshot_rejects_row_and_relationship_changes(db, mutation):
    lifecycle = _lifecycle_contract()
    graph = _make_full_graph(db, f"immutable-graph-{mutation.__name__}")
    _publish_graph(db, lifecycle, graph)
    rubric_id = graph.rubric.id
    before = _graph_snapshot(db, rubric_id)

    with pytest.raises(_rejection_errors(lifecycle)):
        mutation(db, graph)
        db.commit()
    db.rollback()
    db.expire_all()

    assert _graph_snapshot(db, rubric_id) == before


@pytest.mark.parametrize("entity_kind", ["criterion", "compilation"])
def test_loaded_old_parent_cannot_hide_reparenting_into_published_graph(db, entity_kind):
    lifecycle = _lifecycle_contract()
    target = _make_full_graph(db, f"reparent-target-{entity_kind}")
    _publish_graph(db, lifecycle, target)
    source = models.Rubric(
        name=f"P1-03 重挂来源 {entity_kind}",
        version="draft",
        total_score=1,
        status="draft",
        created_by=target.user.id,
    )
    db.add(source)
    db.flush()
    if entity_kind == "criterion":
        entity = models.RubricCriterion(
            rubric_id=source.id,
            code="REPARENT",
            name="待重挂评分项",
            max_score=1,
        )
    else:
        entity = models.RubricCompilation(
            rubric_id=source.id,
            status="created",
            parser_version="p103-reparent",
            compiler_version="p103-reparent",
            prompt_version="p103-reparent",
            created_by=target.user.id,
        )
    db.add(entity)
    db.commit()
    assert entity.rubric.id == source.id  # 刻意把旧 relationship 留在 identity map 中。
    target_before = _graph_snapshot(db, target.rubric.id)
    source_before = _graph_snapshot(db, source.id)

    with pytest.raises(_rejection_errors(lifecycle)):
        entity.rubric_id = target.rubric.id
        db.commit()
    db.rollback()
    db.expire_all()

    assert _graph_snapshot(db, target.rubric.id) == target_before
    assert _graph_snapshot(db, source.id) == source_before


def test_published_snapshot_allows_runtime_records_that_reference_it(db):
    lifecycle = _lifecycle_contract()
    graph = _make_full_graph(db, "runtime-consumers")
    _publish_graph(db, lifecycle, graph)
    before = _graph_snapshot(db, graph.rubric.id)

    batch = models.GradingBatch(
        name="P1-03 发布后运行批次",
        rubric_id=graph.rubric.id,
        created_by=graph.user.id,
    )
    paper = models.Paper(
        batch=batch,
        student_id="P103-001",
        file_name="p103.docx",
        file_path="/tmp/p103.docx",
    )
    scoring_run = models.ScoringRun(
        paper=paper,
        rubric_id=graph.rubric.id,
        status="completed",
    )
    score_item = models.ScoreItem(
        scoring_run=scoring_run,
        criterion=graph.criterion,
        max_score=graph.criterion.max_score,
        ai_score=graph.criterion.max_score,
        evidence_sufficient=True,
        reason="运行时评分结果只引用已发布标准。",
    )
    anchor = models.CalibrationAnchor(
        rubric_id=graph.rubric.id,
        criterion_code=graph.criterion.code,
        score=graph.criterion.max_score,
        max_score=graph.criterion.max_score,
        excerpt="脱敏校准样例",
    )
    db.add_all([batch, paper, scoring_run, score_item, anchor])
    db.commit()
    db.expire_all()

    assert db.get(models.ScoreItem, score_item.id) is not None
    assert db.get(models.CalibrationAnchor, anchor.id) is not None
    assert _graph_snapshot(db, graph.rubric.id) == before


def test_clone_published_rubric_creates_fresh_editable_draft_graph(db):
    lifecycle = _lifecycle_contract()
    source = _make_full_graph(db, "clone-editable")
    clone_actor = models.User(
        username="p103-clone-actor",
        display_name="克隆操作者",
        role="reviewer",
    )
    db.add(clone_actor)
    db.commit()
    _publish_graph(db, lifecycle, source)
    source_id = source.rubric.id
    source_before = _graph_snapshot(db, source_id)
    source_ids = _graph_id_sets(db, source_id)

    cloned = lifecycle.clone_published_rubric(
        db,
        source.rubric.id,
        "2.0.0",
        clone_actor.id,
        name="克隆后的评分标准",
        description="用于下一轮修订",
    )
    db.commit()
    db.expire_all()

    cloned = db.get(models.Rubric, cloned.id)
    clone_graph = _graph_entities(db, cloned.id)
    clone_ids = _graph_id_sets(db, cloned.id)
    assert cloned.id != source_id
    assert cloned.name == "克隆后的评分标准"
    assert cloned.version == "2.0.0"
    assert cloned.description == "用于下一轮修订"
    assert cloned.status == "draft"
    assert cloned.published_at is None
    assert cloned.created_by == clone_actor.id
    assert cloned.owner_id == clone_actor.id
    assert len(clone_graph.compilations) == 1
    assert clone_graph.compilations[0].status == "validated"
    assert clone_graph.compilations[0].created_by == clone_actor.id
    assert clone_graph.compilations[0].reviewed_by is None
    assert clone_graph.compilations[0].reviewed_at is None
    assert clone_graph.compilations[0].published_at is None
    assert clone_graph.compilations[0].human_changes == []
    assert len(clone_graph.versions) == 1
    assert clone_graph.versions[0].version == "2.0.0"
    assert all(rule.status == "draft" for rule in clone_graph.rules)
    assert all(rule.reviewed_by is None and rule.reviewed_at is None for rule in clone_graph.rules)
    assert all(link.review_status == "pending" for link in clone_graph.links)
    assert all(link.reviewed_by is None and link.reviewed_at is None for link in clone_graph.links)
    for table_name in source_ids:
        assert source_ids[table_name].isdisjoint(clone_ids[table_name])

    cloned.description = "草稿仍可编辑"
    clone_graph.rules[0].checker_params["clone_only"] = True
    clone_graph.templates[0].section_path.append("克隆章节")
    db.commit()
    db.expire_all()

    assert _graph_snapshot(db, source_id) == source_before
    edited_clone = _graph_entities(db, cloned.id)
    assert edited_clone.rubric.description == "草稿仍可编辑"
    assert edited_clone.rules[0].checker_params["clone_only"] is True
    assert edited_clone.templates[0].section_path[-1] == "克隆章节"


def test_clone_deep_copies_all_content_and_rebuilds_internal_mappings(db):
    lifecycle = _lifecycle_contract()
    source = _make_full_graph(db, "clone-content")
    clone_actor = models.User(
        username="p103-content-clone-actor",
        display_name="内容克隆操作者",
        role="reviewer",
    )
    db.add(clone_actor)
    db.commit()
    _publish_graph(db, lifecycle, source)
    source_content = _normalized_graph_content(db, source.rubric.id)
    source_ids = _graph_id_sets(db, source.rubric.id)

    cloned = lifecycle.clone_published_rubric(
        db,
        source.rubric.id,
        "2.1.0",
        clone_actor.id,
    )
    db.commit()
    db.expire_all()

    clone_content = _normalized_graph_content(db, cloned.id)
    clone_ids = _graph_id_sets(db, cloned.id)
    assert clone_content == source_content
    for table_name in source_ids:
        assert source_ids[table_name].isdisjoint(clone_ids[table_name])

    source_graph = _graph_entities(db, source.rubric.id)
    clone_graph = _graph_entities(db, cloned.id)
    assert source_graph.versions[0].global_policy == clone_graph.versions[0].global_policy
    assert source_graph.versions[0].global_policy is not clone_graph.versions[0].global_policy
    assert source_graph.templates[0].source_locator == clone_graph.templates[0].source_locator
    assert source_graph.templates[0].source_locator is not clone_graph.templates[0].source_locator
    assert len(source_graph.source_links) == len(clone_graph.source_links)
    assert {
        row.source_rule_id for row in clone_graph.source_links
    }.issubset({item.id for item in clone_graph.source_rules})
    assert {
        item.uploaded_by for item in clone_graph.artifacts
    } == {
        item.uploaded_by for item in source_graph.artifacts
    }


def test_editing_a_clone_recomputes_content_hash_before_republication(db):
    lifecycle = _lifecycle_contract()
    source = _make_full_graph(db, "clone-rehash")
    _publish_graph(db, lifecycle, source)
    source_before = _graph_snapshot(db, source.rubric.id)
    source_graph = _graph_entities(db, source.rubric.id)
    source_hash = source_graph.versions[0].version_hash

    cloned = lifecycle.clone_published_rubric(
        db,
        source.rubric.id,
        "4.0.0",
        source.user.id,
    )
    db.commit()
    clone_graph = _graph_entities(db, cloned.id)
    assert clone_graph.versions[0].version_hash == source_hash

    clone_graph.rules[0].rule_text = "克隆后修订的评分规则内容。"
    db.commit()
    lifecycle.submit_for_review(db, cloned.id)
    db.commit()
    republished_version = lifecycle.publish_rubric(
        db,
        cloned.id,
        clone_graph.compilations[0].id,
        source.user.id,
        now=clone_graph.compilations[0].created_at + timedelta(minutes=5),
    )
    db.commit()
    db.expire_all()

    republished_compilation = db.get(
        models.RubricCompilation,
        clone_graph.compilations[0].id,
    )
    republished_version = db.get(models.RubricVersion, republished_version.id)
    assert republished_version.version_hash == republished_compilation.final_version_hash
    assert republished_version.version_hash != source_hash
    assert _graph_snapshot(db, source.rubric.id) == source_before


@pytest.mark.parametrize("source_state", ["draft", "review"])
def test_clone_requires_a_published_source(db, source_state):
    lifecycle = _lifecycle_contract()
    source = _make_full_graph(db, f"clone-source-{source_state}")
    if source_state == "review":
        lifecycle.submit_for_review(db, source.rubric.id)
        db.commit()
    before = _database_counts(db)
    source_before = _graph_snapshot(db, source.rubric.id)

    with pytest.raises(_rejection_errors(lifecycle)):
        lifecycle.clone_published_rubric(
            db,
            source.rubric.id,
            "3.0.0",
            source.user.id,
        )
        db.commit()
    db.rollback()

    assert _database_counts(db) == before
    assert _graph_snapshot(db, source.rubric.id) == source_before


def test_clone_version_collision_rolls_back_without_partial_graph(db):
    lifecycle = _lifecycle_contract()
    source = _make_full_graph(db, "clone-collision")
    _publish_graph(db, lifecycle, source)
    existing = models.Rubric(
        name=source.rubric.name,
        version="3.1.0",
        total_score=source.rubric.total_score,
        status="draft",
        created_by=source.user.id,
        owner_id=source.user.id,
    )
    db.add(existing)
    db.commit()
    before = _database_counts(db)
    source_before = _graph_snapshot(db, source.rubric.id)

    with pytest.raises(_rejection_errors(lifecycle)):
        lifecycle.clone_published_rubric(
            db,
            source.rubric.id,
            "3.1.0",
            source.user.id,
        )
        db.commit()
    db.rollback()

    assert _database_counts(db) == before
    assert _graph_snapshot(db, source.rubric.id) == source_before


def test_clone_runtime_failure_rolls_back_the_entire_new_graph(db):
    lifecycle = _lifecycle_contract()
    source = _make_full_graph(db, "clone-rollback")
    _publish_graph(db, lifecycle, source)
    before = _database_counts(db)
    source_before = _graph_snapshot(db, source.rubric.id)
    deepest_tables = (
        models.RuleLevel.__table__,
        models.RuleTemplateLink.__table__,
        SOURCE_LINK_TABLE,
    )

    def _fail_after_full_graph_flush(session, _flush_context):
        connection = session.connection()
        copied_all_deep_rows = all(
            connection.execute(select(func.count()).select_from(table)).scalar_one()
            > before[table.name]
            for table in deepest_tables
        )
        if copied_all_deep_rows:
            raise RuntimeError("injected clone failure after full graph flush")

    event.listen(db, "after_flush_postexec", _fail_after_full_graph_flush)
    try:
        with pytest.raises(_runtime_failure_errors(lifecycle)):
            lifecycle.clone_published_rubric(
                db,
                source.rubric.id,
                "3.2.0",
                source.user.id,
            )
            db.commit()
    finally:
        event.remove(db, "after_flush_postexec", _fail_after_full_graph_flush)
        db.rollback()

    assert _database_counts(db) == before
    assert _graph_snapshot(db, source.rubric.id) == source_before
