"""AI 起草规则的 source_refs 必须指向真实输入字段（解析重构方案阶段 2）。"""

import pytest

from backend.app.services.rubric_import.ai_rule_drafter import AIRuleDraftValidationError
from backend.app.services.rubric_import.ai_rule_drafter import validate_ai_rule_draft
from backend.app.services.rubric_import.compiler import analyze_rule_input

CRITERION = {"code": "T02", "name": "需求分析", "max_score": 10, "description": "需求完整"}


def _draft(source_refs):
    return {
        "schema_version": "ai-deduction-draft@1",
        "criterion_code": "T02",
        "input_assessment": analyze_rule_input(["需求边界表达不清"], criterion_code="T02"),
        "rule_groups": [{
            "group_code": "G1", "issue": "需求边界不清", "mutex_group": "G1-SEVERITY", "cap_points": 4,
            "rules": [{"severity": "minor", "trigger": "边界不清", "points": 2, "reason": "r",
                       "repeat_policy": "once", "source": "ai_interpreted_user_text",
                       "source_refs": source_refs}],
        }],
    }


@pytest.mark.parametrize("refs", [
    ["/criteria/T02/deduction_rules/0"],
    ["/criteria/T02/description"],
    ["/criterion/description"],
    ["/criterion/evidence_hints/0"],
    ["/input_analysis/unresolved_segments/0"],
])
def test_refs_pointing_at_real_inputs_are_accepted(refs):
    assert validate_ai_rule_draft(_draft(refs), criterion=CRITERION)


@pytest.mark.parametrize("refs", [
    ["/criteria/T02/deduction_rules/7"],  # 不存在的原文段落
    ["/criteria/T99/description"],  # 其他评分项
    ["/criterion/secret_field"],
    ["第 3 页"],
    ["/criteria/T02/deduction_rules/0", "/made/up"],
])
def test_fabricated_refs_are_rejected(refs):
    with pytest.raises(AIRuleDraftValidationError) as exc:
        validate_ai_rule_draft(_draft(refs), criterion=CRITERION)
    assert exc.value.code == "AI_DRAFT_SOURCE_INVALID"


def test_drafter_repairs_fabricated_source_once():
    from backend.app.services.rubric_import.ai_rule_drafter import draft_deduction_rules

    def group(refs):
        return {"rule_groups": [{"group_code": "G1", "issue": "需求边界不清", "mutex_group": "G1-S", "cap_points": 4, "rules": [
            {"severity": "minor", "trigger": "边界不清", "points": 2, "reason": "r", "repeat_policy": "once",
             "source": "ai_interpreted_user_text", "source_refs": refs}]}]}

    class Scorer:
        provider = "fake"
        model_name = "fake"

        def __init__(self):
            self.responses = [group(["/made/up"]), group(["/criteria/T02/deduction_rules/0"])]
            self.instructions = []

        def complete_json(self, instructions, payload):
            self.instructions.append(instructions)
            return self.responses.pop(0)

    scorer = Scorer()
    draft = draft_deduction_rules(
        criterion=CRITERION, input_analysis=analyze_rule_input(["需求边界表达不清"], criterion_code="T02"),
        scorer=scorer, business_profile_key="thesis")
    assert draft["rule_groups"][0]["rules"][0]["source_refs"] == ["/criteria/T02/deduction_rules/0"]
    assert "AI_DRAFT_SOURCE_INVALID" in scorer.instructions[1]


def _assigned_analysis():
    """与 draft-deduction-rules 路由相同：评分说明 + 人工归入的原文单元。"""
    analysis = analyze_rule_input([], criterion_code="T01")
    assigned = [{"text": "需系统梳理国内外研究成果。", "source_refs": ["docx:p[101]"], "reason": "assigned_source"},
                {"text": "界面需照顾老年人的视觉体验。", "source_refs": ["docx:p[117]"], "reason": "assigned_source"}]
    analysis["unresolved_segments"].append({"text": "选题具有理论意义。", "source_refs": ["/criterion/description"]})
    analysis["unresolved_segments"].extend(assigned)
    analysis["source_refs"].extend(["docx:p[101]", "docx:p[117]"])
    analysis["needs_ai_draft"] = True
    return analysis


