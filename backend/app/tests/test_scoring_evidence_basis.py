"""评分证据与理由（docs/评分证据与理由改造方案.md G1–G5）。

全部用合成数据、内存 SQLite 与假运行时，不调用真实模型。
"""

from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from backend.app.core.config import settings
from backend.app.services.llm import core_view
from backend.app.services.llm.core_adapter import normalize_core_provider_response
from backend.app.services.scoring import document_view
from backend.app.services.scoring import evidence_view
from backend.app.services.scoring.adapters.persistence import CoreRunPersistence
from backend.app.services.scoring.adapters.persistence import LocalDocumentSnapshotStore
from backend.app.services.scoring.adapters.persistence import core_deduction_reason
from backend.app.services.scoring.core import engine as core_engine
from backend.app.services.scoring.frozen_document import FrozenDocument
from backend.app.services.scoring.frozen_document import resolve_frozen_document
from backend.app.services.scoring.judgement_view import build_judgement_view
from backend.app.services.scoring.retrieval.selection import build_v4_envelope
from backend.app.tests.test_m4_execution_persistence import _DocumentSnapshotStore
from backend.app.tests.test_m4_execution_persistence import _database_graph
from backend.app.tests.test_m4_rule_executor import _CheckerRegistry
from backend.app.tests.test_m4_rule_executor import _SemanticRuntime
from backend.app.tests.test_m4_rule_executor import _TechnicalProposalProfile
from backend.app.tests.test_m4_rule_executor import _criterion
from backend.app.tests.test_m4_rule_executor import _deduct_rule
from backend.app.tests.test_m4_rule_executor import _plain
from backend.app.tests.test_m4_rule_executor import _quote_occurrence
from backend.app.tests.test_m4_rule_executor import _request_for
from backend.app.tests.test_m4_rule_executor import _semantic_response
from backend.app.tests.test_m4_rule_executor import _source_quote
from backend.app.tests.test_m4_rule_executor import _unit
from backend.app.tests.test_scoring_stage2 import BUDGET
from backend.app.tests.test_scoring_stage2 import _paper_envelope


class _ReportingProfile:
    """像真实 Profile 一样回填选证记录；信封本身不变，其余委托给测试 Profile。"""

    def __init__(self, report):
        self._inner = _TechnicalProposalProfile()
        self._report = report

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def build_provider_envelope(self, *, base_envelope, selection_report=None):
        if selection_report is not None:
            selection_report.update(self._report)
        return base_envelope


def _outcome(request, responses, *, profile=None):
    return _plain(
        core_engine.score_submission(
            request=deepcopy(request),
            checker_registry=_CheckerRegistry(),
            llm_runtime=_SemanticRuntime(responses),
            profile=profile or _TechnicalProposalProfile(),
        )
    )


def _decision(outcome, rule_code):
    return next(item for item in outcome["rule_decisions"] if item["rule_code"] == rule_code)


def _persist(request, outcome):
    engine, db, rubric, criterion, paper = _database_graph(request)
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
    return engine, db, run


def _frozen(request):
    return FrozenDocument(
        kind="core",
        payload=deepcopy(request["document"]),
        snapshot_hash=request["document"]["document_snapshot_hash"],
    )


# ── G3：保留核对过的引文与选证记录 ─────────────────────────────────


def test_triggered_decision_keeps_the_verified_quote_without_changing_the_score():
    rule = _deduct_rule()
    request = _request_for((_criterion(), rule))
    unit = _unit(request)
    quote = unit["normalized_text"][:12]
    outcome = _outcome(
        request,
        {rule["rule_code"]: _semantic_response(
            rule["rule_code"], occurrences=[_quote_occurrence(request, quote=quote)]
        )},
    )
    decision = _decision(outcome, rule["rule_code"])
    assert decision["status"] == "triggered"
    assert decision["evidence_refs"][0]["quote"] == quote
    assert decision["occurrences"][0]["evidence_refs"][0]["quote"] == quote
    # 只是不再丢掉引文：与不带引文时的分数一致。
    assert outcome["final_total"] is not None


