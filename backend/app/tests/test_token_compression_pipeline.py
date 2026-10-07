"""压缩方案阶段 1–5：判断用视图、互斥组合并、证据压缩、稳定前缀、论文概况卡。

全部使用本地夹具与假运行时，不调用任何真实模型。
"""

from copy import deepcopy
from decimal import Decimal
import json

import httpx
import pytest

from backend.app.core.config import settings
from backend.app.db import models
from backend.app.services.llm.core_adapter import core_runtime_provider_contract
from backend.app.services.llm.core_view import build_core_group_request
from backend.app.services.llm.core_view import build_core_request
from backend.app.services.llm.core_view import decode_core_group_response
from backend.app.services.llm.core_view import decode_core_response
from backend.app.services.llm.mock import MockLLMScorer
from backend.app.services.llm.openai_compatible_adapter import OpenAICompatibleChatScorer
from backend.app.services.llm.usage import UsageMeter
from backend.app.services.scoring.core.contracts import AtomicRuleSnapshot
from backend.app.services.scoring.core.contracts import ScoringRequest
from backend.app.services.scoring.core.rule_context import derive_context_needs
from backend.app.services.scoring.core.rule_executor import eligible_rule_groups
from backend.app.services.scoring.core.rule_executor import execute_rule_plan
from backend.app.services.scoring.core.rule_executor import prompt_envelope_for_rule
from backend.app.services.scoring.decision_ledger import RuleCallJournal
from backend.app.services.scoring.profiles.thesis import ThesisLLMRuntime
from backend.app.services.scoring.retrieval import compression
from backend.app.services.scoring.retrieval.digest import build_paper_digest
from backend.app.services.scoring.retrieval.selection import build_v4_envelope
from backend.app.tests import test_m4_rule_executor as m4


# ---------------------------------------------------------------- fixtures


def _v3_rule(code, *, text, name=None, points="2", mutex_group=None, depends_on=(), needs=None):
    rule = m4._deduct_rule(code=code, max_points=points, mutex_group=mutex_group, depends_on=depends_on)
    rule.update(
        {
            "schema_version": "atomic-rule-snapshot@3",
            "name": name or code,
            "rule_text": text,
            "positive_example": None,
            "negative_example": None,
            "boundary_example": None,
            "context_needs": sorted(needs) if needs is not None else derive_context_needs(name or code, text),
        }
    )
    return rule


class _RichProfile(m4._TechnicalProposalProfile):
    """Thesis-like extensions: student metadata, contexts and a digest."""

    def build_prompt_extensions(self, *, submission_snapshot, document_snapshot):
        return {
            "instructions": {"domain": "thesis"},
            "metadata": {"student_name": "张三丰", "student_id": "20260001234", "title": "测试论文"},
            "profile_extensions": {
                "references": ["[1] 甲. 文献甲[J]. 期刊, 2021.", "[2] 乙. 文献乙[M]. 出版社, 2019."],
                "coherence_findings": [{"kind": "figure_numbering_gap", "message": "图编号不连续", "refs": [1]}],
                "structure_checks": [
                    {"name": "中文摘要", "passed": False, "message": "缺少中文摘要"},
                    {"name": "英文摘要", "passed": True, "message": "ok"},
                ],
            },
            "paper_digest": {"schema_version": "paper-digest@1", "outline": ["需求理解", "风险控制"]},
        }


def _request(*rules):
    return m4._request_for(*((m4._criterion(), rule) for rule in rules))


def _envelopes(request, profile=None, *, shared=False):
    value = ScoringRequest.from_mapping(deepcopy(request)).to_mapping()
    nodes = [node for node in value["plan"]["nodes"] if node["node_kind"] == "atomic_rule"]
    rules = [node["atomic_rule_snapshot"] for node in nodes]
    return [
        prompt_envelope_for_rule(
            request=value,
            node=node,
            profile=profile or m4._TechnicalProposalProfile(),
            selection_rules=rules if shared else None,
        )
        for node in nodes
    ]


def _unit_ids(request):
    return [unit["evidence_unit_id"] for unit in request["document"]["evidence_units"]]


# ---------------------------------------------------------------- 阶段 1：快照 @3


