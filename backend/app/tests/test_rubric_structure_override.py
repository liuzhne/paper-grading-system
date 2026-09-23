"""阶段 6 ①：按用户确认的结构确定性抽取；从台账重建表格。"""

import pytest

from backend.app.services.rubric_import.extraction.structure_override import StructureOverrideError
from backend.app.services.rubric_import.extraction.structure_override import extract_with_override
from backend.app.services.rubric_import.extraction.structure_override import normalize_override
from backend.app.services.rubric_import.sources.rebuild import docx_view_from_ledger
from backend.app.services.rubric_import.sources.rebuild import sheets_from_ledger
from backend.app.services.rubric_import.sources.docx_adapter import load_docx
from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.services.rubric_import.sources.xlsx_adapter import load_xlsx
from backend.app.tests import rubric_parse_fixtures as fx

OVERRIDE = {"sheet": "Sheet", "header_row": 1, "column_mapping": {"name": 1, "max_score": 2, "description": 3}}


def _sheets(data):
    ledger = SourceLedger()
    return load_xlsx(data, ledger, doc_id="excel", doc_role="rules"), ledger


def test_override_extracts_table_that_aliases_cannot_recognise():
    sheets, ledger = _sheets(fx.unmapped_header_xlsx())
    result = extract_with_override(sheets, ledger, normalize_override(OVERRIDE, sheets))
    assert [(r.criterion.name, r.criterion.max_score, r.criterion.description) for r in result.records] == [
        ("选题", 10.0, "选题新颖")]
    assert ledger.status("xlsx:Sheet!R1C1").status == "structural"
    assert ledger.status("xlsx:Sheet!R2C1").claimed_by == ("C01.name",)
    assert result.mapping == {"name": 0, "max_score": 1, "description": 2}


def test_row_types_drop_global_rules_and_mark_totals_and_dimensions():
    sheets, ledger = _sheets(fx.complex_rules_xlsx())
    override = normalize_override({
        "sheet": "评分表", "header_row": 3,
        "column_mapping": {"dimension": 1, "name": 2, "max_score": 3, "description": 4, "deduction_rules": 5},
        "row_types": {"5": "global_rule", "8": "global_rule", "9": "total"},
    }, sheets)
    result = extract_with_override(sheets, ledger, override)
    assert [r.criterion.name for r in result.records] == ["选题意义", "方法设计"]
    assert [(d["row_number"], d["reason"]) for d in result.dropped_rows] == [
        (5, "用户确认：全局规则"), (7, "未找到满分"), (8, "用户确认：全局规则")]
    assert result.total_row == {"row_number": 9, "declared_total": 50.0}
    assert ledger.status("xlsx:评分表!R5C2").status == "unclaimed"


@pytest.mark.parametrize("bad, code", [
    ({**OVERRIDE, "sheet": "不存在"}, "SHEET_NOT_FOUND"),
    ({**OVERRIDE, "header_row": 9}, "HEADER_ROW_INVALID"),
    ({**OVERRIDE, "column_mapping": {"max_score": 2}}, "NAME_COLUMN_REQUIRED"),
    ({**OVERRIDE, "column_mapping": {"name": 1}}, "SCORE_COLUMN_REQUIRED"),
    ({**OVERRIDE, "column_mapping": {"name": 1, "max_score": 1}}, "COLUMN_REUSED"),
    ({**OVERRIDE, "column_mapping": {"name": 1, "max_score": 3}}, "SCORE_COLUMN_NOT_NUMERIC"),
    ({**OVERRIDE, "column_mapping": {"name": 1, "max_score": 2, "bogus": 3}}, "FIELD_INVALID"),
    ({**OVERRIDE, "column_mapping": {"name": 1, "max_score": 7}}, "COLUMN_OUT_OF_RANGE"),
    ({**OVERRIDE, "row_types": {"2": "banana"}}, "ROW_TYPE_INVALID"),
])
def test_invalid_overrides_are_rejected(bad, code):
    sheets, _ = _sheets(fx.unmapped_header_xlsx())
    with pytest.raises(StructureOverrideError) as exc:
        normalize_override(bad, sheets)
    assert exc.value.code == code


def test_sheets_rebuilt_from_ledger_match_the_original_rows():
    original, ledger = _sheets(fx.complex_rules_xlsx())
    restored = sheets_from_ledger(SourceLedger.from_mapping(ledger.to_mapping()), doc_id="excel")
    assert [s.title for s in restored] == ["评分表", "附加扣分"]
    for before, after in zip(original, restored):
        trimmed = [tuple(v if v not in (None, "") else None for v in row) for row in before.rows]
        assert [tuple(str(v) if v is not None else None for v in row) for row in trimmed] == [
            tuple(str(v) if v is not None else None for v in row) for row in after.rows[: len(trimmed)]]
        assert after.unit_at(4, 0) == before.unit_at(4, 0)


def test_docx_view_rebuilt_from_ledger_keeps_blocks_and_tables():
    ledger = SourceLedger()
    original = load_docx(fx.rules_docx(), ledger, doc_id="word", doc_role="rules")
    restored = docx_view_from_ledger(SourceLedger.from_mapping(ledger.to_mapping()), doc_id="word")
    assert [(b["type"], b.get("unit_id")) for b in restored.blocks if b["type"] != "table"] == [
        (b["type"], b.get("unit_id")) for b in original.blocks if b["type"] != "table"]
    assert restored.tables == original.tables
