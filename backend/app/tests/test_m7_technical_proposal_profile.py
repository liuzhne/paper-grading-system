from copy import deepcopy
from datetime import timedelta
from io import BytesIO
import json
from types import SimpleNamespace

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from backend.app.db import models
from backend.app.services.document_parser.extractor import ExtractedBlock
from backend.app.services.document_parser.extractor import ExtractedDocument
from backend.app.services.rubrics import lifecycle
from backend.app.services.scoring.adapters.rubric_snapshot import (
    CompiledRubricSnapshotLoader,
)
from backend.app.services.scoring.core.canonical import canonical_sha256
from backend.app.services.scoring.core.execution_plan import RuleExecutionPlanBuilder
from backend.app.services.scoring.profiles.registry import get_profile
from backend.app.services.scoring.profiles.registry import get_profile_by_key
from backend.app.tests.m2_contract_fixtures import technical_policy_snapshot_payload
from backend.app.tests.m3_contract_fixtures import scoring_request_payload
from backend.app.tests.test_m3_score_submission import _refresh_request_identity
from backend.app.tests.test_m3_score_submission import _score


PROFILE_KEY = "technical_proposal"
PROFILE_VERSION = "technical-proposal-profile@1"
REQUIRED_SECTIONS_KEY = "core.required_sections.v1"
TEXT_LENGTH_KEY = "core.text_length_range.v1"
HYBRID_REQUIRED_KEY = "generic.hybrid.required.v1"


@pytest.fixture
def db(client):
    with client.session_factory() as session:
        yield session


def _policy():
    value = technical_policy_snapshot_payload(
        total_score="80",
        rounding_digits=1,
    )
    value.pop("policy_hash")
    value["policy_key"] = "technical_proposal_release_policy"
    value["grade_scale"] = {
        "basis": "raw_score",
        "bands": [
            {"label": "Gold", "minimum": "68"},
            {"label": "Qualified", "minimum": "48"},
            {"label": "Rework", "minimum": "0"},
        ],
    }
    value["review"]["total_below"] = "48"
    value["review"]["grade_boundary_tolerance"] = {
        "value": "1",
        "unit": "raw_score_points",
    }
    value["policy_hash"] = canonical_sha256(value)
    return value


def _complete_extraction():
    blocks = []

    def heading(text):
        blocks.append(
            ExtractedBlock(
                page=1,
                ordinal=len(blocks),
                kind="paragraph",
                text=text,
                style_name="Heading 1",
            )
        )

    def paragraph(text):
        blocks.append(
            ExtractedBlock(
                page=1,
                ordinal=len(blocks),
                kind="paragraph",
                text=text,
                style_name="Normal",
            )
        )

    def row(*cells):
        blocks.append(
            ExtractedBlock(
                page=1,
                ordinal=len(blocks),
                kind="table_row",
                text=" | ".join(cells),
                table_index=0,
                row_index=len(blocks),
                cell_texts=tuple(cells),
            )
        )

    heading("需求理解")
    paragraph("系统需要支撑高并发交易、十五分钟故障恢复和全年可用性目标。")
    heading("总体方案")
    paragraph("采用双可用区服务和异步消息架构，逐项对应吞吐、恢复和可用性需求。")
    heading("实施计划")
    row("里程碑", "截止日期", "交付物", "负责人")
    row("架构评审", "2026-09-01", "评审纪要", "项目经理")
    heading("风险控制")
    row("风险", "概率", "影响", "措施", "负责人")
    row("容量不足", "中", "延期", "压测并扩容", "技术经理")
    heading("服务承诺")
    paragraph("提供 7x24 支持，重大故障十五分钟响应并持续通报。")
    return ExtractedDocument(
        schema_version="extracted-document@1",
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        source_suffix=".docx",
        blocks=tuple(blocks),
        heading_candidates=(
            "需求理解",
            "总体方案",
            "实施计划",
            "风险控制",
            "服务承诺",
        ),
    )


