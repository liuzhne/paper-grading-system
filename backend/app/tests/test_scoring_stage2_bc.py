"""第二阶段第二轮：快照 @4、人工指定判定范围、复核原因（docs/论文评分第二阶段改造方案.md 第 13 节）。

全部用合成数据、内存 SQLite 与假运行时，不调用真实模型。
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.db import models
from backend.app.services.rubrics.review_workspace import content_token
from backend.app.services.scoring.adapters.persistence import CoreRunPersistence
from backend.app.services.scoring.adapters.rubric_snapshot import CompiledRubricSnapshotLoader
from backend.app.services.scoring.core import engine as core_engine
from backend.app.services.scoring.core.contracts import AtomicRuleSnapshot
from backend.app.services.scoring.retrieval import judge_scope
from backend.app.services.scoring.review_reasons import from_core_issues
from backend.app.tests.test_m4_execution_persistence import _DocumentSnapshotStore
from backend.app.tests.test_m4_execution_persistence import _database_graph
from backend.app.tests.test_m4_rule_executor import _CheckerRegistry
from backend.app.tests.test_m4_rule_executor import _SemanticRuntime
from backend.app.tests.test_m4_rule_executor import _TechnicalProposalProfile
from backend.app.tests.test_m4_rule_executor import _criterion
from backend.app.tests.test_m4_rule_executor import _deduct_rule
from backend.app.tests.test_m4_rule_executor import _none_rule
from backend.app.tests.test_m4_rule_executor import _plain
from backend.app.tests.test_m4_rule_executor import _quote_occurrence
from backend.app.tests.test_m4_rule_executor import _request_for
from backend.app.tests.test_m4_rule_executor import _semantic_response


def _create(client):
    response = client.post("/api/rubrics", json={
        "name": "合成判定范围", "version": "v1", "total_score": 20,
        "criteria": [
            {"code": "T01", "name": "设计", "max_score": 10, "scoring_mode": "deductive",
             "deduction_rules_structured": [
                 # 手动录入的规则：名称是“评分项名 #序号”，条款正文取 reason。
                 {"match": "合成条款一", "points": 2, "reason": "数据库设计缺少说明"},
                 {"match": "合成条款二", "points": 3, "reason": "研究问题在结论中没有回应"},
             ]},
            {"code": "T02", "name": "规范", "max_score": 10, "scoring_mode": "deductive",
             "deduction_rules_structured": [
                 {"match": "合成条款三", "points": 1, "reason": "引用格式不统一"},
             ]},
        ],
    })
    assert response.status_code == 200, response.text
    return response.json()


def _workspace(client, rubric_id):
    response = client.get(f"/api/rubrics/{rubric_id}/review-workspace")
    assert response.status_code == 200, response.text
    return response.json()


def _publish(client, rubric_id):
    view = _workspace(client, rubric_id)
    for rule in view["rules"]:
        if rule["status"] in {"draft", "review"}:
            response = client.post(
                f"/api/rubrics/{rubric_id}/rules/{rule['rule_code']}/confirm",
                json={"compilation_id": view["compilation_id"], "rule_id": rule["id"],
                      "content_token": rule["content_token"], "reason": "核对合成条款"},
            )
            assert response.status_code == 200, response.text
    assert client.post(f"/api/rubrics/{rubric_id}/submit-review").status_code == 200
    published = client.post(f"/api/rubrics/{rubric_id}/publish", json={
        "compilation_id": view["compilation_id"], "reason": "合成发布"})
    assert published.status_code == 200, published.text


def _first_rule(session, rubric_id):
    return session.scalars(
        select(models.AtomicRule)
        .join(models.RubricVersion, models.AtomicRule.rubric_version_id == models.RubricVersion.id)
        .where(models.RubricVersion.rubric_id == rubric_id)
        .order_by(models.AtomicRule.rule_code)
    ).first()


def _load(client, rubric_id):
    with client.session_factory() as session:
        version = session.scalar(
            select(models.RubricVersion).where(models.RubricVersion.rubric_id == rubric_id)
        )
        return CompiledRubricSnapshotLoader().load_from_session(
            session=session,
            rubric_version_id=version.id,
            expected_profile_key=version.business_profile_key,
        ).to_mapping()


# ── 快照 @4 ────────────────────────────────────────────────────────


def test_snapshot_carries_applies_to_and_scope_only_while_the_switch_is_on(client, monkeypatch):
    rubric = _create(client)
    with client.session_factory() as session:
        rule = _first_rule(session, rubric["id"])
        rule.applies_to = "第3章 系统设计"
        rule.evidence_policy = {**(rule.evidence_policy or {}), "judge_scope": "section"}
        code = rule.rule_code
        session.commit()
    _publish(client, rubric["id"])

    off = {item["rule_code"]: item for item in _load(client, rubric["id"])["atomic_rules"]}
    assert off[code]["schema_version"] == "atomic-rule-snapshot@3"
    assert "applies_to" not in off[code]
    assert "judge_scope" not in off[code]["evidence_policy"]

    monkeypatch.setattr(settings, "SCORING_JUDGE_SCOPE_ENABLED", True)
    on_snapshot = _load(client, rubric["id"])
    on = {item["rule_code"]: item for item in on_snapshot["atomic_rules"]}
    assert on[code]["schema_version"] == "atomic-rule-snapshot@4"
    assert on[code]["applies_to"] == "第3章 系统设计"
    assert on[code]["evidence_policy"]["judge_scope"] == "section"
    assert {item["applies_to"] for code_, item in on.items() if code_ != code} == {"global"}
    assert judge_scope.infer_judge_scope(on[code]) == {"scope": "section", "targets": ["第3章 系统设计"]}
    # 同一已发布版本：版本哈希不变，只是快照（与判定身份）不同。
    assert on_snapshot["version_hash"] == _load(client, rubric["id"])["version_hash"]
    monkeypatch.setattr(settings, "SCORING_JUDGE_SCOPE_ENABLED", False)
    assert _load(client, rubric["id"])["rubric_snapshot_hash"] != on_snapshot["rubric_snapshot_hash"]


def test_snapshot_contract_rejects_an_unknown_judge_scope():
    value = {
        "schema_version": "atomic-rule-snapshot@4",
        "rule_code": "R1", "criterion_code": "T01", "direction": "deduct", "effect_type": "score",
        "judge_type": "semantic", "checker_key": None, "checker_version": None, "checker_params": {},
        "evidence_policy": {"mode": "source_quote", "requirement": "required", "minimum_coverage": "1",
                            "allowed_finding_codes": ["R1"], "judge_scope": "chapter"},
        "max_points": "2", "repeat_policy": "once", "cap_points": None, "depends_on_rule_codes": [],
        "mutex_group": None, "levels": [], "name": "规则", "rule_text": "规则", "positive_example": None,
        "negative_example": None, "boundary_example": None, "context_needs": [], "applies_to": "global",
    }
    with pytest.raises(ValueError, match="judge_scope"):
        AtomicRuleSnapshot.from_mapping(value)
    value["evidence_policy"]["judge_scope"] = "section"
    assert AtomicRuleSnapshot.from_mapping(value).to_mapping()["evidence_policy"]["judge_scope"] == "section"
    # @3 仍是闭合契约：不认识的键照旧拒绝。
    value["schema_version"] = "atomic-rule-snapshot@3"
    value.pop("applies_to")
    with pytest.raises(ValueError):
        AtomicRuleSnapshot.from_mapping(value)


# ── 推断与页面 ──────────────────────────────────────────────────────


SEMANTIC = {"judge_type": "semantic"}


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ({**SEMANTIC, "evidence_policy": {"judge_scope": "document"}, "applies_to": "绪论"}, ("document", [], "explicit")),
        ({**SEMANTIC, "evidence_policy": {"judge_scope": "section"}, "applies_to": "第3章 系统设计"}, ("section", ["第3章 系统设计"], "explicit")),
        # 指定了范围但没写适用范围：章节取规则文字里认出的角色。
        ({**SEMANTIC, "evidence_policy": {"judge_scope": "section"}, "name": "结论部分过于简略"}, ("section", ["结论"], "explicit")),
        ({**SEMANTIC, "evidence_policy": {"judge_scope": "cross_section"}, "name": "语言表达"}, ("document", [], "explicit")),
        ({**SEMANTIC, "applies_to": "系统设计"}, ("section", ["系统设计"], "applies_to")),
        ({**SEMANTIC, "name": "研究问题在结论中没有回应"}, ("cross_section", ["绪论", "结论"], "wording")),
        ({**SEMANTIC, "name": "语言表达不规范"}, ("document", [], "default")),
        ({"judge_type": "deterministic", "applies_to": "绪论"}, ("document", [], "deterministic")),
    ],
)
def test_explain_judge_scope(rule, expected):
    explained = judge_scope.explain_judge_scope(rule)
    assert (explained["scope"], explained["targets"], explained["source"]) == expected


def test_review_hint_points_section_existence_rules_to_the_structure_checker():
    assert judge_scope.review_hint({**SEMANTIC, "name": "缺少结论章", "rule_text": ""})
    assert judge_scope.review_hint({**SEMANTIC, "name": "缺少论证依据", "rule_text": ""}) is None
    assert judge_scope.review_hint({"judge_type": "deterministic", "name": "缺少结论章"}) is None
    # 已经按章节取证据的，不再提示。
    assert judge_scope.review_hint({**SEMANTIC, "name": "缺少结论章", "applies_to": "结论"}) is None


def test_review_workspace_shows_scope_without_changing_the_content_token(client):
    rubric = _create(client)
    rules = {rule["rule_code"]: rule for rule in _workspace(client, rubric["id"])["rules"]}
    cross = next((rule for rule in rules.values() if "研究问题" in rule["name"] + rule["rule_text"]), None)
    assert cross is not None, [(rule["name"], rule["rule_text"], rule["judge_type"]) for rule in rules.values()]

    assert cross["judge_scope"] == {"scope": "cross_section", "targets": ["绪论", "结论"], "source": "wording"}
    assert {rule["judge_scope_hint"] for rule in rules.values()} == {None}
    with client.session_factory() as session:
        row = session.scalar(select(models.AtomicRule).where(models.AtomicRule.id == cross["id"]))
        assert cross["content_token"] == content_token(row)


def test_rule_edit_validates_and_clears_judge_scope(client):
    rubric = _create(client)
    rule = _workspace(client, rubric["id"])["rules"][0]
    policy = dict(rule["evidence_policy"] or {})

    # 不可哈希的值（列表、对象）也要回 400，不能在成员判断时抛 TypeError 变成 500。
    for value in ("chapter", ["section"], {"scope": "section"}):
        bad = client.patch(f"/api/rubrics/{rubric['id']}/rules/{rule['rule_code']}",
                           json={"changes": {"evidence_policy": {**policy, "judge_scope": value}}, "reason": "合成修改"})
        assert bad.status_code == 400 and "judge_scope" in bad.json()["detail"], (value, bad.text)

    ok = client.patch(f"/api/rubrics/{rubric['id']}/rules/{rule['rule_code']}",
                      json={"changes": {"evidence_policy": {**policy, "judge_scope": "section"}}, "reason": "合成修改"})
    assert ok.status_code == 200, ok.text
    edited = next(item for item in _workspace(client, rubric["id"])["rules"] if item["rule_code"] == rule["rule_code"])
    assert edited["evidence_policy"]["judge_scope"] == "section"
    assert edited["judge_scope"]["source"] == "explicit"

    cleared = client.patch(f"/api/rubrics/{rubric['id']}/rules/{rule['rule_code']}",
                           json={"changes": {"evidence_policy": {**policy, "judge_scope": None}}, "reason": "合成修改"})
    assert cleared.status_code == 200, cleared.text
    edited = next(item for item in _workspace(client, rubric["id"])["rules"] if item["rule_code"] == rule["rule_code"])
    assert "judge_scope" not in edited["evidence_policy"]


# ── 复核原因 ────────────────────────────────────────────────────────


def test_core_issues_become_structured_review_reasons():
    reasons = from_core_issues(
        [
            {"code": "REVIEW_REQUIRED", "severity": "review", "criterion_code": "T01", "rule_code": "R2", "message": "x"},
            {"code": "RULE_EVIDENCE_INSUFFICIENT", "severity": "review", "criterion_code": "T01", "rule_code": "R1", "message": "x"},
            {"code": "REPORT_ONLY", "severity": "info", "criterion_code": "T01", "rule_code": "R3", "message": "x"},
        ],
        rule_names={"R1": "研究方法描述不清"},
    )
    assert [(item["code"], item["rule_code"]) for item in reasons] == [
        ("evidence_insufficient", "R1"),
        ("rule_requires_review", "R2"),
    ]
    assert "研究方法描述不清" in reasons[0]["message"]
    assert "R2" in reasons[1]["message"]


def test_core_persistence_keeps_the_auto_score_and_explains_insufficient_evidence():
    criterion_snapshot = _criterion()
    rule = _deduct_rule()
    request = _request_for((criterion_snapshot, rule))
    outcome = core_engine.score_submission(
        request=deepcopy(request),
        checker_registry=_CheckerRegistry(),
        llm_runtime=_SemanticRuntime(
            {rule["rule_code"]: _semantic_response(rule["rule_code"], status="insufficient_evidence")}
        ),
        profile=_TechnicalProposalProfile(),
    )
    assert _plain(outcome)["status"] == "review_required"

    engine, db, rubric, criterion, paper = _database_graph(request)
    try:
        store = _DocumentSnapshotStore()
        run = CoreRunPersistence(db, document_snapshot_store=store).persist(
            request=deepcopy(request),
            outcome=outcome,
            paper_id=paper.id,
            rubric_id=rubric.id,
            criterion_id_by_code={criterion.code: criterion.id},
            workflow_profile="template_driven",
            document_snapshot_ref=store.put(request["document"]),
        )
        item = run.items[0]
        # 自动分保留、最终分留空：普通改分可以处理，不会变成改不了分的 blocked。
        assert item.auto_score_status == "calculated"
        assert item.ai_score is not None and item.final_score is None
        assert item.need_manual_review is True
        assert [reason["code"] for reason in item.review_reasons] == ["evidence_insufficient"]
        assert item.review_reasons[0]["rule_code"] == rule["rule_code"]
        assert "证据没有覆盖" in item.review_reason
    finally:
        db.close()
        engine.dispose()


def _persist_single(request, responses):
    outcome = core_engine.score_submission(
        request=deepcopy(request),
        checker_registry=_CheckerRegistry(),
        llm_runtime=_SemanticRuntime(responses),
        profile=_TechnicalProposalProfile(),
    )
    engine, db, rubric, criterion, paper = _database_graph(request)
    try:
        store = _DocumentSnapshotStore()
        run = CoreRunPersistence(db, document_snapshot_store=store).persist(
            request=deepcopy(request),
            outcome=outcome,
            paper_id=paper.id,
            rubric_id=rubric.id,
            criterion_id_by_code={criterion.code: criterion.id},
            workflow_profile="template_driven",
            document_snapshot_ref=store.put(request["document"]),
        )
        item = run.items[0]
        return item.review_reasons, item.review_reason
    finally:
        db.close()
        engine.dispose()


def test_core_persistence_explains_review_rules_and_leaves_plain_items_without_reasons():
    rule = _none_rule(code="proposal.review.v1", effect_type="review")
    request = _request_for((_criterion(mode="review_only"), rule))
    reasons, text = _persist_single(
        request,
        {rule["rule_code"]: _semantic_response(rule["rule_code"], occurrences=[_quote_occurrence(request)])},
    )
    assert [reason["code"] for reason in reasons] == ["rule_requires_review"]
    assert "要求人工复核" in text

    plain = _deduct_rule()
    reasons, text = _persist_single(
        _request_for((_criterion(), plain)),
        {plain["rule_code"]: _semantic_response(plain["rule_code"], status="not_triggered")},
    )
    assert reasons is None and text is None


def test_section_scope_from_applies_to_selects_only_that_section(monkeypatch):
    from backend.app.services.llm import core_view
    from backend.app.services.scoring.retrieval.selection import build_v4_envelope
    from backend.app.tests.test_scoring_stage2 import BUDGET
    from backend.app.tests.test_scoring_stage2 import _envelope
    from backend.app.tests.test_scoring_stage2 import _base_mapping
    from backend.app.tests.test_scoring_stage2 import _paper_units
    import json

    content_hash = _base_mapping()["evidence_units"][0]["normalized_content_hash"]
    rule = {
        "schema_version": "atomic-rule-snapshot@4",
        "applies_to": "第三章 系统设计",
        "name": "数据库设计说明不足",
        "rule_text": "没有给出表结构与字段含义",
    }
    envelope = _envelope(units=_paper_units(content_hash), rule=rule)
    value = build_v4_envelope(envelope, top_k=12, judge_scope=True, **BUDGET).to_mapping()

    assert value["coverage_mode"] == "section"
    assert {unit["section_path"][0] for unit in value["evidence_units"]} == {"第3章 系统设计"}

    monkeypatch.setattr(settings, "SCORING_JUDGE_SCOPE_ENABLED", True)
    view = json.loads(core_view.build_core_request(build_v4_envelope(envelope, top_k=12, judge_scope=True, **BUDGET)).user)
    assert view["scope"] == {"type": "section", "targets": [{"name": "第三章 系统设计", "evidence": 3}]}
    assert view["rule"]["name"] == "数据库设计说明不足"
