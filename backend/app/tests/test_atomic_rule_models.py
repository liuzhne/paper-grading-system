"""P1-02 模板条目、原子规则、档位和模板映射的合同测试。

本文件只约束数据表达、来源关系和基础数据完整性。发布状态机、发布后
不可修改、checker 完整性、发布 blocker、档位单调和分值守恒属于后续任务。
"""

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import JSON
from sqlalchemy import Numeric
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.sqlite import enable_sqlite_foreign_keys


P1_02_MODEL_NAMES = ("TemplateItem", "AtomicRule", "RuleLevel", "RuleTemplateLink")
SOURCE_LINK_TABLE = "atomic_rule_source_rules"
P1_02_REJECTION_ERRORS = (IntegrityError, StatementError, TypeError, ValueError)


def _p1_02_contract():
    missing = [name for name in P1_02_MODEL_NAMES if not hasattr(models, name)]
    assert not missing, f"P1-02 缺少 ORM 模型：{', '.join(missing)}"
    assert SOURCE_LINK_TABLE in models.Base.metadata.tables, "P1-02 缺少 SourceRule↔AtomicRule 多对多关联表"
    return (
        *(getattr(models, name) for name in P1_02_MODEL_NAMES),
        models.Base.metadata.tables[SOURCE_LINK_TABLE],
    )


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


def _make_p1_graph(db, suffix, *, with_version=True):
    user = models.User(
        username=f"p102-{suffix}",
        display_name=f"P1-02 审核员 {suffix}",
        role="reviewer",
    )
    db.add(user)
    db.flush()
    rubric = models.Rubric(
        name=f"P1-02 评分标准 {suffix}",
        version="legacy-draft",
        total_score=100,
        status="draft",
        created_by=user.id,
    )
    db.add(rubric)
    db.flush()
    criterion = models.RubricCriterion(
        rubric_id=rubric.id,
        code="METHOD",
        name="研究方法",
        max_score=20,
        criterion_type="hybrid",
        scoring_mode="deductive",
    )
    db.add(criterion)
    db.flush()

    version_hash = (str((len(suffix) % 9) + 1) * 64) if with_version else None
    compilation = models.RubricCompilation(
        rubric_id=rubric.id,
        status="validated" if with_version else "failed",
        parser_version="excel-v4/docx-v3",
        compiler_version="atomic-v1",
        prompt_version="rubric-compile-2026-07-16",
        created_by=user.id,
        final_version_hash=version_hash,
    )
    db.add(compilation)
    db.flush()
    excel = models.SourceArtifact(
        compilation_id=compilation.id,
        artifact_type="excel",
        file_name=f"评分标准-{suffix}.xlsx",
        file_hash="a" * 64,
        file_size_bytes=4096,
        uploaded_by=user.id,
    )
    docx = models.SourceArtifact(
        compilation_id=compilation.id,
        artifact_type="docx",
        file_name=f"模板-{suffix}.docx",
        file_hash="b" * 64,
        file_size_bytes=8192,
        uploaded_by=user.id,
    )
    db.add_all([excel, docx])
    db.flush()
    source_rule_1 = models.SourceRule(
        source_artifact_id=excel.id,
        source_rule_code="METHOD-01",
        sheet_name="原始规则",
        row_number=2,
        cell_locator="原始规则!A2:N2",
        raw_text="研究步骤完整且关系清晰。",
    )
    source_rule_2 = models.SourceRule(
        source_artifact_id=excel.id,
        source_rule_code="METHOD-02",
        sheet_name="原始规则",
        row_number=3,
        cell_locator="原始规则!A3:N3",
        raw_text="方法选择应有理由和证据。",
    )
    db.add_all([source_rule_1, source_rule_2])
    db.flush()
    version = None
    if with_version:
        version = models.RubricVersion(
            rubric_id=rubric.id,
            compilation_id=compilation.id,
            version="1.0.0",
            workflow_profile="template_driven",
            global_policy={},
            version_hash=version_hash,
            created_by=user.id,
        )
        db.add(version)
        db.flush()
    return SimpleNamespace(
        user=user,
        rubric=rubric,
        criterion=criterion,
        compilation=compilation,
        excel=excel,
        docx=docx,
        source_rule_1=source_rule_1,
        source_rule_2=source_rule_2,
        version=version,
    )


def _new_template(TemplateItem, graph, **overrides):
    values = {
        "source_artifact_id": graph.docx.id,
        "item_code": "TPL-METHOD-01",
        "kind": "structure",
        "section_path": ["第三章 研究方法", "3.2 实验步骤"],
        "raw_text": "  必须依次说明数据准备、训练和验证步骤。\n",
        "normalized_constraint": {"required_labels": ["数据准备", "训练", "验证"]},
        "strictness": "required",
        "source_locator": {"paragraph_id": "w:p-42", "comment_id": "7"},
        "source_hash": "c" * 64,
        "parse_confidence": 0.875,
    }
    values.update(overrides)
    return TemplateItem(**values)


def _new_atomic(AtomicRule, graph, **overrides):
    values = {
        "rubric_version_id": graph.version.id,
        "criterion_id": graph.criterion.id,
        "rule_code": "METHOD-01-D",
        "name": "研究步骤完整性",
        "rule_text": "模板列举的研究步骤必须完整出现。",
        "direction": "deduct",
        "effect_type": "score",
        "max_points": 2,
        "repeat_policy": "once",
        "cap_points": 2,
        "judge_type": "deterministic",
        "checker_key": "required_structure_presence",
        "checker_params": {"required_labels": ["数据准备", "训练", "验证"]},
        "evidence_policy": {"scope": ["第三章"], "min_quotes": 1},
        "positive_example": "三个步骤均出现。",
        "negative_example": "缺少验证步骤。",
        "boundary_example": "标题缺失但正文明确列出。",
        "strictness": "required",
        "applies_to": "第三章 研究方法",
        "mutex_group": "METHOD-STEPS",
        "depends_on_rule_codes": [],
        "status": "draft",
        "creation_method": "compiler",
    }
    values.update(overrides)
    return AtomicRule(**values)


