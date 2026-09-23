"""阶段 3：修复静默丢弃与误解析。"""

import pytest

from backend.app.services.rubric_import import pipeline
from backend.app.services.rubric_import.compiler import analyze_rule_input
from backend.app.services.rubric_import.compiler import is_multi_judgement
from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.tests import rubric_parse_fixtures as fx
from backend.app.tests.test_rubric_parse_baseline import _command


@pytest.mark.parametrize(
    "text",
    ["格式错误、图表不清、引用不规范各扣2分", "摘要缺失；关键词缺失，分别扣1分", "格式错误、图表不清扣2分"],
)
def test_multi_judgement_segments_are_detected(text):
    assert is_multi_judgement(text) is True


@pytest.mark.parametrize(
    "text", ["方法说明不足扣3分", "意义笼统扣1-3分", "文献少于20篇扣2分", "缺少需求说明，扣 3 分", "查重率超过30%扣10分"]
)
def test_single_judgement_segments_are_not_flagged(text):
    assert is_multi_judgement(text) is False


def test_analyze_rule_input_routes_multi_judgement_to_unresolved():
    analysis = analyze_rule_input(["格式错误、图表不清、引用不规范各扣2分", "方法说明不足扣3分"], criterion_code="C03")
    assert analysis["input_state"] == "partial"
    assert [item["text"] for item in analysis["unresolved_segments"]] == ["格式错误、图表不清、引用不规范各扣2分"]
    assert analysis["unresolved_segments"][0]["reason"] == "multiple_judgements"
    assert analysis["needs_ai_draft"] is True
    assert [rule["match"] for rule in analysis["parsed_rules"]] == ["方法说明不足"]


def _complex_graph():
    return pipeline.prepare_file_import(
        command=_command(None), rules_bytes=fx.complex_rules_xlsx(), template_bytes=None
    ).to_mapping()


def test_full_row_merged_note_is_not_parsed_as_a_criterion():
    graph = _complex_graph()
    assert [item["name"] for item in graph["criteria"]] == ["选题意义", "文献综述", "方法设计"]
    raw = graph["compilation"]["raw_parse_output"]
    dropped = {item["row_number"]: item["reason"] for item in raw["extraction"]["dropped_rows"]}
    assert dropped == {7: "未找到满分", 8: "整行合并的说明行"}
    ledger = SourceLedger.from_mapping(raw["source_ledger"])
    assert ledger.status("xlsx:评分表!R8C1").status == "unclaimed"
    blocking = [item["unit_id"] for item in raw["coverage"]["unclaimed"] if item["blocking"]]
    assert blocking == ["xlsx:评分表!R8C1"]
    assert "E4" not in {t["code"] for t in raw["triggers"]}  # 合计 50 与三项之和一致


def test_multi_judgement_file_import_rule_becomes_review_only_with_blocker():
    graph = _complex_graph()
    method = next(item for item in graph["criteria"] if item["name"] == "方法设计")
    assert method["scoring_mode"] == "review_only"
    assert method["deduction_rules_structured"] == []
    codes = {(b["code"], b["criterion_code"]) for b in graph["compilation"]["blockers"]}
    assert ("DEDUCTION_RULE_NEEDS_SPLIT", method["code"]) in codes