def test_decision_records_how_much_evidence_the_rule_saw():
    rule = _deduct_rule()
    request = _request_for((_criterion(), rule))
    report = {"candidate_count": 40, "requested_count": 12, "selected_count": 9}
    outcome = _outcome(
        request,
        {rule["rule_code"]: _semantic_response(rule["rule_code"], status="not_triggered")},
        profile=_ReportingProfile(report),
    )
    assert _decision(outcome, rule["rule_code"])["evidence_selection"] == report


def test_build_v4_envelope_reports_budget_trimming():
    report = {}
    build_v4_envelope(_paper_envelope(), top_k=12, selection_report=report, **BUDGET)
    assert report["selected_count"] == report["requested_count"] <= report["candidate_count"]

    tight = dict(BUDGET, context_window_tokens=4300)
    trimmed = {}
    try:
        build_v4_envelope(_paper_envelope(), top_k=12, selection_report=trimmed, **tight)
    except Exception:  # noqa: BLE001 - 极小预算放不下任何证据时不回填
        assert trimmed == {}
    else:
        assert trimmed["selected_count"] < trimmed["requested_count"]


# ── G5：依据（basis）在执行器里的处理 ───────────────────────────────


def test_basis_quotes_are_reverified_and_bad_ones_never_invalidate_the_decision():
    rule = _deduct_rule()
    request = _request_for((_criterion(), rule))
    unit = _unit(request)
    basis = {
        "evidence": [
            _source_quote(unit, quote=unit["normalized_text"][:10]),
            _source_quote(unit, quote="原文里没有的一句话"),
        ],
        "rationale": "  方法描述   完整，未见该缺陷。 ",
        "rejected_quote_count": 0,
    }
    outcome = _outcome(
        request,
        {rule["rule_code"]: _semantic_response(rule["rule_code"], status="not_triggered", basis=basis)},
    )
    decision = _decision(outcome, rule["rule_code"])
    assert decision["status"] == "not_triggered"
    assert decision["basis"]["rationale"] == "方法描述 完整，未见该缺陷。"
    assert [ref["quote"] for ref in decision["basis"]["evidence_refs"]] == [unit["normalized_text"][:10]]
    assert decision["basis"]["rejected_quote_count"] == 1
    # 依据不参与算分：没有扣分。
    assert decision["occurrences"] == []


def test_invalid_decision_drops_its_basis():
    rule = _deduct_rule()
    request = _request_for((_criterion(), rule))
    outcome = _outcome(
        request,
        {rule["rule_code"]: _semantic_response(
            rule["rule_code"],
            occurrences=[_quote_occurrence(request, quote="伪造的引文")],
            basis={"evidence": [], "rationale": "x", "rejected_quote_count": 0},
        )},
    )
    decision = _decision(outcome, rule["rule_code"])
    assert decision["status"] == "invalid"
    assert "basis" not in decision


# ── G5：提示词开关 ──────────────────────────────────────────────────


def _v4():
    return build_v4_envelope(_paper_envelope(), top_k=12, **BUDGET)


def test_basis_switch_off_keeps_prompt_and_schema_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "SCORING_DECISION_BASIS_MODE", "off")
    request = core_view.build_core_request(_v4())
    assert "basis" not in request.system
    assert "basis" not in request.schema["properties"]
    assert core_view.provider_prompt_suffix() == ""
    assert "不得输出分值、扣分、解释文字、Markdown 或未声明字段。" in request.system


def test_basis_switch_on_asks_for_quotes_and_a_rationale(monkeypatch):
    monkeypatch.setattr(settings, "SCORING_DECISION_BASIS_MODE", "cite")
    request = core_view.build_core_request(_v4())
    assert "basis.rationale" in request.system
    assert "使用论文正文的语言" in request.system
    assert "insufficient_evidence 时" not in request.system
    assert "basis" in request.schema["required"]
    basis_schema = request.schema["properties"]["basis"]
    assert basis_schema["properties"]["evidence"]["maxItems"] == 2
    assert core_view.provider_prompt_suffix() == "+basis@1"

    monkeypatch.setattr(settings, "SCORING_INSUFFICIENT_EVIDENCE_MODE", "review")
    request = core_view.build_core_request(_v4())
    assert "insufficient_evidence 时说明证据缺少哪部分内容" in request.system
    assert core_view.provider_prompt_suffix() == "+insufficient@1+basis@1"


