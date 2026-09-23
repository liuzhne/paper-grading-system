"""prepare_file_import 接入单元台账与覆盖率（阶段 1–2）。"""

from backend.app.services.rubric_import import pipeline
from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.tests import rubric_parse_fixtures as fx
from backend.app.tests.test_rubric_parse_baseline import _command


def _prepare(rules, template=None):
    return pipeline.prepare_file_import(
        command=_command(template), rules_bytes=rules, template_bytes=template
    ).to_mapping()


def _raw(graph):
    return graph["compilation"]["raw_parse_output"]


def test_raw_parse_output_keeps_existing_keys_and_adds_ledger_and_coverage():
    raw = _raw(_prepare(fx.simple_rules_xlsx()))
    assert {"sheet_name", "criteria", "source_rules", "template_items"} <= set(raw)
    ledger = SourceLedger.from_mapping(raw["source_ledger"])
    assert ledger.documents() == {"excel": "rules"}
    assert raw["coverage"]["documents"][0]["ratio"] == 1.0
    assert raw["extraction"]["header_row"] == 1
    assert raw["extraction"]["mapping"]["max_score"] == 3


def test_complex_workbook_coverage_lists_lost_content():
    raw = _raw(_prepare(fx.complex_rules_xlsx()))
    unclaimed = {item["text"] for item in raw["coverage"]["unclaimed"]}
    assert "表述要严谨" in unclaimed  # 无满分行
    assert "需结合答辩" in unclaimed  # 未映射列
    assert "XX大学本科毕业论文评分表" in unclaimed  # 表头上方
    extraction = raw["extraction"]
    # 阶段 3 起，整行合并的表尾说明（第 8 行）不再被误解析为评分项
    assert [row["row_number"] for row in extraction["dropped_rows"]] == [7, 8]
    assert extraction["total_row"] == {"row_number": 9, "declared_total": 50.0}
    assert extraction["ignored_sheets"][0]["title"] == "附加扣分"
    assert raw["coverage"]["documents"][0]["ratio"] < 1


def test_source_rule_cell_locator_points_at_real_row_range():
    graph = _prepare(fx.complex_rules_xlsx())
    locators = [item["cell_locator"] for item in graph["source_rules"]]
    assert locators == ["评分表!A4:E4", "评分表!A5:F5", "评分表!A6:E6"]


def test_template_units_are_registered_with_template_role():
    raw = _raw(_prepare(fx.simple_rules_xlsx(), fx.template_docx_with_comments()))
    ledger = SourceLedger.from_mapping(raw["source_ledger"])
    assert ledger.documents() == {"excel": "rules", "word": "template"}
    word = {u.text: ledger.status(u.unit_id) for u in ledger.units(doc_id="word")}
    assert word["此处需说明数据来源，缺失扣3分"].status == "consumed"
    assert word["此处需说明数据来源，缺失扣3分"].claimed_by[0].startswith("template_items.WORD-COMMENT-")
    assert word["说明研究背景、研究意义和论文结构。"].status == "context"
    assert word["第三章 研究方法"].status == "context"
    assert word["正文不少于800字，否则扣2分。"].status == "unclaimed"  # 命中规则信号，待处理
    doc = next(d for d in raw["coverage"]["documents"] if d["doc_id"] == "word")
    # 批注 + “正文不少于800字…” + “须有题注” + “图表”（论文 Profile 信号词）
    assert doc["denominator"] == 4
    assert doc["handled"] == 1