def test_snapshot_v3_carries_the_published_wording():
    rule = _v3_rule("R1", text="方案缺少升级窗口说明。", needs=["structure"])
    normalized = AtomicRuleSnapshot.from_mapping(rule).to_mapping()
    assert normalized["rule_text"] == "方案缺少升级窗口说明。"
    assert normalized["context_needs"] == ["structure"]

    for bad in (["unknown"], ["structure", "coherence"]):
        broken = deepcopy(rule)
        broken["context_needs"] = bad
        with pytest.raises(ValueError):
            AtomicRuleSnapshot.from_mapping(broken)
    legacy = m4._deduct_rule()
    legacy["rule_text"] = "@2 has no wording fields"
    with pytest.raises(ValueError):
        AtomicRuleSnapshot.from_mapping(legacy)


def test_context_needs_follow_the_rule_wording():
    assert derive_context_needs("文献综述", "参考文献数量不足") == ["references"]
    assert derive_context_needs("一致性", "需求与测试前后不一致") == ["coherence"]
    assert derive_context_needs("完整性", "缺少测试章节") == ["structure"]
    assert derive_context_needs("创新", "创新点不明确") == []


# ---------------------------------------------------------------- 阶段 1/2/4：选证


def _v4(envelope, *, selection_rules=None, top_k=1):
    return build_v4_envelope(
        envelope,
        context_window_tokens=32768,
        reserved_output_tokens=512,
        safety_margin_tokens=0,
        top_k=top_k,
        selection_rules=selection_rules,
    ).to_mapping()


def test_rule_wording_ranks_evidence_without_flipping_to_exhaustive_coverage():
    request = _request(_v3_rule("R1", text="方案缺少升级窗口的说明"))
    [envelope] = _envelopes(request)

    selected = _v4(envelope)

    # "缺少" would make the wording an absence query; coverage stays structural.
    assert selected["coverage_mode"] == "top_k"
    assert selected["evidence_units"][0]["normalized_text"] == "升级窗口为十五分钟。"
    assert selected["evidence_selection_identity"]["selector_version"] == "section-bm25-diverse@2"


def test_group_members_share_one_evidence_selection():
    first = _v3_rule("R1", text="未说明升级窗口", mutex_group="G")
    second = _v3_rule("R2", text="未明确风险负责人", mutex_group="G", points="6")
    request = _request(first, second)
    env_first, env_second = _envelopes(request)

    alone = [_v4(env_first)["evidence_units"], _v4(env_second)["evidence_units"]]
    shared = [
        _v4(env_first, selection_rules=[first, second])["evidence_units"],
        _v4(env_second, selection_rules=[first, second])["evidence_units"],
    ]

    assert alone[0] != alone[1]
    assert shared[0] == shared[1]


def test_a_shared_selection_query_does_not_depend_on_the_member():
    # Regression: the member's own rule code leaked into the query, so on a
    # real paper (hundreds of units) BM25 picked different evidence per tier.
    from backend.app.services.scoring.retrieval import selection

    tiers = _tiers()
    envelopes = _envelopes(_request(*tiers))
    values = [envelope.to_mapping() for envelope in envelopes]

    queries = {selection._ranking_query(value, tiers) for value in values}
    structural = {selection._structural_query(value, tiers) for value in values}

    assert len(queries) == 1 and len(structural) == 1
    assert len({selection._ranking_query(value) for value in values}) == 3


# ---------------------------------------------------------------- 阶段 3：证据压缩


def test_units_are_classified_by_type():
    assert compression.classify_unit("学生姓名： | 张三") == "personal"
    assert compression.classify_unit("[1] 张三. 题名[J]. 期刊, 2020.") == "reference"
    assert compression.classify_unit("图4.1 用户管理功能图") == "caption"
    assert compression.classify_unit("title | varchar | 200 | 允许") == "table"
    assert compression.classify_unit("def f(x):\n    y = x;\n    return {y};\n") == "code"
    assert compression.classify_unit("本系统采用 Spring Boot 实现。") == "paragraph"