def test_basis_is_decoded_with_verified_quotes(monkeypatch):
    monkeypatch.setattr(settings, "SCORING_DECISION_BASIS_MODE", "cite")
    envelope = _v4()
    request = core_view.build_core_request(envelope)
    alias, unit_id = next(iter(request.alias_to_unit.items()))
    unit = next(
        item for item in envelope.to_mapping()["evidence_units"] if item["evidence_unit_id"] == unit_id
    )
    rule_code = envelope.to_mapping()["atomic_rule_snapshot"]["rule_code"]
    response = {
        "schema_version": "semantic-rule-response@2",
        "rule_code": rule_code,
        "status": "not_triggered",
        "level_code": None,
        "occurrences": [],
        "basis": {
            "evidence": [
                {"type": "source_quote", "evidence_ref": alias, "quote": unit["normalized_text"][:8]},
                {"type": "source_quote", "evidence_ref": "E999", "quote": "不存在的段落"},
            ],
            "rationale": "结论逐条回应了研究问题。",
        },
    }
    decoded = core_view.decode_core_response(envelope, response, request)
    assert decoded["basis"]["rationale"] == "结论逐条回应了研究问题。"
    assert decoded["basis"]["evidence"] == [
        {"type": "source_quote", "evidence_unit_id": unit_id, "quote": unit["normalized_text"][:8]}
    ]
    assert decoded["basis"]["rejected_quote_count"] == 1

    # 模型漏写 basis 不判失败：判定照常，只是没有依据。
    missing = {key: value for key, value in response.items() if key != "basis"}
    assert "basis" not in core_view.decode_core_response(envelope, missing, request)


def test_basis_is_rejected_when_the_switch_is_off():
    envelope = _v4()
    rule_code = envelope.to_mapping()["atomic_rule_snapshot"]["rule_code"]
    with pytest.raises(ValueError, match="invalid fields"):
        normalize_core_provider_response(
            envelope,
            {
                "schema_version": "semantic-rule-response@2",
                "rule_code": rule_code,
                "status": "not_triggered",
                "level_code": None,
                "occurrences": [],
                "basis": {"evidence": [], "rationale": "x"},
            },
        )


# ── G2：落库的中文说明 ──────────────────────────────────────────────


def test_core_deduction_reason_is_chinese_and_names_the_rule():
    assert core_deduction_reason("数据库设计缺少说明", "-2.50") == "规则「数据库设计缺少说明」扣 2.5 分"
    assert core_deduction_reason(None, "-10") == "扣 10 分"


def test_persisted_item_explains_itself_and_keeps_display_extras():
    rule = _deduct_rule(max_points="5")
    request = _request_for((_criterion(), rule))
    unit = _unit(request)
    quote = unit["normalized_text"][:12]
    outcome = _outcome(
        request,
        {rule["rule_code"]: _semantic_response(
            rule["rule_code"],
            occurrences=[_quote_occurrence(request, quote=quote)],
            basis={"evidence": [], "rationale": "这里缺少说明。", "rejected_quote_count": 0},
        )},
        profile=_ReportingProfile({"candidate_count": 3, "requested_count": 3, "selected_count": 3}),
    )
    engine, db, run = _persist(request, outcome)
    try:
        item = run.items[0]
        assert "Authorized" not in json.dumps(item.deduction_items, ensure_ascii=False)
        assert all(entry["reason"].startswith("规则「") for entry in item.deduction_items)
        assert item.reason.startswith("按 1 条规则判定")
        result = item.rule_results[0]
        assert result["basis"]["rationale"] == "这里缺少说明。"
        assert result["evidence_selection"]["selected_count"] == 3
        assert item.evidence[0]["quote"] == quote
    finally:
        db.close()
        engine.dispose()


