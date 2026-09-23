"""阶段 6 ③④：差异计算、合入计划、按确认结构导入与从台账重新解析。"""

import pytest

from backend.app.services.rubric_import import pipeline
from backend.app.services.rubric_import.sources.units import SourceLedger
from backend.app.services.rubric_import.suggestions import diff_criteria
from backend.app.services.rubric_import.suggestions import merge_plan
from backend.app.services.rubric_import.suggestions import structure_fingerprint
from backend.app.tests import rubric_parse_fixtures as fx
from backend.app.tests.test_rubric_parse_baseline import _command

CURRENT = [
    {"code": "C01", "name": "选题意义", "max_score": 10, "description": None, "deduction_rules": []},
    {"code": "C02", "name": "文献综述", "max_score": 10, "description": "综述充分", "deduction_rules": []},
    {"code": "C03", "name": "旧项目", "max_score": 5, "description": None, "deduction_rules": []},
]
PROPOSED = [
    {"code": "C01", "name": "选题意义", "max_score": 10, "description": "有价值", "deduction_rules": [], "row_number": 4},
    {"code": "C02", "name": "文献综述", "max_score": 15, "description": "综述充分", "deduction_rules": [], "row_number": 5},
    {"code": "C03", "name": "写作规范", "max_score": 5, "description": None, "deduction_rules": [], "row_number": 7},
]


def _kinds(items):
    return {(i["kind"], i.get("code"), i.get("field")) for i in items}


def test_diff_classifies_new_fill_modify_and_removed():
    items = diff_criteria(CURRENT, PROPOSED)
    assert _kinds(items) == {
        ("fill", "C01", "description"),
        ("modify", "C02", "max_score"),
        ("conflict", "C03", None),  # 新项沿用了已有项的编号
        ("removed", "C03", None),
    }
    modify = next(i for i in items if i["kind"] == "modify")
    assert (modify["before"], modify["after"]) == (10.0, 15.0)


def test_diff_new_item_when_code_is_free_and_duplicate_names_conflict():
    proposed = [*PROPOSED[:2], {**PROPOSED[2], "code": "C09"},
                {"code": "C10", "name": "写作规范", "max_score": 1, "description": None, "deduction_rules": [],
                 "row_number": 8}]
    items = diff_criteria(CURRENT, proposed)
    assert ("new", "C09", None) not in _kinds(items)  # 同名重复 → 两条都是冲突
    assert {("conflict", "C09", None), ("conflict", "C10", None)} <= _kinds(items)
    clean = diff_criteria(CURRENT, [*PROPOSED[:2], {**PROPOSED[2], "code": "C09"}])
    new = next(i for i in clean if i["kind"] == "new")
    assert (new["code"], new["row_number"], new["after"]["name"]) == ("C09", 7, "写作规范")


def test_merge_plan_requires_confirmation_for_modifications():
    items = diff_criteria(CURRENT[:2], [*PROPOSED[:2], {**PROPOSED[2], "code": "C09"}])
    ids = {i["kind"]: i["id"] for i in items}
    plan = merge_plan(items, confirm=set())
    assert plan["blocked"] == []
    # 未确认的修改 → 保留当前值
    assert plan["keep_values"] == [{"row_number": 5, "field": "max_score", "value": 10.0}]
    assert merge_plan(items, confirm={ids["modify"]})["keep_values"] == []
    assert merge_plan(items, exclude={ids["new"]})["exclude_rows"] == [7]


def test_removed_criteria_always_block_merge():
    """已有评分项被旧版本原子规则引用（外键），按结构重新解析不能删除它们：只能增、改，不能减。"""

    items = diff_criteria(CURRENT, [*PROPOSED[:2], {**PROPOSED[2], "code": "C09"}])
    removed = next(i for i in items if i["kind"] == "removed")
    assert merge_plan(items)["blocked"] == [removed["id"]]
    assert merge_plan(items, confirm={removed["id"]})["blocked"] == [removed["id"]]


def test_merge_plan_blocks_on_unresolved_conflicts():
    items = diff_criteria(CURRENT[:2], [*PROPOSED[:2], {**PROPOSED[2], "code": "C02", "name": "写作规范"}])
    conflict = next(i for i in items if i["kind"] == "conflict")
    assert merge_plan(items)["blocked"] == [conflict["id"]]
    assert merge_plan(items, exclude={conflict["id"]})["blocked"] == []