def test_long_units_become_verbatim_fragments_around_relevant_sentences():
    text = "".join("第%d句是无关的背景描述内容。" % index for index in range(30))
    text = text.replace("第15句是无关的背景描述内容。", "第15句说明了测试环境的搭建过程。")

    shown, reducer = compression.compress_unit(
        text, kind="paragraph", terms={"测试", "环境"}, cap=120
    )

    assert reducer == "sentences"
    assert len(shown) <= 120 + 5
    assert "测试环境" in shown and compression.ELLIPSIS in shown
    assert all(fragment in text for fragment in compression.verbatim_fragments(shown))


def test_evidence_budget_personal_data_and_captions_are_decided_and_recorded():
    long = "这是一段很长的正文内容，用于检验预算。" * 40
    units = [
        {"evidence_unit_id": "p", "normalized_text": "学生姓名： | 张三"},
        {"evidence_unit_id": "c", "normalized_text": "图2.1 总体业务流程图"},
    ] + [{"evidence_unit_id": "u%d" % index, "normalized_text": long} for index in range(12)]

    shown, decisions = compression.compress_evidence(units, query="预算", keep_captions=False)

    assert len(decisions) == len(units)
    reasons = {item["evidence_unit_id"]: item["reason"] for item in decisions}
    assert reasons["p"] == "personal_information"
    assert reasons["c"] == "caption_not_needed"
    assert "evidence_budget" in reasons.values()
    assert sum(len(text) for _unit, text in shown) <= compression.EVIDENCE_CHAR_CAP
    with_captions, _ = compression.compress_evidence(units[:2], query="", keep_captions=True)
    assert [unit["evidence_unit_id"] for unit, _text in with_captions] == ["c"]


# ---------------------------------------------------------------- 阶段 5：概况卡


def test_paper_digest_is_deterministic_orientation():
    document = {
        "sections": [
            {"section_path": ["第1章 绪论"], "heading": "第1章 绪论", "normalized_text": "x" * 300},
            {"section_path": ["第3章 需求分析"], "heading": "第3章 需求分析", "normalized_text": "y" * 200},
            {"section_path": ["第3章 需求分析", "3.1 功能需求"], "heading": "3.1 功能需求", "normalized_text": ""},
            {"section_path": ["第3章 需求分析", "3.1 功能需求", "3.1.1 细节"], "heading": "3.1.1 细节", "normalized_text": ""},
            {"section_path": ["第6章 系统测试"], "heading": "第6章 系统测试", "normalized_text": "z" * 100},
            {"section_path": ["未命名开头"], "heading": "未命名开头", "normalized_text": ""},
            {"section_path": ["第6章 系统测试", "x"], "heading": "13 | 回复客服消息 | 管理员", "normalized_text": ""},
        ],
        "evidence_units": [
            {"normalized_text": "图3.1 用例图"},
            {"normalized_text": "表6.1 测试用例"},
        ],
    }
    extensions = {
        "references": ["[1] 甲. 题名[J]. 2021.", "[2] 乙. 题名[M]. 2015.", "[3] 丙. 网页[EB/OL]. 2024."],
        "structure_checks": [{"name": "英文摘要", "passed": False}],
    }

    digest = build_paper_digest(document_snapshot=document, profile_extensions=extensions, title="题目")

    assert digest == build_paper_digest(document_snapshot=document, profile_extensions=extensions, title="题目")
    # Level-3 headings, the parser's placeholder and table rows are noise.
    assert digest["outline"] == ["第1章 绪论", "第3章 需求分析", "3.1 功能需求", "第6章 系统测试"]
    assert digest["body_chars"] == 600
    assert (digest["figure_captions"], digest["table_captions"]) == (1, 1)
    assert digest["references"] == {
        "count": 3,
        "types": {"EB/OL": 1, "J": 1, "M": 1},
        "year_range": [2015, 2024],
        "within_5_years_of_latest": 2,
    }
    assert digest["key_sections"]["requirements"] and digest["key_sections"]["testing"]
    assert not digest["key_sections"]["conclusion"]
    assert digest["failed_structure_checks"] == ["英文摘要"]
    assert len(json.dumps(digest, ensure_ascii=False)) < 800


# ---------------------------------------------------------------- 阶段 1/4：判断用视图


