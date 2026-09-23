"""阶段 6 ②：LLM 结构识别——预处理、上下文预算、输出校验与一次修复重试。"""

from io import BytesIO

import pytest
from openpyxl import Workbook

from backend.app.services.rubric_import.extraction.llm_structure import STRUCTURE_PROMPT_VERSION
from backend.app.services.rubric_import.extraction.llm_structure import StructureError
from backend.app.services.rubric_import.extraction.llm_structure import build_table_payload
from backend.app.services.rubric_import.extraction.llm_structure import recognize_structure
from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.services.rubric_import.sources.xlsx_adapter import load_xlsx
from backend.app.tests import rubric_parse_fixtures as fx
from backend.app.tests.test_rubric_unit_classifier import FakeScorer


def _sheets(data):
    return load_xlsx(data, SourceLedger(), doc_id="excel", doc_role="rules")


def _cells(payload, sheet, row):
    table = next(t for t in payload["table_data"] if t["sheet"] == sheet)
    return next(r["cells"] for r in table["rows"] if r["row"] == row)


def test_payload_uses_ids_merge_refs_number_tags_and_truncation():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "表"
    sheet.append(["评分项", "分值", "说明"])
    sheet.append(["选题", 10, "长" * 300])
    buffer = BytesIO()
    workbook.save(buffer)
    result = build_table_payload(_sheets(buffer.getvalue()), failure_codes=["E1"])
    cells = _cells(result["payload"], "表", "R2")
    assert cells["C2"] == "num:10"
    assert cells["C3"].startswith("长" * 120) and cells["C3"].endswith("…(共300字)")
    assert result["payload"]["failure_codes"] == ["E1"]
    columns = result["payload"]["table_data"][0]["columns"]
    assert columns[1] == {"col": "C2", "header_guess": "分值", "samples": ["num:10"]}
    assert result["estimate"]["calls"] == 1


def test_payload_marks_merged_cells_and_includes_requested_rows():
    result = build_table_payload(_sheets(fx.complex_rules_xlsx()), failure_codes=["E4"], extra_rows={"评分表": [9]},
                                 max_rows=5)
    assert _cells(result["payload"], "评分表", "R5")["C1"] == "↑R4C1"
    rows = [r["row"] for r in result["payload"]["table_data"][0]["rows"]]
    assert rows == ["R1", "R3", "R4", "R5", "R9"]  # 空行省略，前 5 行 + 合计行


def test_payload_is_compressed_to_budget_and_refused_when_impossible():
    workbook = Workbook()
    for index in range(60):
        workbook.active.append([f"评分项{index}", index, "说明" * 60])
    buffer = BytesIO()
    workbook.save(buffer)
    sheets = _sheets(buffer.getvalue())
    roomy = build_table_payload(sheets, failure_codes=["E1"], budget_chars=100_000)
    assert roomy["estimate"]["compression"] == "none"
    budget = roomy["estimate"]["chars"] - 1
    tight = build_table_payload(sheets, failure_codes=["E1"], budget_chars=budget)
    assert tight["estimate"]["chars"] <= budget
    assert tight["estimate"]["compression"] != "none"
    with pytest.raises(StructureError) as exc:
        build_table_payload(sheets, failure_codes=["E1"], budget_chars=200)
    assert exc.value.code == "STRUCTURE_BUDGET_EXCEEDED"


VALID = {
    "sheet": "Sheet", "header_row": "R1",
    "column_mapping": [{"col": "C1", "field": "name", "reason": "名称"},
                       {"col": "C2", "field": "max_score", "reason": "数字"},
                       {"col": "C3", "field": "description", "reason": "说明"}],
    "row_types": [{"row": "R2", "type": "criterion", "reason": "评分项"}],
    "unresolved": [],
}


def test_valid_output_becomes_normalized_override_with_reasons():
    scorer = FakeScorer(VALID)
    result = recognize_structure(_sheets(fx.unmapped_header_xlsx()), scorer, failure_codes=["E1"])
    assert result["override"] == {"sheet": "Sheet", "header_row": 1,
                                  "column_mapping": {"name": 1, "max_score": 2, "description": 3},
                                  "row_types": {"2": "criterion"}}
    assert result["reasons"]["columns"]["C2"] == "数字"
    assert result["prompt_version"] == STRUCTURE_PROMPT_VERSION
    instructions, payload = scorer.calls[0]
    assert "不可信" in instructions and "只识别结构" in instructions
    assert payload["table_data"][0]["sheet"] == "Sheet"


def test_ignore_and_unknown_columns_are_not_mapped():
    output = {**VALID, "column_mapping": [*VALID["column_mapping"][:2], {"col": "C3", "field": "ignore", "reason": "x"}]}
    result = recognize_structure(_sheets(fx.unmapped_header_xlsx()), FakeScorer(output), failure_codes=["E1"])
    assert result["override"]["column_mapping"] == {"name": 1, "max_score": 2}


def test_invalid_output_is_repaired_once_with_the_error_code():
    bad = {**VALID, "column_mapping": [{"col": "C1", "field": "name", "reason": "x"},
                                       {"col": "C3", "field": "max_score", "reason": "x"}]}
    scorer = FakeScorer(bad, VALID)
    result = recognize_structure(_sheets(fx.unmapped_header_xlsx()), scorer, failure_codes=["E1"])
    assert result["override"]["column_mapping"]["max_score"] == 2
    assert "SCORE_COLUMN_NOT_NUMERIC" in scorer.calls[1][0]


@pytest.mark.parametrize("output, code", [
    ({"sheet": "Sheet", "header_row": "R99", "column_mapping": [], "row_types": []}, "HEADER_ROW_INVALID"),
    ({**VALID, "column_mapping": [{"col": "C9", "field": "name", "reason": "x"}]}, "COLUMN_OUT_OF_RANGE"),
    ({**VALID, "row_types": [{"row": "R50", "type": "criterion", "reason": "x"}]}, "ROW_OUT_OF_RANGE"),
    ({**VALID, "sheet": "别的表"}, "SHEET_NOT_FOUND"),
    ("not json", "STRUCTURE_OUTPUT_INVALID"),
])
def test_repeatedly_invalid_output_is_rejected(output, code):
    with pytest.raises(StructureError) as exc:
        recognize_structure(_sheets(fx.unmapped_header_xlsx()), FakeScorer(output, output), failure_codes=["E1"])
    assert exc.value.code == code


def test_mock_connection_is_refused():
    with pytest.raises(StructureError) as exc:
        recognize_structure(_sheets(fx.unmapped_header_xlsx()), type("M", (), {"provider": "mock"})(),
                            failure_codes=["E1"])
    assert exc.value.code == "AI_CONNECTION_MISSING"