def test_fingerprint_depends_on_ledger_criteria_and_structure():
    base = structure_fingerprint({"units": [{"unit_id": "a", "text": "x"}]}, CURRENT, {"sheet": "S"})
    assert base == structure_fingerprint({"units": [{"unit_id": "a", "text": "x"}]}, CURRENT, {"sheet": "S"})
    assert base != structure_fingerprint({"units": [{"unit_id": "a", "text": "y"}]}, CURRENT, {"sheet": "S"})
    assert base != structure_fingerprint({"units": [{"unit_id": "a", "text": "x"}]}, CURRENT[:1], {"sheet": "S"})
    assert base != structure_fingerprint({"units": [{"unit_id": "a", "text": "x"}]}, CURRENT, {"sheet": "T"})


OVERRIDE = {"sheet": "Sheet", "header_row": 1, "column_mapping": {"name": 1, "max_score": 2, "description": 3}}


def test_import_with_structure_override_parses_unrecognised_header():
    with pytest.raises(ValueError):
        pipeline.prepare_file_import(command=_command(None), rules_bytes=fx.unmapped_header_xlsx())
    graph = pipeline.prepare_file_import(
        command=_command(None), rules_bytes=fx.unmapped_header_xlsx(), structure_override=OVERRIDE
    ).to_mapping()
    assert [(c["name"], c["max_score"], c["description"]) for c in graph["criteria"]] == [("选题", "10", "选题新颖")]
    assert graph["compilation"]["raw_parse_output"]["structure_override"]["column_mapping"]["max_score"] == 2


def _complex_raw():
    graph = pipeline.prepare_file_import(command=_command(None), rules_bytes=fx.complex_rules_xlsx()).to_mapping()
    return graph


def test_reparse_from_ledger_applies_confirmed_structure_and_keeps_human_decisions():
    graph = _complex_raw()
    raw = graph["compilation"]["raw_parse_output"]
    ledger = SourceLedger.from_mapping(raw["source_ledger"])
    ledger.mark("xlsx:评分表!R1C1", "context", extracted_by="human")
    raw["source_ledger"] = ledger.to_mapping()
    override = {"sheet": "评分表", "header_row": 3,
                "column_mapping": {"dimension": 1, "name": 2, "max_score": 3, "description": 4, "deduction_rules": 5},
                "row_types": {"5": "global_rule"}}
    reparsed = pipeline.prepare_structure_reparse(
        command=_command(None), raw_parse_output=raw, artifacts=graph["artifacts"], structure_override=override,
        keep_values=[{"row_number": 4, "field": "name", "value": "选题价值"}],
    ).to_mapping()
    assert [c["name"] for c in reparsed["criteria"]] == ["选题价值", "方法设计"]
    new_raw = reparsed["compilation"]["raw_parse_output"]
    new_ledger = SourceLedger.from_mapping(new_raw["source_ledger"])
    assert new_ledger.status("xlsx:评分表!R1C1").extracted_by == "human"
    assert new_ledger.status("xlsx:评分表!R5C2").status == "unclaimed"
    assert new_raw["structure_override"]["row_types"] == {"5": "global_rule"}
    assert reparsed["artifacts"] == graph["artifacts"]


def test_reparse_keeps_word_template_items_and_conflicts():
    graph = pipeline.prepare_file_import(
        command=_command(True), rules_bytes=fx.simple_rules_xlsx(), template_bytes=fx.template_docx_with_comments()
    ).to_mapping()
    raw = graph["compilation"]["raw_parse_output"]
    reparsed = pipeline.prepare_structure_reparse(
        command=_command(True), raw_parse_output=raw, artifacts=graph["artifacts"],
        structure_override={"sheet": "评分规则", "header_row": 1,
                            "column_mapping": {"code": 1, "name": 2, "max_score": 3, "description": 4,
                                               "evidence_hints": 5, "deduction_rules": 6}},
    ).to_mapping()
    assert reparsed["template_items"] == graph["template_items"]
    assert [c["code"] for c in reparsed["criteria"]] == ["C01", "C02"]
    new_ledger = SourceLedger.from_mapping(reparsed["compilation"]["raw_parse_output"]["source_ledger"])
    comment = next(u for u in new_ledger.units(doc_id="word") if u.kind == "comment")
    assert new_ledger.status(comment.unit_id).status == "consumed"