def test_view_sends_only_what_judging_needs_in_a_prefix_stable_order():
    rule = _v3_rule("R1", text="参考文献引用不规范", needs=["references"])
    request = _request(rule)
    [envelope] = _envelopes(request, _RichProfile())

    view_request = build_core_request(envelope)
    view = json.loads(view_request.user)
    serialized = view_request.user

    assert list(view) == ["paper", "criterion", "context", "evidence", "rule"]
    assert view["rule"]["rule_text"] == "参考文献引用不规范"
    assert set(view["context"]) == {"references"}
    assert [item["ref"] for item in view["evidence"]] == ["E1", "E2", "E3"]
    for leaked in ("张三丰", "20260001234", "runtime_identity", "plan_hash", *_unit_ids(request)):
        assert leaked not in serialized
    full = json.dumps(envelope.to_mapping(), ensure_ascii=False, separators=(",", ":"))
    assert len(serialized) < len(full) * 0.6
    # The system prompt is identical for every rule: a cacheable prefix.
    other = build_core_request(_envelopes(_request(_v3_rule("R9", text="x")), _RichProfile())[0])
    assert other.system == view_request.system


def test_rules_without_context_needs_get_no_context():
    request = _request(_v3_rule("R1", text="创新点不明确", needs=[]))
    [envelope] = _envelopes(request, _RichProfile())
    assert "context" not in json.loads(build_core_request(envelope).user)


def test_view_aliases_map_back_and_unknown_refs_fail_closed():
    request = _request(_v3_rule("R1", text="升级窗口"))
    [envelope] = _envelopes(request)
    view_request = build_core_request(envelope)
    unit = request["document"]["evidence_units"][0]
    response = {
        "schema_version": "semantic-rule-response@2",
        "rule_code": "R1",
        "status": "triggered",
        "level_code": None,
        "occurrences": [
            {
                "finding_code": m4.ALLOWED_FINDING_CODES[0],
                "evidence": [{"type": "source_quote", "evidence_ref": "E1", "quote": unit["normalized_text"]}],
            }
        ],
    }

    decoded = decode_core_response(envelope, response, view_request)
    assert decoded["occurrences"][0]["evidence"][0]["evidence_unit_id"] == unit["evidence_unit_id"]

    response["occurrences"][0]["evidence"][0]["evidence_ref"] = "E9"
    with pytest.raises(ValueError):
        decode_core_response(envelope, response, view_request)


# ---------------------------------------------------------------- 阶段 2：组请求


def _tiers():
    return [
        _v3_rule("T.1", text="轻度：缺 1 项", mutex_group="G", points="2"),
        _v3_rule("T.2", text="中度：缺 2 项", mutex_group="G", points="6"),
        _v3_rule("T.3", text="重度：缺 3 项及以上", mutex_group="G", points="14"),
    ]


def _group_response(status, selected=None, occurrences=None):
    return {
        "schema_version": "semantic-group-response@1",
        "group_code": "G",
        "status": status,
        "selected_rule_code": selected,
        "occurrences": occurrences or [],
    }


def test_group_request_projects_one_decision_onto_every_tier():
    request = _request(*_tiers())
    envelopes = _envelopes(request, shared=True)
    group_request = build_core_group_request(envelopes, group_code="G")
    view = json.loads(group_request.user)
    assert [item["rule_code"] for item in view["rules"]] == ["T.1", "T.2", "T.3"]
    unit = request["document"]["evidence_units"][1]
    occurrence = {
        "finding_code": m4.ALLOWED_FINDING_CODES[0],
        "evidence": [{"type": "source_quote", "evidence_ref": "E2", "quote": unit["normalized_text"]}],
    }

    decoded = decode_core_group_response(
        envelopes, _group_response("triggered", "T.2", [occurrence]), group_request
    )
    assert {code: item["status"] for code, item in decoded.items()} == {
        "T.1": "not_triggered",
        "T.2": "triggered",
        "T.3": "not_triggered",
    }
    none = decode_core_group_response(envelopes, _group_response("not_applicable"), group_request)
    assert {item["status"] for item in none.values()} == {"not_applicable"}
    for bad in (
        _group_response("triggered", "OTHER", [occurrence]),
        _group_response("not_triggered", None, [occurrence]),
        {**_group_response("not_triggered"), "group_code": "H"},
    ):
        with pytest.raises(ValueError):
            decode_core_group_response(envelopes, bad, group_request)