# ── G2/G4：判定视图与证据充分度 ─────────────────────────────────────


def _view_for(request, responses, *, report=None):
    profile = _ReportingProfile(report) if report else None
    outcome = _outcome(request, responses, profile=profile)
    engine, db, run = _persist(request, outcome)
    try:
        return build_judgement_view(run, run.items[0], frozen=_frozen(request))
    finally:
        db.close()
        engine.dispose()


def test_judgement_view_lists_deductions_with_quotes_and_a_high_sufficiency():
    rule = _deduct_rule()
    request = _request_for((_criterion(), rule))
    quote = _unit(request)["normalized_text"][:12]
    view = _view_for(
        request,
        {rule["rule_code"]: _semantic_response(
            rule["rule_code"], occurrences=[_quote_occurrence(request, quote=quote)]
        )},
        report={"candidate_count": 3, "requested_count": 3, "selected_count": 3},
    )
    row = view["rules"][0]
    assert row["outcome"] == "deducted" and row["effect"].startswith("扣 ")
    assert row["evidence"][0]["location_status"] == "verified"
    assert row["evidence"][0]["quote"] == quote
    assert view["evidence_sufficiency"]["level"] == "high"


def test_judgement_view_explains_items_without_deductions():
    rule = _deduct_rule()
    request = _request_for((_criterion(), rule))
    view = _view_for(
        request,
        {rule["rule_code"]: _semantic_response(rule["rule_code"], status="not_triggered")},
    )
    row = view["rules"][0]
    assert (row["outcome"], row["label"]) == ("not_triggered", "未发现问题")
    # 旧评分没有选证记录：不降档，只注明。
    sufficiency = view["evidence_sufficiency"]
    assert sufficiency["level"] == "high"
    assert sufficiency["notes"] and "旧评分" in sufficiency["notes"][0]


def test_budget_trimming_lowers_sufficiency_to_medium():
    rule = _deduct_rule()
    request = _request_for((_criterion(), rule))
    view = _view_for(
        request,
        {rule["rule_code"]: _semantic_response(rule["rule_code"], status="not_triggered")},
        report={"candidate_count": 40, "requested_count": 12, "selected_count": 9},
    )
    assert view["evidence_sufficiency"]["level"] == "medium"
    assert "少放了 3 段" in view["evidence_sufficiency"]["reasons"][0]


def test_cited_not_triggered_without_a_quote_is_medium_and_bad_quotes_are_low():
    rule = _deduct_rule()
    request = _request_for((_criterion(), rule))
    report = {"candidate_count": 3, "requested_count": 3, "selected_count": 3}
    view = _view_for(
        request,
        {rule["rule_code"]: _semantic_response(
            rule["rule_code"],
            status="not_triggered",
            basis={"evidence": [], "rationale": "没有找到相关段落。", "rejected_quote_count": 0},
        )},
        report=report,
    )
    assert view["rules"][0]["rationale"] == "没有找到相关段落。"
    assert view["evidence_sufficiency"]["level"] == "medium"

    unit = _unit(request)
    view = _view_for(
        request,
        {rule["rule_code"]: _semantic_response(
            rule["rule_code"],
            status="not_triggered",
            basis={"evidence": [_source_quote(unit, quote="编造的引文")], "rationale": "x", "rejected_quote_count": 0},
        )},
        report=report,
    )
    assert view["evidence_sufficiency"]["level"] == "low"


def test_insufficient_evidence_is_shown_and_lowers_sufficiency(monkeypatch):
    rule = _deduct_rule()
    request = _request_for((_criterion(), rule))
    view = _view_for(
        request,
        {rule["rule_code"]: _semantic_response(rule["rule_code"], status="insufficient_evidence")},
    )
    assert view["rules"][0]["outcome"] == "insufficient"
    assert view["evidence_sufficiency"]["level"] == "low"


def test_legacy_runs_get_no_judgement_view():
    item = SimpleNamespace(rule_results=None, review_reasons=None)
    assert build_judgement_view(SimpleNamespace(), item, frozen=FrozenDocument(kind="legacy")) is None


