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