def _submission(metadata=None):
    return SimpleNamespace(
        id="proposal-submission-1",
        source_artifact_hash="a" * 64,
        submission_metadata=metadata
        or {
            "proposal_id": "TP-2026-001",
            "vendor_name": "Acme Solutions",
            "project_name": "智能交易平台",
        },
    )


def _rules_xlsx():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Technical Proposal Rules"
    headers = [
        "原子规则编号",
        "评分项编号",
        "评分项",
        "分值",
        "类型",
        "评分模式",
        "评分说明",
        "扣分规则",
        "适用范围",
        "分档",
        "checker_key",
        "checker_params",
        "evidence_policy",
        "effect_type",
        "repeat_policy",
        "子检查",
    ]
    sheet.append(headers)
    sheet.append(
        [
            "proposal.structure.v1",
            "STRUCTURE",
            "方案章节完整性",
            10,
            "deterministic",
            "deductive",
            "必须包含用户发布时声明的五个方案章节。",
            "缺失必需章节扣5分",
            "global",
            None,
            REQUIRED_SECTIONS_KEY,
            json.dumps(
                {
                    "required_sections": [
                        "需求理解",
                        "总体方案",
                        "实施计划",
                        "风险控制",
                        "服务承诺",
                    ]
                },
                ensure_ascii=False,
            ),
            None,
            "score",
            "once",
            None,
        ]
    )
    sheet.append(
        [
            "proposal.length.v1",
            "LENGTH",
            "有效内容长度",
            10,
            "deterministic",
            "deductive",
            "长度上下限完全来自 checker_params。",
            "有效内容长度越界扣5分",
            "global",
            None,
            TEXT_LENGTH_KEY,
            json.dumps({"minimum_chars": 80, "maximum_chars": 20000}),
            None,
            "score",
            "once",
            None,
        ]
    )
    sheet.append(
        [
            "proposal.solution_alignment.v1",
            "SOLUTION_ALIGNMENT",
            "需求与总体方案匹配度",
            20,
            "semantic",
            "banded",
            "总体方案应逐项回应需求理解。",
            None,
            "需求理解|总体方案",
            "ALIGN_HIGH:20;ALIGN_MEDIUM:12;ALIGN_LOW:5",
            None,
            None,
            None,
            "score",
            None,
            None,
        ]
    )
    sheet.append(
        [
            "proposal.risk_fields.v1",
            "RISK_CONTROL",
            "风险项字段完整性",
            15,
            "semantic",
            "deductive",
            "每个风险项应包含概率、影响、措施和负责人。",
            "风险项每缺一个字段扣3分",
            "风险控制",
            None,
            None,
            None,
            json.dumps(
                {
                    "mode": "source_quote",
                    "requirement": "required",
                    "minimum_coverage": "1",
                }
            ),
            "score",
            "per_occurrence",
            None,
        ]
    )
    sheet.append(
        [
            "IMPLEMENTATION",
            "IMPLEMENTATION",
            "实施交付",
            25,
            "hybrid",
            "hybrid",
            "编译时必须展开为两个独立叶子。",
            None,
            "实施计划",
            None,
            None,
            None,
            None,
            None,
            None,
            (
                "Milestone completeness | deterministic | 10\n"
                "Implementation feasibility | semantic | 15"
            ),
        ]
    )
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _import_command():
    return {
        "schema_version": "rubric-import-command@2",
        "source_kind": "file_import",
        "rubric": {
            "name": "Technical proposal production fixture",
            "version": "v1",
            "description": "M7 production Profile acceptance rubric.",
            "business_profile_key": PROFILE_KEY,
            "workflow_profile": "template_driven",
            "global_policy": _policy(),
        },
        "files": {
            "rules_file_name": "technical-proposal-rules.xlsx",
            "template_file_name": None,
        },
        "compiler": {
            "parser_version": "rubric-file-parser@2",
            "compiler_version": "atomic-rule-compiler@1",
            "prompt_version": "rubric-compilation-prompt@2",
            "model_provider": None,
            "model_name": None,
            "sampling_params": {},
        },
        "version": {
            "version": "v1",
            "hash_scheme": "rubric-content-v2",
        },
    }