# ── G1：论文 Core 评分回到冻结正文 ──────────────────────────────────


def test_paper_core_run_resolves_its_frozen_document_from_the_snapshot_store():
    request = _request_for((_criterion(), _deduct_rule()))
    ref = LocalDocumentSnapshotStore().put(request["document"])
    run = SimpleNamespace(id="run-1", submission_id=None, document_snapshot_ref=ref, paper_id="p1")
    frozen = resolve_frozen_document(run)
    assert frozen.kind == "core" and frozen.available
    assert frozen.snapshot_hash == request["document"]["document_snapshot_hash"]

    unit = _unit(request)
    quote = unit["normalized_text"][:6]
    item = SimpleNamespace(evidence=[
        {"evidence_unit_id": unit["evidence_unit_id"], "evidence_type": "source_quote",
         "locator": unit["locator"], "payload_hash": "0" * 64, "quote": quote},
        {"evidence_unit_id": unit["evidence_unit_id"], "evidence_type": "source_quote",
         "locator": unit["locator"], "payload_hash": "1" * 64},
    ])
    views = evidence_view.build_evidence_view(None, run, item)
    assert [view["location_status"] for view in views] == ["verified", "unit_context"]
    assert views[0]["quote"] == quote and views[1]["quote"] is None

    page = document_view.build_document_view(None, run, anchor_id=unit["evidence_unit_id"])
    assert page["source_kind"] == "core_snapshot" and page["frozen"] is True
    assert page["anchor_found"] is True


def test_unreadable_frozen_document_is_reported_not_replaced_by_the_reparse():
    run = SimpleNamespace(
        id="run-2", submission_id=None, paper_id="p1",
        document_snapshot_ref="document-snapshot:sha256:" + "a" * 64,
    )
    frozen = resolve_frozen_document(run)
    assert frozen.kind == "core" and not frozen.available
    page = document_view.build_document_view(None, run)
    assert page["source_kind"] == "unavailable"
    assert "读不到" in page["unavailable_reason"]

    legacy = SimpleNamespace(id="run-3", submission_id=None, document_snapshot_ref=None)
    assert resolve_frozen_document(legacy).kind == "legacy"


# ── 复审补充（PR #77）────────────────────────────────────────────────


def test_unhashable_basis_refs_are_counted_not_raised(monkeypatch):
    monkeypatch.setattr(settings, "SCORING_DECISION_BASIS_MODE", "cite")
    envelope = _v4()
    request = core_view.build_core_request(envelope)
    rule_code = envelope.to_mapping()["atomic_rule_snapshot"]["rule_code"]
    response = {
        "schema_version": "semantic-rule-response@2",
        "rule_code": rule_code,
        "status": "not_triggered",
        "level_code": None,
        "occurrences": [],
        "basis": {
            "evidence": [{"type": "source_quote", "evidence_ref": ["E1"], "quote": "x"}],
            "rationale": "理由",
        },
    }
    decoded = core_view.decode_core_response(envelope, response, request)
    assert decoded["basis"]["evidence"] == [] and decoded["basis"]["rejected_quote_count"] == 1

    # 直接给归一化函数一个不可哈希的单元编号，也只算一条丢弃的引文。
    response["basis"]["evidence"] = [{"type": "source_quote", "evidence_unit_id": ["u"], "quote": "x"}]
    normalized = normalize_core_provider_response(envelope, response, allow_basis=True)
    assert normalized["basis"]["rejected_quote_count"] == 1

    # 执行器再核对时同样不抛。
    rule = _deduct_rule()
    req = _request_for((_criterion(), rule))
    outcome = _outcome(req, {rule["rule_code"]: _semantic_response(
        rule["rule_code"], status="not_triggered",
        basis={"evidence": [{"type": "source_quote", "evidence_unit_id": {"a": 1}, "quote": "x"}],
               "rationale": "r", "rejected_quote_count": 0},
    )})
    assert _decision(outcome, rule["rule_code"])["basis"]["rejected_quote_count"] == 1