class _RefScorer:
    """按每批 payload 回答：模型只能看到、也只能引用本批的原文单元。"""
    provider = "fake"
    model_name = "fake"

    def __init__(self, refs_for, repair_refs_for=None):
        self.refs_for = refs_for
        self.repair_refs_for = repair_refs_for or refs_for
        self.calls = []

    def complete_json(self, instructions, payload):
        self.calls.append((instructions, payload))
        unit_ref = payload["batch"]["focus_units"][0]["source_refs"][0]
        pick = self.repair_refs_for if "上次输出未通过校验" in instructions else self.refs_for
        return {"rule_groups": [{"group_code": "T01_B_LIT", "issue": "文献综述不足", "mutex_group": "M", "cap_points": 4,
                                 "rules": [{"severity": "minor", "trigger": "未梳理国内外研究", "points": 2, "reason": "r",
                                            "repeat_policy": "once", "source": "ai_interpreted_user_text",
                                            "source_refs": pick(unit_ref)}]}]}


CRITERION_T01 = {"code": "T01", "name": "选题与开题", "max_score": 20, "description": "选题具有理论意义。"}


def test_batch_position_refs_are_rewritten_to_real_paragraph_ids():
    from backend.app.services.rubric_import.ai_rule_drafter import draft_deduction_rules

    # 三个单元切成三批：评分说明、docx:p[101]、docx:p[117]。模型写位置指针或去掉文档前缀的编号。
    scorer = _RefScorer(lambda ref: ["/batch/focus_units/0", ref.split(":")[-1] if ":" in ref else "/input_analysis/focus_units/0/text"])
    draft = draft_deduction_rules(criterion=CRITERION_T01, input_analysis=_assigned_analysis(),
                                  scorer=scorer, business_profile_key="thesis")

    assert len(scorer.calls) == 3
    assert [group["rules"][0]["source_refs"] for group in draft["rule_groups"]] == [
        ["/criterion/description"], ["docx:p[101]"], ["docx:p[117]"]]
    batch_two = next(payload for _, payload in scorer.calls if payload["batch"]["index"] == 2)
    assert "allowed_source_refs" in scorer.calls[0][0]
    assert batch_two["batch"]["allowed_source_refs"] == [
        "/criterion/description", "/criterion/evidence_hints", "/criterion/name", "docx:p[101]"]


def test_unknown_refs_still_fail_after_one_repair_and_name_the_bad_ref():
    from backend.app.services.rubric_import.ai_rule_drafter import draft_deduction_rules

    analysis = analyze_rule_input([], criterion_code="T01")
    analysis["unresolved_segments"].append({"text": "需系统梳理国内外研究成果。", "source_refs": ["docx:p[101]"]})
    analysis["source_refs"].append("docx:p[101]")
    analysis["needs_ai_draft"] = True
    scorer = _RefScorer(lambda _ref: ["/constraints/maximum_points"], lambda _ref: ["docx:p[999]"])
    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=CRITERION_T01, input_analysis=analysis,
                              scorer=scorer, business_profile_key="thesis")

    assert caught.value.code == "AI_DRAFT_SOURCE_INVALID"
    assert "docx:p[999]" in caught.value.message
    assert len(scorer.calls) == 2
    assert "AI_DRAFT_SOURCE_INVALID" in scorer.calls[1][0]


def test_ambiguous_bare_ids_are_not_guessed():
    from backend.app.services.rubric_import.ai_rule_drafter import _normalize_source_refs

    raw = {"rule_groups": [{"rules": [{"source_refs": ["p[1]"]}]}]}
    _normalize_source_refs(raw, {"focus_units": [{"source_refs": ["docx:p[1]"]}, {"source_refs": ["xlsx:p[1]"]}]})
    assert raw["rule_groups"][0]["rules"][0]["source_refs"] == ["p[1]"]