def test_group_request_requires_one_shared_selection():
    request = _request(*_tiers())
    envelopes = _envelopes(request, shared=True)
    tampered = envelopes[1].to_mapping()
    tampered["evidence_units"] = tampered["evidence_units"][:1]

    with pytest.raises(ValueError):
        build_core_group_request([envelopes[0], tampered, envelopes[2]], group_code="G")


# ---------------------------------------------------------------- 阶段 2：执行器


class _GroupRuntime(m4._SemanticRuntime):
    def __init__(self, request, *, selected="T.2", fail=None):
        super().__init__({})
        self.request = request
        self.selected = selected
        self.fail = fail
        self.group_calls = 0

    def score_group(self, *, envelopes, group_code):
        self.group_calls += 1
        if self.fail is not None:
            raise self.fail
        responses = {}
        for envelope in envelopes:
            code = envelope.to_mapping()["atomic_rule_snapshot"]["rule_code"]
            if code == self.selected:
                responses[code] = m4._semantic_response(
                    code, occurrences=[m4._quote_occurrence(self.request)]
                )
            else:
                responses[code] = m4._semantic_response(code, status="not_triggered")
        return responses


class _MemoryLedger:
    def __init__(self):
        self.rows = {}

    def get(self, *, decision_identity):
        value = self.rows.get(decision_identity)
        return None if value is None else deepcopy(value)

    def put(self, *, decision_identity, rule_code, response, usage=None):
        self.rows[decision_identity] = deepcopy(response)


def _execute(request, runtime, *, ledger=None, journal=None):
    return m4._result_mapping(
        execute_rule_plan(
            request=deepcopy(request),
            checker_registry=m4._CheckerRegistry(),
            llm_runtime=runtime,
            profile=m4._TechnicalProposalProfile(),
            decision_ledger=ledger,
            execution_journal=journal,
        )
    )


def test_a_mutex_group_is_judged_in_one_call_without_conflicts():
    request = _request(*_tiers())
    runtime = _GroupRuntime(request)
    journal = RuleCallJournal()

    result = _execute(request, runtime, journal=journal)

    assert runtime.group_calls == 1 and runtime.envelopes == []
    statuses = {code: m4._decision(result, code)["status"] for code in ("T.1", "T.2", "T.3")}
    assert statuses == {"T.1": "not_triggered", "T.2": "triggered", "T.3": "not_triggered"}
    assert m4._issues(result, "MUTEX_CONFLICT") == []
    assert len(set(journal.group_call_ids.values())) == 1
    assert set(journal.group_call_ids) == {"T.1", "T.2", "T.3"}


def test_a_group_decision_is_replayed_from_the_ledger():
    request = _request(*_tiers())
    ledger = _MemoryLedger()
    first = _execute(request, _GroupRuntime(request), ledger=ledger)
    runtime = _GroupRuntime(request)
    journal = RuleCallJournal()

    second = _execute(request, runtime, ledger=ledger, journal=journal)

    assert runtime.group_calls == 0
    assert second == first
    assert journal.reused_rule_codes == {"T.1", "T.2", "T.3"}


def test_a_failed_group_call_fails_every_tier_with_one_code():
    class _Failure(RuntimeError):
        code = "PROVIDER_CIRCUIT_OPEN"

    request = _request(*_tiers())
    result = _execute(request, _GroupRuntime(request, fail=_Failure()))

    for code in ("T.1", "T.2", "T.3"):
        assert m4._decision(result, code)["status"] == "invalid"
    assert {issue["code"] for issue in result["review_issues"]} == {"PROVIDER_CIRCUIT_OPEN"}


def test_runtimes_without_group_support_fall_back_to_one_call_per_rule():
    request = _request(*_tiers())
    runtime = m4._SemanticRuntime(
        {code: m4._semantic_response(code, status="not_triggered") for code in ("T.1", "T.2", "T.3")}
    )

    _execute(request, runtime)

    assert len(runtime.envelopes) == 3


