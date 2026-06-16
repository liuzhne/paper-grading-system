from io import BytesIO

from openpyxl import Workbook

from backend.app.services.rubric_import.parser import _parse_sub_checks
from backend.app.services.rubric_import.parser import parse_excel_rules


def test_parse_sub_checks_lines_and_kinds():
    raw = "字数达标 | 确定性 | 4\n论证质量 | 语义 | 6"
    subs = _parse_sub_checks(raw)
    assert len(subs) == 2
    assert subs[0] == {"kind": "deterministic", "name": "字数达标", "criteria": "字数达标", "max_points": 4.0}
    assert subs[1]["kind"] == "llm_judgment"
    assert subs[1]["max_points"] == 6.0


def test_parse_sub_checks_kind_defaults_to_llm():
    subs = _parse_sub_checks("整体质量")
    assert subs == [{"kind": "llm_judgment", "name": "整体质量", "criteria": "整体质量", "max_points": 0.0}]


def _xlsx_with_sub_checks():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["编号", "评分项", "分值", "子检查"])
    sheet.append(["C1", "研究与写作", 10, "字数达标 | 规则 | 4\n论证质量 | 语义 | 6"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_excel_sub_checks_enable_hybrid():
    criteria, _ = parse_excel_rules(_xlsx_with_sub_checks())
    assert len(criteria) == 1
    criterion = criteria[0]
    # 提供子检查列 → 自动启用混合制
    assert criterion.criterion_type == "hybrid"
    assert [sub["kind"] for sub in criterion.sub_checks] == ["deterministic", "llm_judgment"]
    assert sum(sub["max_points"] for sub in criterion.sub_checks) == 10.0
