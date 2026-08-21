"""P1-01 评分标准来源、编译与版本模型的合同测试。

这些测试刻意只覆盖持久化与来源关系。发布状态机、发布后不可变、
TemplateItem/AtomicRule 以及 blocker 的业务判定属于后续任务，不在此处约束。
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models


PROVENANCE_MODEL_NAMES = (
    "RubricCompilation",
    "SourceArtifact",
    "SourceRule",
    "RubricVersion",
)


def _provenance_models():
    missing = [name for name in PROVENANCE_MODEL_NAMES if not hasattr(models, name)]
    assert not missing, f"P1-01 缺少 ORM 模型：{', '.join(missing)}"
    return tuple(getattr(models, name) for name in PROVENANCE_MODEL_NAMES)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _add_user_and_rubric(db, suffix):
    user = models.User(
        username=f"provenance-{suffix}",
        display_name=f"来源审核员-{suffix}",
        role="reviewer",
    )
    db.add(user)
    db.flush()
    rubric = models.Rubric(
        name=f"评分标准-{suffix}",
        version="legacy-draft",
        total_score=100,
        status="draft",
        created_by=user.id,
    )
    db.add(rubric)
    db.flush()
    return user, rubric


def _new_compilation(RubricCompilation, rubric_id, user_id, **overrides):
    values = {
        "rubric_id": rubric_id,
        "status": "validated",
        "parser_version": "excel-v4/docx-v3",
        "compiler_version": "atomic-v1",
        "model_provider": "openai_compatible",
        "model_name": "judge-model-v2",
        "sampling_params": {"temperature": 0, "seed": 7, "stop": ["<END>"]},
        "prompt_version": "rubric-compile-2026-07-15",
        "raw_parse_output": {"dimensions": [{"code": "METHOD", "name": "研究方法"}]},
        "raw_model_output": {"suggestions": [{"source_rule_code": "METHOD-01"}]},
        "validation_result": {"valid": True, "checked_ids": ["METHOD-01"]},
        "blockers": [],
        "warnings": [{"code": "LOW_CONFIDENCE", "locator": "原始规则!F2"}],
        "human_changes": [],
        "created_by": user_id,
    }
    values.update(overrides)
    return RubricCompilation(**values)


def _new_artifact(SourceArtifact, compilation_id, user_id, **overrides):
    values = {
        "compilation_id": compilation_id,
        "artifact_type": "excel",
        "file_name": "学院评分标准（终稿）.xlsx",
        "file_hash": "a" * 64,
        "file_size_bytes": 4096,
        "uploaded_by": user_id,
    }
    values.update(overrides)
    return SourceArtifact(**values)


def test_provenance_schema_keeps_required_lineage_and_one_version():
    RubricCompilation, SourceArtifact, SourceRule, RubricVersion = _provenance_models()

    expected_columns = {
        RubricCompilation: {
            "rubric_id",
            "status",
            "parser_version",
            "compiler_version",
            "model_provider",
            "model_name",
            "sampling_params",
            "prompt_version",
            "raw_parse_output",
            "raw_model_output",
            "validation_result",
            "blockers",
            "warnings",
            "human_changes",
            "created_by",
            "reviewed_by",
            "reviewed_at",
            "published_at",
            "final_version_hash",
        },
        SourceArtifact: {
            "compilation_id",
            "artifact_type",
            "file_name",
            "file_hash",
            "file_size_bytes",
            "uploaded_by",
        },
        SourceRule: {
            "source_artifact_id",
            "source_rule_code",
            "sheet_name",
            "row_number",
            "cell_locator",
            "raw_text",
        },
        RubricVersion: {
            "rubric_id",
            "compilation_id",
            "version",
            "workflow_profile",
            "global_policy",
            "version_hash",
            "created_by",
        },
    }
    for model, required in expected_columns.items():
        assert required.issubset(model.__table__.c.keys())

    required_foreign_keys = {
        (RubricCompilation, "rubric_id"): "rubrics.id",
        (RubricCompilation, "created_by"): "users.id",
        (SourceArtifact, "compilation_id"): "rubric_compilations.id",
        (SourceArtifact, "uploaded_by"): "users.id",
        (SourceRule, "source_artifact_id"): "source_artifacts.id",
        (RubricVersion, "created_by"): "users.id",
    }
    for (model, column_name), target in required_foreign_keys.items():
        column = model.__table__.c[column_name]
        assert column.nullable is False
        assert {foreign_key.target_fullname for foreign_key in column.foreign_keys} == {target}

    reviewed_by = RubricCompilation.__table__.c.reviewed_by
    assert reviewed_by.nullable is True
    assert {foreign_key.target_fullname for foreign_key in reviewed_by.foreign_keys} == {"users.id"}

    assert RubricVersion.__table__.c.rubric_id.nullable is False
    assert RubricVersion.__table__.c.compilation_id.nullable is False
    version_lineage_foreign_keys = {
        frozenset(
            (column.name, element.target_fullname)
            for column, element in zip(constraint.columns, constraint.elements)
        )
        for constraint in RubricVersion.__table__.foreign_key_constraints
    }
    assert frozenset(
        {
            ("compilation_id", "rubric_compilations.id"),
            ("rubric_id", "rubric_compilations.rubric_id"),
        }
    ) in version_lineage_foreign_keys

    compilation_column = RubricVersion.__table__.c.compilation_id
    unique_column_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in RubricVersion.__table__.constraints
        if getattr(constraint, "unique", False) or constraint.__class__.__name__ == "UniqueConstraint"
    }
    unique_column_sets.update(
        tuple(column.name for column in index.columns)
        for index in RubricVersion.__table__.indexes
        if index.unique
    )
    assert compilation_column.unique is True or ("compilation_id",) in unique_column_sets


def test_provenance_graph_round_trips_verbatim_source_and_compilation_metadata(db):
    RubricCompilation, SourceArtifact, SourceRule, RubricVersion = _provenance_models()
    user, rubric = _add_user_and_rubric(db, "roundtrip")
    reviewed_at = datetime(2026, 7, 15, 12, 30, 45)
    published_at = datetime(2026, 7, 15, 13, 0, 0)
    version_hash = "f" * 64
    raw_rule_text = "  必须说明样本来源\n=1+1；阈值 0.80 🔒\t"
    human_changes = [
        {
            "path": "/source_rules/METHOD-01/rule_text",
            "before": "说明样本",
            "after": "必须说明样本来源",
            "reason": "审核者按原始单元格修订",
        }
    ]
    compilation = _new_compilation(
        RubricCompilation,
        rubric.id,
        user.id,
        human_changes=human_changes,
        reviewed_by=user.id,
        reviewed_at=reviewed_at,
        published_at=published_at,
        final_version_hash=version_hash,
    )
    db.add(compilation)
    db.flush()
    excel = _new_artifact(SourceArtifact, compilation.id, user.id)
    docx = _new_artifact(
        SourceArtifact,
        compilation.id,
        user.id,
        artifact_type="docx",
        file_name="论文模板（含批注）.docx",
        file_hash="b" * 64,
        file_size_bytes=8193,
    )
    db.add_all([excel, docx])
    db.flush()
    source_rule = SourceRule(
        source_artifact_id=excel.id,
        source_rule_code="METHOD-01",
        sheet_name="原始规则",
        row_number=2,
        cell_locator="原始规则!A2:N2",
        raw_text=raw_rule_text,
    )
    version = RubricVersion(
        rubric_id=rubric.id,
        compilation_id=compilation.id,
        version="1.0.0",
        workflow_profile="template_driven",
        global_policy={
            "rounding": {"mode": "half_up", "digits": 1},
            "grade_boundaries": [{"code": "PASS", "min": 60}],
        },
        version_hash=version_hash,
        created_by=user.id,
    )
    db.add_all([source_rule, version])
    db.commit()

    compilation_id = compilation.id
    rule_id = source_rule.id
    version_id = version.id
    db.expire_all()

    stored = db.get(RubricCompilation, compilation_id)
    stored_artifacts = db.query(SourceArtifact).filter_by(compilation_id=stored.id).all()
    assert {artifact.artifact_type for artifact in stored_artifacts} == {"excel", "docx"}
    stored_excel = next(artifact for artifact in stored_artifacts if artifact.artifact_type == "excel")
    assert stored_excel.file_name == "学院评分标准（终稿）.xlsx"
    assert stored_excel.file_hash == "a" * 64
    assert stored_excel.file_size_bytes == 4096
    assert stored_excel.uploaded_by == user.id
    stored_rule = db.get(SourceRule, rule_id)
    assert stored_rule.source_artifact_id == stored_excel.id
    assert stored_rule.sheet_name == "原始规则"
    assert stored_rule.row_number == 2
    assert stored_rule.cell_locator == "原始规则!A2:N2"
    assert stored_rule.raw_text == raw_rule_text

    assert stored.parser_version == "excel-v4/docx-v3"
    assert stored.compiler_version == "atomic-v1"
    assert stored.model_provider == "openai_compatible"
    assert stored.model_name == "judge-model-v2"
    assert stored.sampling_params == {"temperature": 0, "seed": 7, "stop": ["<END>"]}
    assert stored.prompt_version == "rubric-compile-2026-07-15"
    assert stored.raw_parse_output["dimensions"][0]["name"] == "研究方法"
    assert stored.raw_model_output == {"suggestions": [{"source_rule_code": "METHOD-01"}]}
    assert stored.validation_result == {"valid": True, "checked_ids": ["METHOD-01"]}
    assert stored.warnings == [{"code": "LOW_CONFIDENCE", "locator": "原始规则!F2"}]
    assert stored.human_changes == human_changes
    assert stored.reviewed_by == user.id
    assert stored.reviewed_at == reviewed_at
    assert stored.published_at == published_at
    assert stored.final_version_hash == version_hash

    stored_version = db.get(RubricVersion, version_id)
    assert stored_version.compilation_id == stored.id
    assert stored_version.rubric_id == stored.rubric_id
    assert stored_version.workflow_profile == "template_driven"
    assert stored_version.version_hash == stored.final_version_hash
    assert stored_version.global_policy["rounding"] == {"mode": "half_up", "digits": 1}


def test_failed_compilation_can_be_audited_without_a_published_version(db):
    RubricCompilation, SourceArtifact, _, RubricVersion = _provenance_models()
    user, rubric = _add_user_and_rubric(db, "failed")
    compilation = _new_compilation(
        RubricCompilation,
        rubric.id,
        user.id,
        status="failed",
        model_provider=None,
        model_name=None,
        sampling_params={},
        raw_parse_output={"error": {"stage": "docx", "code": "INVALID_PACKAGE"}},
        raw_model_output={},
        validation_result={"valid": False},
        blockers=[{"code": "PARSE_FAILED", "locator": "损坏模板.docx"}],
        warnings=[],
        final_version_hash=None,
    )
    db.add(compilation)
    db.flush()
    artifact = _new_artifact(
        SourceArtifact,
        compilation.id,
        user.id,
        artifact_type="docx",
        file_name="损坏模板.docx",
        file_hash="0" * 64,
        file_size_bytes=128,
    )
    db.add(artifact)
    db.commit()
    compilation_id = compilation.id
    db.expire_all()

    stored = db.get(RubricCompilation, compilation_id)
    assert stored.status == "failed"
    assert stored.model_provider is None
    assert stored.model_name is None
    assert stored.blockers == [{"code": "PARSE_FAILED", "locator": "损坏模板.docx"}]
    assert stored.final_version_hash is None
    assert stored.reviewed_by is None
    assert stored.reviewed_at is None
    assert stored.published_at is None
    assert db.query(RubricVersion).filter_by(compilation_id=stored.id).count() == 0


def test_mutable_json_defaults_are_isolated_between_compilations(db):
    RubricCompilation, SourceArtifact, _, _ = _provenance_models()
    user, rubric = _add_user_and_rubric(db, "defaults")
    compilations = []
    for index in range(2):
        compilation = RubricCompilation(
            rubric_id=rubric.id,
            status="created",
            parser_version="pending",
            compiler_version="pending",
            prompt_version="pending",
            created_by=user.id,
        )
        db.add(compilation)
        db.flush()
        artifact = _new_artifact(
            SourceArtifact,
            compilation.id,
            user.id,
            file_name=f"规则-{index}.xlsx",
            file_hash=str(index) * 64,
        )
        db.add(artifact)
        compilations.append(compilation)
    db.flush()

    list_fields = ("blockers", "warnings", "human_changes")
    dict_fields = ("sampling_params", "raw_parse_output", "raw_model_output", "validation_result")
    for field in list_fields:
        first_value = getattr(compilations[0], field)
        second_value = getattr(compilations[1], field)
        assert first_value == second_value == []
        assert first_value is not second_value
    for field in dict_fields:
        first_value = getattr(compilations[0], field)
        second_value = getattr(compilations[1], field)
        assert first_value == second_value == {}
        assert first_value is not second_value

    compilation_ids = [compilation.id for compilation in compilations]
    compilations[0].blockers = [*compilations[0].blockers, {"code": "ONLY_FIRST"}]
    compilations[0].sampling_params = {**compilations[0].sampling_params, "temperature": 0.5}
    db.commit()
    db.expire_all()
    first = db.get(RubricCompilation, compilation_ids[0])
    second = db.get(RubricCompilation, compilation_ids[1])
    assert first.blockers == [{"code": "ONLY_FIRST"}]
    assert first.sampling_params == {"temperature": 0.5}
    assert second.blockers == []
    assert second.sampling_params == {}


def test_one_compilation_cannot_publish_two_versions(db):
    RubricCompilation, SourceArtifact, _, RubricVersion = _provenance_models()
    user, rubric = _add_user_and_rubric(db, "one-version")
    compilation = _new_compilation(
        RubricCompilation,
        rubric.id,
        user.id,
        final_version_hash="1" * 64,
    )
    db.add(compilation)
    db.flush()
    db.add(_new_artifact(SourceArtifact, compilation.id, user.id))
    first = RubricVersion(
        rubric_id=rubric.id,
        compilation_id=compilation.id,
        version="1.0.0",
        workflow_profile="excel_only",
        global_policy={},
        version_hash="1" * 64,
        created_by=user.id,
    )
    db.add(first)
    db.commit()
    first_id = first.id

    second = RubricVersion(
        rubric_id=rubric.id,
        compilation_id=compilation.id,
        version="1.0.1",
        workflow_profile="excel_only",
        global_policy={},
        version_hash="1" * 64,
        created_by=user.id,
    )
    db.add(second)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    assert db.get(RubricVersion, first_id) is not None


def test_version_cannot_cross_rubric_compilation_lineage(db):
    RubricCompilation, SourceArtifact, _, RubricVersion = _provenance_models()
    user, source_rubric = _add_user_and_rubric(db, "source-lineage")
    other_rubric = models.Rubric(
        name="另一套评分标准",
        version="legacy-draft",
        total_score=100,
        status="draft",
        created_by=user.id,
    )
    db.add(other_rubric)
    db.flush()
    version_hash = "3" * 64
    compilation = _new_compilation(
        RubricCompilation,
        source_rubric.id,
        user.id,
        final_version_hash=version_hash,
    )
    db.add(compilation)
    db.flush()
    db.add(_new_artifact(SourceArtifact, compilation.id, user.id))
    db.commit()

    try:
        version = RubricVersion(
            rubric_id=other_rubric.id,
            compilation_id=compilation.id,
            version="1.0.0",
            workflow_profile="excel_only",
            global_policy={},
            version_hash=version_hash,
            created_by=user.id,
        )
        db.add(version)
        db.commit()
    except (IntegrityError, ValueError):
        db.rollback()
        assert db.query(RubricVersion).filter_by(compilation_id=compilation.id).count() == 0
    else:
        db.expire_all()
        stored = db.get(RubricVersion, version.id)
        assert stored.rubric_id == source_rubric.id


def test_version_hash_must_match_compilation_final_hash(db):
    RubricCompilation, SourceArtifact, _, RubricVersion = _provenance_models()
    user, rubric = _add_user_and_rubric(db, "hash-lineage")
    compilation = _new_compilation(
        RubricCompilation,
        rubric.id,
        user.id,
        final_version_hash="4" * 64,
    )
    db.add(compilation)
    db.flush()
    db.add(_new_artifact(SourceArtifact, compilation.id, user.id))
    db.commit()

    try:
        version = RubricVersion(
            rubric_id=rubric.id,
            compilation_id=compilation.id,
            version="1.0.0",
            workflow_profile="excel_only",
            global_policy={},
            version_hash="5" * 64,
            created_by=user.id,
        )
        db.add(version)
        db.commit()
    except (IntegrityError, ValueError):
        db.rollback()
        assert db.query(RubricVersion).filter_by(compilation_id=compilation.id).count() == 0
    else:
        db.expire_all()
        stored_version = db.get(RubricVersion, version.id)
        stored_compilation = db.get(RubricCompilation, compilation.id)
        assert stored_version.version_hash == stored_compilation.final_version_hash


def test_same_source_file_can_be_reimported_in_a_new_compilation(db):
    RubricCompilation, SourceArtifact, _, _ = _provenance_models()
    user, rubric = _add_user_and_rubric(db, "reimport")
    shared_hash = "d" * 64
    for index in range(2):
        compilation = _new_compilation(
            RubricCompilation,
            rubric.id,
            user.id,
            status="failed" if index == 0 else "validated",
        )
        db.add(compilation)
        db.flush()
        artifact = _new_artifact(
            SourceArtifact,
            compilation.id,
            user.id,
            file_name="同一份规则.xlsx",
            file_hash=shared_hash,
        )
        db.add(artifact)
    db.commit()

    artifacts = db.query(SourceArtifact).filter_by(file_hash=shared_hash).all()
    assert len(artifacts) == 2
    assert len({artifact.compilation_id for artifact in artifacts}) == 2


def test_in_place_json_audit_changes_are_persisted(db):
    RubricCompilation, SourceArtifact, _, RubricVersion = _provenance_models()
    user, rubric = _add_user_and_rubric(db, "mutable-json")
    version_hash = "e" * 64
    compilation = _new_compilation(
        RubricCompilation,
        rubric.id,
        user.id,
        blockers=[],
        warnings=[],
        human_changes=[],
        sampling_params={},
        final_version_hash=version_hash,
    )
    db.add(compilation)
    db.flush()
    db.add(_new_artifact(SourceArtifact, compilation.id, user.id))
    version = RubricVersion(
        rubric_id=rubric.id,
        compilation_id=compilation.id,
        version="1.0.0",
        workflow_profile="template_driven",
        global_policy={},
        version_hash=version_hash,
        created_by=user.id,
    )
    db.add(version)
    db.commit()

    compilation.blockers.append({"code": "MISSING_MAPPING"})
    compilation.human_changes.append({"path": "/rules/0", "reason": "人工修订"})
    compilation.sampling_params["temperature"] = 0.2
    version.global_policy["rounding"] = "half_up"
    db.commit()
    compilation_id = compilation.id
    version_id = version.id
    db.expire_all()

    stored_compilation = db.get(RubricCompilation, compilation_id)
    stored_version = db.get(RubricVersion, version_id)
    assert stored_compilation.blockers == [{"code": "MISSING_MAPPING"}]
    assert stored_compilation.human_changes == [{"path": "/rules/0", "reason": "人工修订"}]
    assert stored_compilation.sampling_params == {"temperature": 0.2}
    assert stored_version.global_policy == {"rounding": "half_up"}
