"""阶段 4：Word 正文/表格抽取、文档角色与双文件冲突。"""

import pytest
from docx import Document
from io import BytesIO

from backend.app.services.rubric_import import pipeline
from backend.app.services.rubric_import.extraction.docx_extractor import extract_docx_rules
from backend.app.services.rubric_import.sources.docx_adapter import load_docx
from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.tests import rubric_parse_fixtures as fx
from backend.app.tests.test_rubric_parse_baseline import _command


def _extract(data):
    ledger = SourceLedger()
    view = load_docx(data, ledger, doc_id="word", doc_role="rules")
    return extract_docx_rules(view, ledger), ledger


def _by_text(ledger):
    return {unit.text: ledger.status(unit.unit_id) for unit in ledger.units()}


def test_word_table_criteria_are_extracted_and_units_claimed():
    result, ledger = _extract(fx.rules_docx())
    assert [(r.criterion.code, r.criterion.name, r.criterion.max_score) for r in result.records] == [
        ("K01", "问题分析", 40.0), ("K02", "方案设计", 40.0), ("K03", "报告规范", 20.0)]
    assert result.sheet_title == "表格1"
    status = _by_text(ledger)
    assert status["问题分析"].claimed_by == ("K01.name",)
    assert status["评分项"].status == "structural"
    assert status["课程报告评分标准"].status == "context"  # 标题
    assert status["总分100分，各评分项如下。"].status == "structural"  # 总分说明
    assert status["迟交一天扣5分。"].status == "unclaimed"  # 正文里的疑似规则


def test_word_paragraph_criteria_are_extracted_when_no_table():
    result, ledger = _extract(fx.rules_docx_paragraphs())
    assert [(r.criterion.name, r.criterion.max_score, r.criterion.description) for r in result.records] == [
        ("选题意义", 10.0, "选题具有实际价值。"), ("方案设计", 60.0, "方案完整可行。"), ("报告规范", 30.0, "格式规范。")]
    assert result.sheet_title == "正文"
    assert _by_text(ledger)["一、选题意义（10分）：选题具有实际价值。"].status == "consumed"


def test_word_without_criteria_raises():
    document = Document()
    document.add_paragraph("本文档没有评分项。")
    buffer = BytesIO()
    document.save(buffer)
    with pytest.raises(ValueError, match="Word 未解析到有效评分项"):
        _extract(buffer.getvalue())


def test_word_only_import_uses_word_as_rules_document():
    graph = pipeline.prepare_file_import(
        command=_command(None), rules_bytes=None, template_bytes=fx.rules_docx()
    ).to_mapping()
    assert [item["name"] for item in graph["criteria"]] == ["问题分析", "方案设计", "报告规范"]
    assert [a["artifact_type"] for a in graph["artifacts"]] == ["word"]
    assert {s["artifact_token"] for s in graph["source_rules"]} == {"word"}
    assert graph["source_rules"][0]["cell_locator"] == "docx:tbl[0]/r1"
    raw = graph["compilation"]["raw_parse_output"]
    ledger = SourceLedger.from_mapping(raw["source_ledger"])
    assert ledger.documents() == {"word": "rules"}
    assert raw["extraction"]["sheet_title"] == "表格1"
    assert [i["text"] for i in raw["coverage"]["unclaimed"] if i["blocking"]] == ["迟交一天扣5分。"]


def test_no_files_is_rejected():
    with pytest.raises(ValueError):
        pipeline.prepare_file_import(command=_command(None), rules_bytes=None, template_bytes=None)


def test_dual_upload_records_word_conflicts_and_evidence():
    graph = pipeline.prepare_file_import(
        command=_command(True), rules_bytes=fx.simple_rules_xlsx(), template_bytes=fx.template_docx_with_rule_table()
    ).to_mapping()
    assert [item["code"] for item in graph["criteria"]] == ["C01", "C02"]  # Excel 为结构主干
    raw = graph["compilation"]["raw_parse_output"]
    conflicts = {(c["type"], c["word_code"]): c for c in raw["source_conflicts"]}
    assert set(conflicts) == {("score_mismatch", "C02"), ("word_only", "C03")}
    assert conflicts[("score_mismatch", "C02")]["excel_max_score"] == 15.0
    assert conflicts[("score_mismatch", "C02")]["word_max_score"] == 10.0
    ledger = SourceLedger.from_mapping(raw["source_ledger"])
    status = {u.text: ledger.status(u.unit_id) for u in ledger.units(doc_id="word")}
    assert status["研究方法"].claimed_by == ("C01.word_evidence",)
    assert status["创新性"].status == "unclaimed"
    assert status["文献综述"].status == "unclaimed"


def test_excel_only_import_has_no_conflicts():
    graph = pipeline.prepare_file_import(
        command=_command(None), rules_bytes=fx.simple_rules_xlsx(), template_bytes=None
    ).to_mapping()
    assert graph["compilation"]["raw_parse_output"]["source_conflicts"] == []