def _unique_column_sets(table):
    unique_sets = {
        frozenset(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    unique_sets.update(
        frozenset(column.name for column in index.columns)
        for index in table.indexes
        if index.unique
    )
    return unique_sets


def _source_rules_collection(rule):
    relationships = [
        relationship
        for relationship in sa_inspect(type(rule)).relationships
        if relationship.mapper.class_ is models.SourceRule and relationship.uselist
    ]
    assert len(relationships) == 1, "AtomicRule 必须公开一个指向 SourceRule 的多对多集合关系"
    relationship = relationships[0]
    assert relationship.secondary is models.Base.metadata.tables[SOURCE_LINK_TABLE]
    return getattr(rule, relationship.key)


def test_p1_02_schema_declares_models_fields_relations_and_scoped_uniques():
    TemplateItem, AtomicRule, RuleLevel, RuleTemplateLink, source_links = _p1_02_contract()
    expected_columns = {
        TemplateItem: {
            "id",
            "source_artifact_id",
            "item_code",
            "kind",
            "section_path",
            "raw_text",
            "normalized_constraint",
            "strictness",
            "source_locator",
            "source_hash",
            "parse_confidence",
            "created_at",
        },
        AtomicRule: {
            "id",
            "rubric_version_id",
            "criterion_id",
            "rule_code",
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
            "status",
            "creation_method",
            "reviewed_by",
            "reviewed_at",
            "created_at",
        },
        RuleLevel: {
            "id",
            "atomic_rule_id",
            "level_code",
            "points",
            "descriptor",
            "positive_example",
            "negative_example",
            "display_order",
            "created_at",
        },
        RuleTemplateLink: {
            "id",
            "rule_id",
            "template_item_id",
            "relationship_type",
            "match_method",
            "match_confidence",
            "rationale",
            "review_status",
            "reviewed_by",
            "reviewed_at",
            "created_at",
        },
    }
    for model, required in expected_columns.items():
        assert required.issubset(model.__table__.c.keys())
        assert {column.name for column in model.__table__.primary_key.columns} == {"id"}

    required_foreign_keys = {
        (TemplateItem, "source_artifact_id"): "source_artifacts.id",
        (AtomicRule, "rubric_version_id"): "rubric_versions.id",
        (AtomicRule, "criterion_id"): "rubric_criteria.id",
        (RuleLevel, "atomic_rule_id"): "atomic_rules.id",
        (RuleTemplateLink, "rule_id"): "atomic_rules.id",
        (RuleTemplateLink, "template_item_id"): "template_items.id",
    }
    for (model, column_name), target in required_foreign_keys.items():
        column = model.__table__.c[column_name]
        assert column.nullable is False
        assert {foreign_key.target_fullname for foreign_key in column.foreign_keys} == {target}

    for model in (AtomicRule, RuleTemplateLink):
        reviewed_by = model.__table__.c.reviewed_by
        assert reviewed_by.nullable is True
        assert {foreign_key.target_fullname for foreign_key in reviewed_by.foreign_keys} == {"users.id"}

    expected_nullable = {
        TemplateItem: {"normalized_constraint"},
        AtomicRule: {
            "max_points",
            "repeat_policy",
            "cap_points",
            "checker_key",
            "positive_example",
            "negative_example",
            "boundary_example",
            "mutex_group",
            "reviewed_by",
            "reviewed_at",
        },
        RuleLevel: {"positive_example", "negative_example"},
        RuleTemplateLink: {"match_confidence", "reviewed_by", "reviewed_at"},
    }
    for model, nullable_columns in expected_nullable.items():
        assert all(model.__table__.c[column].nullable is True for column in nullable_columns)

    assert frozenset({"source_artifact_id", "item_code"}) in _unique_column_sets(TemplateItem.__table__)
    assert frozenset({"rubric_version_id", "rule_code"}) in _unique_column_sets(AtomicRule.__table__)
    assert frozenset({"atomic_rule_id", "level_code"}) in _unique_column_sets(RuleLevel.__table__)
    assert frozenset({"rule_id", "template_item_id"}) in _unique_column_sets(RuleTemplateLink.__table__)

    assert {"atomic_rule_id", "source_rule_id"}.issubset(source_links.c.keys())
    assert {column.name for column in source_links.primary_key.columns} == {"atomic_rule_id", "source_rule_id"}
    expected_source_link_foreign_keys = {
        "atomic_rule_id": "atomic_rules.id",
        "source_rule_id": "source_rules.id",
    }
    for column_name, target in expected_source_link_foreign_keys.items():
        column = source_links.c[column_name]
        assert column.nullable is False
        assert {foreign_key.target_fullname for foreign_key in column.foreign_keys} == {target}

    json_columns = {
        TemplateItem: {"section_path", "normalized_constraint", "source_locator"},
        AtomicRule: {"depends_on_rule_codes", "checker_params", "evidence_policy"},
    }
    for model, columns in json_columns.items():
        assert all(isinstance(model.__table__.c[column].type, JSON) for column in columns)
    expected_json_defaults = {
        TemplateItem: {"section_path", "source_locator"},
        AtomicRule: {"depends_on_rule_codes", "checker_params", "evidence_policy"},
    }
    for model, columns in expected_json_defaults.items():
        assert all(model.__table__.c[column].default is not None for column in columns)
    numeric_columns = {
        (TemplateItem, "parse_confidence"),
        (AtomicRule, "max_points"),
        (AtomicRule, "cap_points"),
        (RuleLevel, "points"),
        (RuleTemplateLink, "match_confidence"),
    }
    assert all(isinstance(model.__table__.c[column].type, Numeric) for model, column in numeric_columns)

    expected_enum_values = {
        TemplateItem: {
            "section",
            "content",
            "comment",
            "structure",
            "format",
            "required",
            "preferred",
            "unknown",
        },
        AtomicRule: {
            "band",
            "deduct",
            "bonus",
            "none",
            "score",
            "review",
            "block_submission",
            "report_only",
            "once",
            "per_occurrence",
            "capped",
            "deterministic",
            "semantic",
            "required",
            "preferred",
            "unknown",
        },
        RuleTemplateLink: {
            "support",
            "constraint",
            "format_baseline",
            "exception",
            "exact",
            "heuristic",
            "llm_suggestion",
            "manual",
            "pending",
            "confirmed",
            "rejected",
        },
    }
    for model, values in expected_enum_values.items():
        check_sql = " ".join(
            str(constraint.sqltext)
            for constraint in model.__table__.constraints
            if constraint.__class__.__name__ == "CheckConstraint"
        )
        assert all(value in check_sql for value in values)


def test_atomic_rule_graph_round_trips_deterministic_semantic_sources_levels_and_template_review(db):
    TemplateItem, AtomicRule, RuleLevel, RuleTemplateLink, source_links = _p1_02_contract()
    graph = _make_p1_graph(db, "roundtrip")
    template = _new_template(TemplateItem, graph)
    deterministic = _new_atomic(AtomicRule, graph)
    semantic = _new_atomic(
        AtomicRule,
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
    )
    db.add_all([template, deterministic, semantic])
    db.flush()
    levels = [
        RuleLevel(
            atomic_rule_id=semantic.id,
            level_code="L0",
            points=0,
            descriptor="仅罗列方法名，过程断裂。",
            positive_example=None,
            negative_example="缺少输入输出说明。",
            display_order=0,
        ),
        RuleLevel(
            atomic_rule_id=semantic.id,
            level_code="L1",
            points=4,
            descriptor="步骤、输入输出和选择理由完整。",
            positive_example="说明训练、验证及选择依据。",
            negative_example=None,
            display_order=1,
        ),
    ]
    link = RuleTemplateLink(
        rule_id=deterministic.id,
        template_item_id=template.id,
        relationship_type="constraint",
        match_method="manual",
        match_confidence=1,
        rationale="审核者确认该模板步骤约束直接支持此确定性规则。",
        review_status="confirmed",
        reviewed_by=graph.user.id,
        reviewed_at=datetime(2026, 7, 16, 10, 0, 0),
    )
    db.add_all([*levels, link])
    db.execute(
        source_links.insert(),
        [
            {"atomic_rule_id": deterministic.id, "source_rule_id": graph.source_rule_1.id},
            {"atomic_rule_id": semantic.id, "source_rule_id": graph.source_rule_1.id},
            {"atomic_rule_id": semantic.id, "source_rule_id": graph.source_rule_2.id},
        ],
    )
    db.commit()
    template_id = template.id
    deterministic_id = deterministic.id
    semantic_id = semantic.id
    link_id = link.id
    db.expire_all()

    stored_template = db.get(TemplateItem, template_id)
    assert stored_template.section_path == ["第三章 研究方法", "3.2 实验步骤"]
    assert stored_template.raw_text == "  必须依次说明数据准备、训练和验证步骤。\n"
    assert stored_template.normalized_constraint == {"required_labels": ["数据准备", "训练", "验证"]}
    assert stored_template.source_locator == {"paragraph_id": "w:p-42", "comment_id": "7"}

    stored_deterministic = db.get(AtomicRule, deterministic_id)
    stored_semantic = db.get(AtomicRule, semantic_id)
    assert stored_deterministic.judge_type == "deterministic"
    assert stored_deterministic.checker_params == {"required_labels": ["数据准备", "训练", "验证"]}
    assert stored_semantic.judge_type == "semantic"
    assert stored_semantic.checker_key is None
    assert {level.level_code for level in db.query(RuleLevel).filter_by(atomic_rule_id=semantic_id)} == {"L0", "L1"}

    stored_sources = {
        (row.atomic_rule_id, row.source_rule_id)
        for row in db.execute(select(source_links.c.atomic_rule_id, source_links.c.source_rule_id))
    }
    assert (deterministic_id, graph.source_rule_1.id) in stored_sources
    assert {(semantic_id, graph.source_rule_1.id), (semantic_id, graph.source_rule_2.id)}.issubset(stored_sources)
    stored_link = db.get(RuleTemplateLink, link_id)
    assert stored_link.review_status == "confirmed"
    assert stored_link.match_confidence == 1
    assert stored_link.rationale.startswith("审核者确认")
    assert stored_link.reviewed_by == graph.user.id
    assert stored_link.reviewed_at == datetime(2026, 7, 16, 10, 0, 0)


def test_unknown_template_item_can_be_saved_before_any_rubric_version(db):
    TemplateItem, _, _, _, _ = _p1_02_contract()
    graph = _make_p1_graph(db, "unknown", with_version=False)
    item = _new_template(
        TemplateItem,
        graph,
        item_code="TPL-UNKNOWN-01",
        kind="format",
        section_path=[],
        raw_text="",
        normalized_constraint=None,
        strictness="unknown",
        source_locator={"xml_path": "/w:document/w:body/w:p[9]"},
        parse_confidence=0,
    )
    db.add(item)
    db.commit()
    item_id = item.id
    db.expire_all()

    stored = db.get(TemplateItem, item_id)
    assert stored.normalized_constraint is None
    assert stored.strictness == "unknown"
    assert stored.parse_confidence == 0
    assert db.query(models.RubricVersion).filter_by(compilation_id=graph.compilation.id).count() == 0


def test_source_rule_atomic_rule_relationship_is_true_many_to_many(db):
    _, AtomicRule, _, _, source_links = _p1_02_contract()
    graph = _make_p1_graph(db, "many-to-many")
    deterministic = _new_atomic(AtomicRule, graph)
    semantic = _new_atomic(
        AtomicRule,
        graph,
        rule_code="METHOD-01-S",
        judge_type="semantic",
        direction="band",
        repeat_policy=None,
        cap_points=None,
        checker_key=None,
        checker_params={},
    )
    db.add_all([deterministic, semantic])
    db.flush()
    db.execute(
        source_links.insert(),
        [
            {"atomic_rule_id": deterministic.id, "source_rule_id": graph.source_rule_1.id},
            {"atomic_rule_id": semantic.id, "source_rule_id": graph.source_rule_1.id},
            {"atomic_rule_id": semantic.id, "source_rule_id": graph.source_rule_2.id},
        ],
    )
    db.commit()

    with pytest.raises(IntegrityError):
        db.execute(
            source_links.insert().values(
                atomic_rule_id=deterministic.id,
                source_rule_id=graph.source_rule_1.id,
            )
        )
        db.commit()
    db.rollback()


@pytest.mark.parametrize("judge_type", ["hybrid", "llm_judgment", ""])
def test_atomic_rule_rejects_non_leaf_judge_types(db, judge_type):
    _, AtomicRule, _, _, _ = _p1_02_contract()
    graph = _make_p1_graph(db, f"judge-{judge_type or 'empty'}")
    with pytest.raises(P1_02_REJECTION_ERRORS):
        rule = _new_atomic(AtomicRule, graph, judge_type=judge_type)
        db.add(rule)
        db.commit()


@pytest.mark.parametrize(
    ("object_type", "field"),
    [
        ("template", "kind"),
        ("template", "strictness"),
        ("atomic", "direction"),
        ("atomic", "effect_type"),
        ("atomic", "repeat_policy"),
        ("atomic", "strictness"),
        ("link", "relationship_type"),
        ("link", "match_method"),
        ("link", "review_status"),
    ],
)
def test_enum_fields_reject_values_outside_the_p1_02_contract(db, object_type, field):
    TemplateItem, AtomicRule, _, RuleTemplateLink, _ = _p1_02_contract()
    graph = _make_p1_graph(db, f"enum-{object_type}-{field}")
    if object_type == "link":
        template = _new_template(TemplateItem, graph)
        rule = _new_atomic(AtomicRule, graph)
        db.add_all([template, rule])
        db.flush()
    with pytest.raises(P1_02_REJECTION_ERRORS):
        if object_type == "template":
            candidate = _new_template(
                TemplateItem,
                graph,
                item_code=f"TPL-INVALID-{field.upper()}",
                **{field: "__invalid__"},
            )
        elif object_type == "atomic":
            candidate = _new_atomic(
                AtomicRule,
                graph,
                rule_code=f"METHOD-INVALID-{field.upper()}",
                **{field: "__invalid__"},
            )
        else:
            values = {
                "rule_id": rule.id,
                "template_item_id": template.id,
                "relationship_type": "support",
                "match_method": "manual",
                "match_confidence": None,
                "rationale": "非法枚举边界测试",
                "review_status": "pending",
            }
            values[field] = "__invalid__"
            candidate = RuleTemplateLink(**values)
        db.add(candidate)
        db.commit()


@pytest.mark.parametrize(
    ("object_type", "field", "invalid_value"),
    [
        ("template", "section_path", {"not": "a list"}),
        ("template", "source_locator", ["not", "an", "object"]),
        ("template", "normalized_constraint", ["not", "an", "object"]),
        ("atomic", "depends_on_rule_codes", {"not": "a list"}),
        ("atomic", "checker_params", ["not", "an", "object"]),
        ("atomic", "evidence_policy", ["not", "an", "object"]),
    ],
)
def test_json_fields_reject_invalid_top_level_shapes(db, object_type, field, invalid_value):
    TemplateItem, AtomicRule, _, _, _ = _p1_02_contract()
    graph = _make_p1_graph(db, f"json-shape-{field}")
    with pytest.raises(P1_02_REJECTION_ERRORS):
        if object_type == "template":
            candidate = _new_template(
                TemplateItem,
                graph,
                item_code=f"TPL-SHAPE-{field.upper()}",
                **{field: invalid_value},
            )
        else:
            candidate = _new_atomic(
                AtomicRule,
                graph,
                rule_code=f"METHOD-SHAPE-{field.upper()}",
                **{field: invalid_value},
            )
        db.add(candidate)
        db.commit()


def test_scoped_unique_constraints_reject_duplicates(db):
    TemplateItem, AtomicRule, RuleLevel, RuleTemplateLink, _ = _p1_02_contract()
    graph = _make_p1_graph(db, "unique")
    template = _new_template(TemplateItem, graph)
    rule = _new_atomic(AtomicRule, graph)
    db.add_all([template, rule])
    db.flush()
    level = RuleLevel(
        atomic_rule_id=rule.id,
        level_code="L0",
        points=0,
        descriptor="不满足",
        display_order=0,
    )
    link = RuleTemplateLink(
        rule_id=rule.id,
        template_item_id=template.id,
        relationship_type="constraint",
        match_method="manual",
        match_confidence=None,
        rationale="待审核",
        review_status="pending",
    )
    db.add_all([level, link])
    db.commit()

    duplicate_cases = [
        _new_template(TemplateItem, graph),
        _new_atomic(AtomicRule, graph),
        RuleLevel(
            atomic_rule_id=rule.id,
            level_code="L0",
            points=1,
            descriptor="重复档位",
            display_order=1,
        ),
        RuleTemplateLink(
            rule_id=rule.id,
            template_item_id=template.id,
            relationship_type="support",
            match_method="exact",
            match_confidence=1,
            rationale="重复映射",
            review_status="pending",
        ),
    ]
    for duplicate in duplicate_cases:
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_codes_can_be_reused_in_a_different_artifact_version_or_parent_rule(db):
    TemplateItem, AtomicRule, RuleLevel, _, _ = _p1_02_contract()
    first = _make_p1_graph(db, "scope-a")
    second = _make_p1_graph(db, "scope-b")
    first_item = _new_template(TemplateItem, first)
    second_item = _new_template(TemplateItem, second)
    first_rule = _new_atomic(AtomicRule, first)
    second_rule = _new_atomic(AtomicRule, second)
    db.add_all([first_item, second_item, first_rule, second_rule])
    db.flush()
    db.add_all(
        [
            RuleLevel(
                atomic_rule_id=first_rule.id,
                level_code="L0",
                points=0,
                descriptor="第一版本",
                display_order=0,
            ),
            RuleLevel(
                atomic_rule_id=second_rule.id,
                level_code="L0",
                points=0,
                descriptor="第二版本",
                display_order=0,
            ),
        ]
    )
    db.commit()

    assert db.query(TemplateItem).filter_by(item_code="TPL-METHOD-01").count() == 2
    assert db.query(AtomicRule).filter_by(rule_code="METHOD-01-D").count() == 2
    assert db.query(RuleLevel).filter_by(level_code="L0").count() == 2


def test_json_defaults_are_isolated_and_top_level_mutations_persist(db):
    TemplateItem, AtomicRule, _, _, _ = _p1_02_contract()
    graph = _make_p1_graph(db, "json")
    first_item = TemplateItem(
        source_artifact_id=graph.docx.id,
        item_code="TPL-JSON-01",
        kind="content",
        raw_text="仅用于验证 JSON 默认值。",
        normalized_constraint={},
        strictness="preferred",
        source_hash="d" * 64,
        parse_confidence=1,
    )
    second_item = TemplateItem(
        source_artifact_id=graph.docx.id,
        item_code="TPL-JSON-02",
        kind="content",
        raw_text="仅用于验证 JSON 默认值。",
        normalized_constraint={},
        strictness="preferred",
        source_hash="e" * 64,
        parse_confidence=1,
    )
    first_rule = AtomicRule(
        rubric_version_id=graph.version.id,
        criterion_id=graph.criterion.id,
        rule_code="METHOD-JSON-01",
        name="JSON 默认值规则一",
        rule_text="不执行评分，仅验证默认 JSON 容器。",
        direction="none",
        effect_type="report_only",
        judge_type="semantic",
        strictness="preferred",
        applies_to="global",
        status="draft",
        creation_method="compiler",
    )
    second_rule = AtomicRule(
        rubric_version_id=graph.version.id,
        criterion_id=graph.criterion.id,
        rule_code="METHOD-JSON-02",
        name="JSON 默认值规则二",
        rule_text="不执行评分，仅验证默认 JSON 容器。",
        direction="none",
        effect_type="report_only",
        judge_type="semantic",
        strictness="preferred",
        applies_to="global",
        status="draft",
        creation_method="compiler",
    )
    db.add_all([first_item, second_item, first_rule, second_rule])
    db.commit()

    assert first_item.section_path == second_item.section_path == []
    assert first_item.source_locator == second_item.source_locator == {}
    assert first_rule.depends_on_rule_codes == second_rule.depends_on_rule_codes == []
    assert first_rule.checker_params == second_rule.checker_params == {}
    assert first_rule.evidence_policy == second_rule.evidence_policy == {}
    assert first_item.section_path is not second_item.section_path
    assert first_item.source_locator is not second_item.source_locator
    assert first_item.normalized_constraint is not second_item.normalized_constraint
    assert first_rule.depends_on_rule_codes is not second_rule.depends_on_rule_codes
    assert first_rule.checker_params is not second_rule.checker_params
    assert first_rule.evidence_policy is not second_rule.evidence_policy
    assert first_rule.max_points is None
    assert first_rule.repeat_policy is None
    assert first_rule.cap_points is None
    assert first_rule.checker_key is None
    first_item.section_path.append("第三章")
    first_item.source_locator["paragraph_id"] = "w:p-99"
    first_item.normalized_constraint["required_count"] = 3
    first_rule.depends_on_rule_codes.append("METHOD-00-D")
    first_rule.checker_params["required_count"] = 3
    first_rule.evidence_policy["min_quotes"] = 1
    db.commit()
    ids = (first_item.id, second_item.id, first_rule.id, second_rule.id)
    db.expire_all()

    stored_first_item = db.get(TemplateItem, ids[0])
    stored_second_item = db.get(TemplateItem, ids[1])
    stored_first_rule = db.get(AtomicRule, ids[2])
    stored_second_rule = db.get(AtomicRule, ids[3])
    assert stored_first_item.section_path == ["第三章"]
    assert stored_first_item.source_locator == {"paragraph_id": "w:p-99"}
    assert stored_first_item.normalized_constraint == {"required_count": 3}
    assert stored_second_item.section_path == []
    assert stored_second_item.source_locator == {}
    assert stored_second_item.normalized_constraint == {}
    assert stored_first_rule.depends_on_rule_codes == ["METHOD-00-D"]
    assert stored_first_rule.checker_params == {"required_count": 3}
    assert stored_first_rule.evidence_policy == {"min_quotes": 1}
    assert stored_second_rule.depends_on_rule_codes == []
    assert stored_second_rule.checker_params == {}
    assert stored_second_rule.evidence_policy == {}


def test_numeric_boundaries_accept_inclusive_zero_and_one(db):
    TemplateItem, AtomicRule, RuleLevel, RuleTemplateLink, _ = _p1_02_contract()
    graph = _make_p1_graph(db, "numeric-inclusive")
    template = _new_template(
        TemplateItem,
        graph,
        item_code="TPL-NUMERIC-INCLUSIVE",
        parse_confidence=1,
    )
    rule = _new_atomic(
        AtomicRule,
        graph,
        rule_code="METHOD-NUMERIC-INCLUSIVE",
        max_points=0,
        cap_points=0,
    )
    db.add_all([template, rule])
    db.flush()
    level = RuleLevel(
        atomic_rule_id=rule.id,
        level_code="L0",
        points=0,
        descriptor="零分边界",
        display_order=0,
    )
    link = RuleTemplateLink(
        rule_id=rule.id,
        template_item_id=template.id,
        relationship_type="support",
        match_method="manual",
        match_confidence=0,
        rationale="置信度下界仍是有效值。",
        review_status="pending",
    )
    db.add_all([level, link])
    db.commit()

    assert template.parse_confidence == 1
    assert rule.max_points == 0
    assert rule.cap_points == 0
    assert level.points == 0
    assert level.display_order == 0
    assert link.match_confidence == 0


def test_fractional_scores_and_confidences_round_trip_without_precision_loss(db):
    TemplateItem, AtomicRule, RuleLevel, RuleTemplateLink, _ = _p1_02_contract()
    graph = _make_p1_graph(db, "numeric-precision")
    template = _new_template(
        TemplateItem,
        graph,
        item_code="TPL-NUMERIC-PRECISION",
        parse_confidence=Decimal("0.875"),
    )
    rule = _new_atomic(
        AtomicRule,
        graph,
        rule_code="METHOD-NUMERIC-PRECISION",
        max_points=Decimal("1.25"),
        repeat_policy="capped",
        cap_points=Decimal("1.25"),
    )
    db.add_all([template, rule])
    db.flush()
    level = RuleLevel(
        atomic_rule_id=rule.id,
        level_code="L-PRECISION",
        points=Decimal("0.75"),
        descriptor="保留两位小数的档位。",
        display_order=0,
    )
    link = RuleTemplateLink(
        rule_id=rule.id,
        template_item_id=template.id,
        relationship_type="support",
        match_method="heuristic",
        match_confidence=Decimal("0.625"),
        rationale="保留三位小数的匹配置信度。",
        review_status="pending",
    )
    db.add_all([level, link])
    db.commit()
    ids = (template.id, rule.id, level.id, link.id)
    db.expire_all()

    assert db.get(TemplateItem, ids[0]).parse_confidence == Decimal("0.875")
    stored_rule = db.get(AtomicRule, ids[1])
    assert stored_rule.max_points == Decimal("1.25")
    assert stored_rule.cap_points == Decimal("1.25")
    assert db.get(RuleLevel, ids[2]).points == Decimal("0.75")
    assert db.get(RuleTemplateLink, ids[3]).match_confidence == Decimal("0.625")


@pytest.mark.parametrize(
    ("object_type", "overrides"),
    [
        ("template", {"parse_confidence": Decimal("-0.001")}),
        ("template", {"parse_confidence": Decimal("1.001")}),
        ("atomic", {"max_points": -0.01}),
        ("atomic", {"cap_points": -0.01}),
        ("level", {"points": -0.01}),
        ("level", {"display_order": -1}),
        ("link", {"match_confidence": Decimal("-0.001")}),
        ("link", {"match_confidence": Decimal("1.001")}),
    ],
)
def test_numeric_boundaries_reject_invalid_values(db, object_type, overrides):
    TemplateItem, AtomicRule, RuleLevel, RuleTemplateLink, _ = _p1_02_contract()
    graph = _make_p1_graph(db, f"numeric-{object_type}-{str(overrides).replace(' ', '')}")
    template = _new_template(TemplateItem, graph)
    rule = _new_atomic(AtomicRule, graph)
    db.add_all([template, rule])
    db.flush()
    with pytest.raises(P1_02_REJECTION_ERRORS):
        if object_type == "template":
            candidate = _new_template(TemplateItem, graph, item_code="TPL-NUMERIC", **overrides)
        elif object_type == "atomic":
            candidate = _new_atomic(AtomicRule, graph, rule_code="METHOD-NUMERIC", **overrides)
        elif object_type == "level":
            values = {
                "atomic_rule_id": rule.id,
                "level_code": "L-INVALID",
                "points": 0,
                "descriptor": "边界测试",
                "display_order": 0,
            }
            values.update(overrides)
            candidate = RuleLevel(**values)
        else:
            values = {
                "rule_id": rule.id,
                "template_item_id": template.id,
                "relationship_type": "support",
                "match_method": "manual",
                "match_confidence": 0,
                "rationale": "边界测试",
                "review_status": "pending",
            }
            values.update(overrides)
            candidate = RuleTemplateLink(**values)
        db.add(candidate)
        db.commit()


def test_atomic_rule_rejects_source_rule_from_another_compilation(db):
    _, AtomicRule, _, _, source_links = _p1_02_contract()
    first = _make_p1_graph(db, "source-lineage-a")
    second = _make_p1_graph(db, "source-lineage-b")
    db.commit()
    rule = _new_atomic(AtomicRule, first, rule_code="METHOD-CROSS-SOURCE")
    db.add(rule)
    db.commit()

    with pytest.raises(P1_02_REJECTION_ERRORS):
        _source_rules_collection(rule).append(second.source_rule_1)
        db.commit()
    db.rollback()
    stored_links = db.execute(
        select(source_links).where(
            source_links.c.atomic_rule_id == rule.id,
            source_links.c.source_rule_id == second.source_rule_1.id,
        )
    ).all()
    assert stored_links == []


def test_rule_template_link_rejects_template_from_another_compilation(db):
    TemplateItem, AtomicRule, _, RuleTemplateLink, _ = _p1_02_contract()
    first = _make_p1_graph(db, "template-lineage-a")
    second = _make_p1_graph(db, "template-lineage-b")
    rule = _new_atomic(AtomicRule, first, rule_code="METHOD-CROSS-TEMPLATE")
    foreign_template = _new_template(
        TemplateItem,
        second,
        item_code="TPL-CROSS-COMPILATION",
    )
    db.add_all([rule, foreign_template])
    db.commit()

    with pytest.raises(P1_02_REJECTION_ERRORS):
        link = RuleTemplateLink(
            rule_id=rule.id,
            template_item_id=foreign_template.id,
            relationship_type="constraint",
            match_method="manual",
            match_confidence=None,
            rationale="跨 compilation 的模板不得静默复用。",
            review_status="pending",
        )
        db.add(link)
        db.commit()
    db.rollback()
    assert (
        db.query(RuleTemplateLink)
        .filter_by(rule_id=rule.id, template_item_id=foreign_template.id)
        .count()
        == 0
    )


def test_atomic_rule_rejects_criterion_from_another_rubric(db):
    _, AtomicRule, _, _, _ = _p1_02_contract()
    first = _make_p1_graph(db, "lineage-a")
    second = _make_p1_graph(db, "lineage-b")
    db.commit()
    with pytest.raises(P1_02_REJECTION_ERRORS):
        rule = _new_atomic(
            AtomicRule,
            first,
            rule_code="METHOD-CROSS-RUBRIC",
            criterion_id=second.criterion.id,
        )
        db.add(rule)
        db.commit()
    db.rollback()
    assert db.query(AtomicRule).filter_by(rule_code="METHOD-CROSS-RUBRIC").count() == 0


def test_atomic_rule_fk_change_ignores_stale_loaded_relationships(db):
    _, AtomicRule, _, _, _ = _p1_02_contract()
    first = _make_p1_graph(db, "stale-fk-a")
    second = _make_p1_graph(db, "stale-fk-b")
    rule = _new_atomic(AtomicRule, first, rule_code="METHOD-STALE-FK")
    db.add(rule)
    db.commit()

    assert rule.rubric_version.id == first.version.id
    assert rule.criterion.id == first.criterion.id
    rule.rubric_version_id = second.version.id
    with pytest.raises(P1_02_REJECTION_ERRORS):
        db.commit()
    db.rollback()

    db.refresh(rule)
    assert rule.rubric_version_id == first.version.id


def test_atomic_rule_version_change_revalidates_existing_source_links(db):
    _, AtomicRule, _, _, _ = _p1_02_contract()
    first = _make_p1_graph(db, "move-source-a")
    second = _make_p1_graph(db, "move-source-b")
    rule = _new_atomic(AtomicRule, first, rule_code="METHOD-MOVE-SOURCE")
    _source_rules_collection(rule).append(first.source_rule_1)
    db.add(rule)
    db.commit()

    rule.rubric_version_id = second.version.id
    rule.criterion_id = second.criterion.id
    with pytest.raises(P1_02_REJECTION_ERRORS):
        db.commit()
    db.rollback()


def test_atomic_rule_version_change_revalidates_existing_template_links(db):
    TemplateItem, AtomicRule, _, RuleTemplateLink, _ = _p1_02_contract()
    first = _make_p1_graph(db, "move-template-a")
    second = _make_p1_graph(db, "move-template-b")
    rule = _new_atomic(AtomicRule, first, rule_code="METHOD-MOVE-TEMPLATE")
    template = _new_template(TemplateItem, first, item_code="TPL-MOVE-TEMPLATE")
    link = RuleTemplateLink(
        rule=rule,
        template_item=template,
        relationship_type="constraint",
        match_method="manual",
        match_confidence=None,
        rationale="先建立同 compilation 的合法映射。",
        review_status="pending",
    )
    db.add_all([rule, template, link])
    db.commit()

    rule.rubric_version_id = second.version.id
    rule.criterion_id = second.criterion.id
    with pytest.raises(P1_02_REJECTION_ERRORS):
        db.commit()
    db.rollback()


def test_source_rule_artifact_change_revalidates_existing_atomic_rules(db):
    _, AtomicRule, _, _, _ = _p1_02_contract()
    first = _make_p1_graph(db, "move-artifact-source-a")
    second = _make_p1_graph(db, "move-artifact-source-b")
    rule = _new_atomic(AtomicRule, first, rule_code="METHOD-MOVE-SOURCE-ARTIFACT")
    _source_rules_collection(rule).append(first.source_rule_1)
    db.add(rule)
    db.commit()

    assert first.source_rule_1.artifact.id == first.excel.id
    first.source_rule_1.source_artifact_id = second.excel.id
    with pytest.raises(P1_02_REJECTION_ERRORS):
        db.commit()
    db.rollback()


def test_template_item_artifact_change_revalidates_existing_links(db):
    TemplateItem, AtomicRule, _, RuleTemplateLink, _ = _p1_02_contract()
    first = _make_p1_graph(db, "move-artifact-template-a")
    second = _make_p1_graph(db, "move-artifact-template-b")
    rule = _new_atomic(AtomicRule, first, rule_code="METHOD-MOVE-TEMPLATE-ARTIFACT")
    template = _new_template(TemplateItem, first, item_code="TPL-MOVE-ARTIFACT")
    link = RuleTemplateLink(
        rule=rule,
        template_item=template,
        relationship_type="constraint",
        match_method="manual",
        match_confidence=None,
        rationale="先建立同 compilation 的合法映射。",
        review_status="pending",
    )
    db.add_all([rule, template, link])
    db.commit()

    assert template.artifact.id == first.docx.id
    template.source_artifact_id = second.docx.id
    with pytest.raises(P1_02_REJECTION_ERRORS):
        db.commit()
    db.rollback()


def test_rule_template_link_fk_change_ignores_stale_loaded_relationship(db):
    TemplateItem, AtomicRule, _, RuleTemplateLink, _ = _p1_02_contract()
    first = _make_p1_graph(db, "move-link-a")
    second = _make_p1_graph(db, "move-link-b")
    rule = _new_atomic(AtomicRule, first, rule_code="METHOD-MOVE-LINK")
    template = _new_template(TemplateItem, first, item_code="TPL-MOVE-LINK-LOCAL")
    foreign_template = _new_template(
        TemplateItem,
        second,
        item_code="TPL-MOVE-LINK-FOREIGN",
    )
    link = RuleTemplateLink(
        rule=rule,
        template_item=template,
        relationship_type="support",
        match_method="manual",
        match_confidence=None,
        rationale="先建立同 compilation 的合法映射。",
        review_status="pending",
    )
    db.add_all([rule, template, foreign_template, link])
    db.commit()

    assert link.template_item.id == template.id
    link.template_item_id = foreign_template.id
    with pytest.raises(P1_02_REJECTION_ERRORS):
        db.commit()
    db.rollback()


def test_criterion_rubric_change_revalidates_existing_atomic_rules(db):
    _, AtomicRule, _, _, _ = _p1_02_contract()
    first = _make_p1_graph(db, "move-criterion-parent-a")
    second = _make_p1_graph(db, "move-criterion-parent-b")
    rule = _new_atomic(AtomicRule, first, rule_code="METHOD-MOVE-CRITERION-PARENT")
    db.add(rule)
    db.commit()

    first.criterion.rubric_id = second.rubric.id
    with pytest.raises(P1_02_REJECTION_ERRORS):
        db.commit()
    db.rollback()


def test_rubric_version_parent_change_revalidates_existing_atomic_rules(db):
    _, AtomicRule, _, _, _ = _p1_02_contract()
    first = _make_p1_graph(db, "move-version-parent-a")
    second = _make_p1_graph(db, "move-version-parent-b")
    rule = _new_atomic(AtomicRule, first, rule_code="METHOD-MOVE-VERSION-PARENT")
    db.add(rule)
    db.commit()

    first.version.rubric_id = second.rubric.id
    with pytest.raises(P1_02_REJECTION_ERRORS):
        db.commit()
    db.rollback()


def test_source_artifact_compilation_change_revalidates_descendant_source_links(db):
    _, AtomicRule, _, _, _ = _p1_02_contract()
    first = _make_p1_graph(db, "move-source-parent-a")
    second = _make_p1_graph(db, "move-source-parent-b")
    rule = _new_atomic(AtomicRule, first, rule_code="METHOD-MOVE-SOURCE-PARENT")
    _source_rules_collection(rule).append(first.source_rule_1)
    db.add(rule)
    db.commit()

    first.excel.compilation_id = second.compilation.id
    with pytest.raises(P1_02_REJECTION_ERRORS):
        db.commit()
    db.rollback()


def test_template_artifact_compilation_change_revalidates_descendant_links(db):
    TemplateItem, AtomicRule, _, RuleTemplateLink, _ = _p1_02_contract()
    first = _make_p1_graph(db, "move-template-parent-a")
    second = _make_p1_graph(db, "move-template-parent-b")
    rule = _new_atomic(AtomicRule, first, rule_code="METHOD-MOVE-TEMPLATE-PARENT")
    template = _new_template(TemplateItem, first, item_code="TPL-MOVE-TEMPLATE-PARENT")
    link = RuleTemplateLink(
        rule=rule,
        template_item=template,
        relationship_type="constraint",
        match_method="manual",
        match_confidence=None,
        rationale="先建立同 compilation 的合法映射。",
        review_status="pending",
    )
    db.add_all([rule, template, link])
    db.commit()

    first.docx.compilation_id = second.compilation.id
    with pytest.raises(P1_02_REJECTION_ERRORS):
        db.commit()
    db.rollback()