def test_production_registry_resolves_exact_technical_proposal_profile():
    profile = get_profile_by_key(PROFILE_KEY)

    assert profile.profile_key == PROFILE_KEY
    assert profile.profile_version == PROFILE_VERSION
    assert get_profile(
        profile_key=PROFILE_KEY,
        profile_version=PROFILE_VERSION,
    ).profile_version == PROFILE_VERSION
    with pytest.raises(ValueError, match="version mismatch"):
        get_profile(
            profile_key=PROFILE_KEY,
            profile_version="rolling-latest",
        )


def test_profile_interprets_generic_document_and_rejects_metadata_leakage():
    profile = get_profile_by_key(PROFILE_KEY)
    document = profile.interpret_document(
        extracted_document=_complete_extraction(),
        submission=_submission(),
    ).to_mapping()

    assert document["profile_key"] == PROFILE_KEY
    assert document["profile_version"] == PROFILE_VERSION
    assert [item["heading"] for item in document["sections"]] == [
        "需求理解",
        "总体方案",
        "实施计划",
        "风险控制",
        "服务承诺",
    ]
    extension = document["profile_extensions"][PROFILE_KEY]
    assert extension["milestone_complete"] is True
    assert extension["risk_items"] == [
        {
            "item_ordinal": 0,
            "fields_present": [
                "impact",
                "mitigation",
                "owner",
                "probability",
            ],
            "source_text": "容量不足 | 中 | 延期 | 压测并扩容 | 技术经理",
        }
    ]
    assert "student_id" not in json.dumps(document, ensure_ascii=False)
    assert "thesis" not in json.dumps(document, ensure_ascii=False).lower()

    with pytest.raises(ValueError, match="metadata"):
        profile.interpret_document(
            extracted_document=_complete_extraction(),
            submission=_submission(
                {
                    "proposal_id": "TP-2026-001",
                    "vendor_name": "Acme Solutions",
                    "project_name": "智能交易平台",
                    "student_id": "must-not-leak",
                }
            ),
        )


def test_profile_checkers_are_parameter_driven_and_profile_scoped():
    profile = get_profile_by_key(PROFILE_KEY)
    document = profile.interpret_document(
        extracted_document=_complete_extraction(),
        submission=_submission(),
    ).to_mapping()
    registry = profile.build_checker_registry()
    manifest = registry.manifest(
        profile_key=PROFILE_KEY,
        document_schema_version="document-snapshot@1",
    )
    assert {REQUIRED_SECTIONS_KEY, TEXT_LENGTH_KEY, HYBRID_REQUIRED_KEY}.issubset(
        manifest
    )

    required = registry.resolve(
        checker_key=REQUIRED_SECTIONS_KEY,
        checker_version="1.0.0",
        checker_params={"required_sections": ["需求理解", "服务承诺"]},
        profile_key=PROFILE_KEY,
        document_schema_version="document-snapshot@1",
    )
    assert required(
        document=document,
        params={"required_sections": ["需求理解", "服务承诺"]},
    )["observations"][0]["status"] == "not_triggered"

    length = registry.resolve(
        checker_key=TEXT_LENGTH_KEY,
        checker_version="1.0.0",
        checker_params={"minimum_chars": 1, "maximum_chars": 10},
        profile_key=PROFILE_KEY,
        document_schema_version="document-snapshot@1",
    )
    assert length(
        document=document,
        params={"minimum_chars": 1, "maximum_chars": 10},
    )["observations"][0]["status"] == "triggered"
    with pytest.raises(ValueError, match="does not support profile"):
        registry.resolve(
            checker_key=REQUIRED_SECTIONS_KEY,
            checker_version="1.0.0",
            checker_params={"required_sections": ["摘要"]},
            profile_key="thesis",
            document_schema_version="document-snapshot@1",
        )