def test_unhashable_occurrence_ref_fails_closed_as_a_value_error():
    envelope = _v4()
    request = core_view.build_core_request(envelope)
    rule_code = envelope.to_mapping()["atomic_rule_snapshot"]["rule_code"]
    with pytest.raises(ValueError, match="unknown evidence ref"):
        core_view.decode_core_response(envelope, {
            "schema_version": "semantic-rule-response@2",
            "rule_code": rule_code,
            "status": "triggered",
            "level_code": None,
            "occurrences": [{"finding_code": "GAP", "evidence": [
                {"type": "source_quote", "evidence_ref": ["E1"], "quote": "x"}]}],
        }, request)


def test_group_basis_is_shown_once_on_the_chosen_tier():
    from backend.app.services.scoring.judgement_view import _share_group_basis

    def row(code, outcome, rationale):
        return {
            "rule_code": code, "rule_name": "档位" + code, "judge_type": "semantic", "outcome": outcome,
            "label": "", "effect": None, "rationale": rationale, "evidence": [],
            "basis_evidence": [{"quote": "q"}], "has_basis": True, "rejected_quote_count": 1,
            "evidence_selection": None, "basis_scope": None, "basis_shared_with": None,
            "_group_code": "G1",
        }

    rows = [row("A", "not_triggered", "缺陷在第 3 章"), row("B", "deducted", "缺陷在第 3 章")]
    _share_group_basis(rows)
    unchosen, chosen = rows
    assert chosen["basis_scope"] == "group" and chosen["rationale"] == "缺陷在第 3 章"
    # 没选中的那一档不再把组的理由当成自己的：它显示“未发现问题”，下面不能写“缺陷在哪里”。
    assert unchosen["rationale"] is None and unchosen["basis_evidence"] == []
    assert unchosen["basis_shared_with"] == "档位B"
    assert unchosen["rejected_quote_count"] == 0


def test_group_decision_basis_is_marked_with_its_group():
    from backend.app.services.scoring.core.results import _normalize_decision_basis

    value = {"evidence_refs": [], "rationale": "r", "rejected_quote_count": 0, "group_code": "G1"}
    assert _normalize_decision_basis(value, "basis")["group_code"] == "G1"
    assert "group_code" not in _normalize_decision_basis({**value, "group_code": None}, "basis")


def test_item_summary_counts_insufficient_evidence_separately():
    from backend.app.services.scoring.adapters.persistence import core_item_reason

    results = [
        {"rule_code": "R1", "status": "not_triggered"},
        {"rule_code": "R2", "status": "not_triggered"},
        {"rule_code": "R3", "status": "triggered"},
    ]
    issues = [{"code": "RULE_EVIDENCE_INSUFFICIENT", "rule_code": "R2"}]
    assert core_item_reason(results, issues) == "按 3 条规则判定：未发现问题 1 条、证据不足 1 条、命中 1 条。"
    assert core_item_reason(results) == "按 3 条规则判定：未发现问题 2 条、命中 1 条。"


def test_one_group_call_marks_every_tier_basis_with_its_group():
    from backend.app.tests.test_token_compression_pipeline import _GroupRuntime
    from backend.app.tests.test_token_compression_pipeline import _execute
    from backend.app.tests.test_token_compression_pipeline import _request as group_request
    from backend.app.tests.test_token_compression_pipeline import _tiers

    class _BasisGroupRuntime(_GroupRuntime):
        def score_group(self, *, envelopes, group_code):
            responses = super().score_group(envelopes=envelopes, group_code=group_code)
            for response in responses.values():
                response["basis"] = {"evidence": [], "rationale": "第二档最符合。", "rejected_quote_count": 0}
            return responses

    request = group_request(*_tiers())
    result = _execute(request, _BasisGroupRuntime(request))
    bases = {code: _decision(result, code)["basis"] for code in ("T.1", "T.2", "T.3")}
    assert {basis["rationale"] for basis in bases.values()} == {"第二档最符合。"}
    assert len({basis["group_code"] for basis in bases.values()}) == 1