def test_groups_with_dependencies_or_mixed_tiers_are_not_merged():
    tiers = _tiers()
    tiers[2]["depends_on_rule_codes"] = ["T.1"]
    request = _request(*tiers)
    value = ScoringRequest.from_mapping(deepcopy(request)).to_mapping()
    nodes = {node["rule_code"]: node for node in value["plan"]["nodes"]}

    assert eligible_rule_groups(nodes, sorted(nodes)) == {}

    request = _request(*_tiers())
    value = ScoringRequest.from_mapping(deepcopy(request)).to_mapping()
    nodes = {node["rule_code"]: node for node in value["plan"]["nodes"]}
    groups = eligible_rule_groups(nodes, sorted(nodes))
    assert groups["T.3"][1] == ["T.1", "T.2", "T.3"]
    assert Decimal(nodes["T.3"]["atomic_rule_snapshot"]["max_points"]) == Decimal("14")


# ---------------------------------------------------------------- 运行时与适配器


def test_paper_runtime_group_calls_are_metered_and_mock_falls_back():
    mock_runtime = ThesisLLMRuntime(MockLLMScorer())
    assert mock_runtime.supports_group_calls is False
    assert mock_runtime.score_group(envelopes=[], group_code="G") is None

    class _GroupScorer:
        provider = "fake"

        def __init__(self):
            self.usage_meter = UsageMeter()

        def score_core_group(self, *, envelopes, group_code):
            self.usage_meter.record_success({"prompt_tokens": 900, "completion_tokens": 30})
            return {"ok": group_code}

    runtime = ThesisLLMRuntime(_GroupScorer())
    assert runtime.score_group(envelopes=[], group_code="G") == {"ok": "G"}
    assert runtime.last_call_usage["prompt_tokens"] == 900