def test_policy_review_boundary_becomes_a_core_review_issue():
    request = scoring_request_payload()
    policy = request["plan"]["policy_snapshot"]
    policy.pop("policy_hash")
    policy["review"]["total_below"] = "95"
    policy["policy_hash"] = canonical_sha256(policy)
    _refresh_request_identity(request)

    result = _score(request=request).to_mapping()

    assert result["final_total"] == "90"
    assert {item["code"] for item in result["review_issues"]} == {
        "POLICY_REVIEW_REQUIRED"
    }


def test_excel_hybrid_import_can_be_reviewed_published_and_planned(db):
    from backend.app.services.dev_user import ensure_dev_user
    from backend.app.services.rubric_import.pipeline import prepare_file_import
    from backend.app.services.rubric_import.pipeline import persist_prepared_import

    actor = ensure_dev_user(db)
    actor_id = actor.id
    db.commit()
    prepared = prepare_file_import(
        command=_import_command(),
        rules_bytes=_rules_xlsx(),
    )
    graph = prepared.to_mapping()
    leaves = [
        item
        for item in graph["criteria"]
        if item.get("parent_criterion_code") == "IMPLEMENTATION"
    ]
    assert len(leaves) == 2
    assert {item["scoring_mode"] for item in leaves} == {
        "deductive",
        "banded",
    }
    assert all(item["scoring_mode"] != "llm_direct" for item in graph["criteria"])
    assert sum(float(item["max_score"]) for item in graph["criteria"]) == 80

    identity = persist_prepared_import(
        session=db,
        prepared=prepared,
        actor_id=actor_id,
    )
    rules = db.scalars(
        select(models.AtomicRule)
        .where(models.AtomicRule.rubric_version_id == identity.rubric_version_id)
        .order_by(models.AtomicRule.rule_code)
    ).all()
    start = max(rule.created_at for rule in rules) + timedelta(minutes=1)
    for index, rule in enumerate(rules):
        submitted_at = start + timedelta(minutes=index * 2)
        lifecycle.submit_atomic_rule_for_review(
            db,
            identity.rubric_id,
            rule.rule_code,
            actor_id,
            "规则来源和执行映射已核对。",
            now=submitted_at,
        )
        db.commit()
        lifecycle.approve_atomic_rule(
            db,
            identity.rubric_id,
            rule.rule_code,
            actor_id,
            "批准进入技术方案正式评分标准。",
            now=submitted_at + timedelta(minutes=1),
        )
        db.commit()

    lifecycle.submit_for_review(db, identity.rubric_id)
    db.commit()
    published = lifecycle.publish_rubric(
        db,
        identity.rubric_id,
        identity.compilation_id,
        actor_id,
        now=start + timedelta(hours=2),
    )
    db.commit()

    profile = get_profile_by_key(PROFILE_KEY)
    snapshot = CompiledRubricSnapshotLoader().load_from_session(
        session=db,
        rubric_version_id=published.id,
        expected_profile_key=PROFILE_KEY,
    )
    plan = RuleExecutionPlanBuilder(
        checker_registry=profile.build_checker_registry(),
        policy_compiler_version="scoring-policy-compiler@1",
        engine_contract_version="scoring-core@1",
    ).build(
        rubric=snapshot,
        profile=profile,
        document_schema_version="document-snapshot@1",
    ).to_mapping()

    assert plan["business_profile_key"] == PROFILE_KEY
    assert plan["business_profile_version"] == PROFILE_VERSION
    assert plan["policy_snapshot"] == _policy()
    assert set(plan["checker_manifest"]) == {
        REQUIRED_SECTIONS_KEY,
        TEXT_LENGTH_KEY,
        HYBRID_REQUIRED_KEY,
    }
    assert all(
        node["atomic_rule_snapshot"]["schema_version"]
        == "atomic-rule-snapshot@2"
        for node in plan["nodes"]
    )
    assert all(rule.status == "approved" for rule in rules)