class _ScriptedChatClient:
    def __init__(self, content):
        self.content = content
        self.payload = None

    def post(self, url, headers, json, **kwargs):
        self.payload = json
        body = {
            "id": "chat",
            "choices": [{"message": {"role": "assistant", "content": self.content}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 40, "total_tokens": 540},
        }
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    def close(self):
        pass


def test_chat_adapter_sends_one_group_view_and_decodes_every_tier():
    request = _request(*_tiers())
    unit = request["document"]["evidence_units"][0]
    client = _ScriptedChatClient(
        json.dumps(
            _group_response(
                "triggered",
                "T.3",
                [
                    {
                        "finding_code": m4.ALLOWED_FINDING_CODES[0],
                        "evidence": [{"type": "source_quote", "evidence_ref": "E1", "quote": unit["normalized_text"]}],
                    }
                ],
            ),
            ensure_ascii=False,
        )
    )
    scorer = OpenAICompatibleChatScorer(
        api_key="test-key",
        base_url="https://group-view.invalid/v1",
        model_name="group-model",
        client=client,
        temperature=0,
        max_tokens=700,
        response_format_json=True,
        thinking_type="disabled",
    )
    request["runtime_identity"]["provider"] = core_runtime_provider_contract(
        scorer, artifact_hash="8" * 64
    )
    m4._refresh_request_identity(request)
    envelopes = _envelopes(request, shared=True)

    decoded = scorer.score_core_group(envelopes=envelopes, group_code="G")

    view = json.loads(client.payload["messages"][1]["content"])
    assert [item["rule_code"] for item in view["rules"]] == ["T.1", "T.2", "T.3"]
    assert decoded["T.3"]["status"] == "triggered"
    assert decoded["T.1"]["status"] == decoded["T.2"]["status"] == "not_triggered"
    assert scorer.usage_meter.snapshot()["request_count"] == 1
    assert scorer.last_view_summary["view_version"] == "provider-view@1"


# ---------------------------------------------------------------- 持久化


def test_group_call_and_reuse_facts_are_persisted_per_rule():
    from backend.app.services.scoring.adapters.persistence import CoreRunPersistence
    from backend.app.tests.m3_contract_fixtures import scoring_request_payload
    from backend.app.tests.test_m3_legacy_adapters_modes import _InMemoryDocumentSnapshotStore
    from backend.app.tests.test_m3_legacy_adapters_modes import _outcome_for
    from backend.app.tests.test_m3_legacy_adapters_modes import _persistence_db

    engine, db, rubric, criteria, paper = _persistence_db()
    try:
        request = scoring_request_payload()
        store = _InMemoryDocumentSnapshotStore()
        rule_codes = [
            node["rule_code"] for node in request["plan"]["nodes"] if node["node_kind"] == "atomic_rule"
        ]
        run = CoreRunPersistence(db, document_snapshot_store=store).persist(
            request=deepcopy(request),
            outcome=_outcome_for(request),
            paper_id=paper.id,
            rubric_id=rubric.id,
            criterion_id_by_code={item.code: item.id for item in criteria},
            workflow_profile="template_driven",
            document_snapshot_ref=store.put(request["document"]),
            usage={"prompt_tokens": 321, "completion_tokens": 12, "total_tokens": 333},
            reused_rule_codes={rule_codes[0]},
            group_call_ids={rule_codes[0]: "a" * 64},
        )
        tasks = {
            task.rule_code: task
            for task in db.query(models.RuleScoringTask).filter_by(scoring_run_id=run.id)
        }
        assert run.prompt_tokens == 321
        assert tasks[rule_codes[0]].group_call_id == "a" * 64
        assert tasks[rule_codes[0]].decision_reused is (tasks[rule_codes[0]].status != "failed_exhausted")
        for code in rule_codes[1:]:
            assert tasks[code].group_call_id is None
            assert tasks[code].decision_reused is False
    finally:
        db.close()
        engine.dispose()


def test_criterion_scope_shares_evidence_across_rules(monkeypatch):
    from backend.app.services.scoring.profiles.thesis import ThesisProfile

    first = _v3_rule("R1", text="未说明升级窗口")
    second = _v3_rule("R2", text="未明确风险负责人")
    request = _request(first, second)
    [env_first, env_second] = _envelopes(request)
    profile = ThesisProfile()
    monkeypatch.setattr(settings, "SCORING_PROMPT_ENVELOPE_VERSION", "v4")
    monkeypatch.setattr(settings, "SCORING_EVIDENCE_SELECTION_MODE", "scoped")
    monkeypatch.setattr(settings, "SCORING_EVIDENCE_TOP_K", 1)

    def units(scope):
        monkeypatch.setattr(settings, "SCORING_EVIDENCE_SCOPE", scope)
        return [
            profile.build_provider_envelope(
                base_envelope=envelope, criterion_rules=[first, second]
            ).to_mapping()["evidence_units"]
            for envelope in (env_first, env_second)
        ]

    rule_scope = units("rule")
    criterion_scope = units("criterion")
    assert rule_scope[0] != rule_scope[1]
    assert criterion_scope[0] == criterion_scope[1]


def test_preflight_measures_the_view_that_is_actually_sent():
    from dataclasses import replace

    from backend.app.services.llm.core_view import preflight_core_request
    from backend.app.services.llm.core_view import request_input_estimate
    from backend.app.services.scoring.core.contracts import PromptEnvelopeV4
    from backend.app.services.scoring.retrieval.selection import TokenBudgetError

    request = _request(_v3_rule("R1", text="升级窗口"))
    [envelope] = _envelopes(request)
    v4 = PromptEnvelopeV4.from_mapping(_v4(envelope, top_k=3))
    view_request = build_core_request(v4)
    budget = v4.to_mapping()["token_budget_identity"]

    measured = preflight_core_request(v4, view_request)

    assert measured == request_input_estimate(view_request)
    # The envelope's own estimate covers identity fields that are never sent.
    assert measured < budget["estimated_input_tokens"]
    oversized = replace(view_request, user="x" * (3 * budget["context_window_tokens"]))
    with pytest.raises(TokenBudgetError):
        preflight_core_request(v4, oversized)


def test_a_quote_spanning_an_ellipsis_is_rejected_against_the_original():
    request = _request(_v3_rule("R1", text="升级窗口"))
    [envelope] = _envelopes(request)
    view_request = build_core_request(envelope)
    text = request["document"]["evidence_units"][0]["normalized_text"]
    spanning = text[:3] + compression.ELLIPSIS + text[-3:]
    response = {
        "schema_version": "semantic-rule-response@2",
        "rule_code": "R1",
        "status": "triggered",
        "level_code": None,
        "occurrences": [
            {
                "finding_code": m4.ALLOWED_FINDING_CODES[0],
                "evidence": [{"type": "source_quote", "evidence_ref": "E1", "quote": spanning}],
            }
        ],
    }

    with pytest.raises(ValueError):
        decode_core_response(envelope, response, view_request)
