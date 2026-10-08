import httpx
import pytest

from backend.app.services.rubric_import.ai_rule_drafter import (
    AIRuleDraftValidationError,
    AI_RULE_DRAFT_MAX_CONCURRENCY,
    AI_RULE_DRAFT_MAX_OUTPUT_TOKENS,
    AI_RULE_DRAFT_TIMEOUT_SECONDS,
    draft_deduction_rules,
    validate_ai_rule_draft,
)
from backend.app.services.rubric_import.compiler import analyze_rule_input


class _DraftScorer:
    provider = "openai_compatible"
    model_name = "draft-test-model"
    model_version = "test-v1"

    def __init__(self, result):
        self.result = result
        self.payloads = []

    def complete_json(self, instructions, payload):
        self.payloads.append((instructions, payload))
        return self.result


class _RejectedDraftScorer:
    provider = "openai_compatible"
    model_name = "openai/gpt-oss-120b"
    model_version = "chat-completions"

    def complete_json(self, _instructions, _payload):
        request = httpx.Request(
            "POST", "https://api.groq.com/openai/v1/chat/completions"
        )
        response = httpx.Response(400, request=request)
        raise httpx.HTTPStatusError(
            "provider rejected request", request=request, response=response
        )


def _criterion(**overrides):
    value = {
        "code": "T02",
        "name": "需求分析",
        "max_score": 20,
        "description": "需求分析应完整、明确并可验证。",
        "evidence_hints": ["需求分析"],
        "deduction_rules": [],
        "scoring_mode": "deductive",
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    ("rules", "state", "parsed_count", "unresolved_count"),
    [
        ([], "absent", 0, 0),
        (["缺少需求说明，扣 3 分"], "parsed", 1, 0),
        (
            ["缺少需求说明，扣 3 分", "需求边界表达不清"],
            "partial",
            1,
            1,
        ),
        (["需求边界表达不清"], "unparsed", 0, 1),
    ],
)
def test_rule_input_analysis_distinguishes_absence_from_parse_failure(
    rules,
    state,
    parsed_count,
    unresolved_count,
):
    analysis = analyze_rule_input(rules, criterion_code="T02")

    assert analysis["input_state"] == state
    assert len(analysis["parsed_rules"]) == parsed_count
    assert len(analysis["unresolved_segments"]) == unresolved_count
    assert analysis["needs_ai_draft"] is (state in {"absent", "partial", "unparsed"})
    if rules:
        assert analysis["raw_segments"] == rules


def test_range_input_requires_severity_expansion_instead_of_silent_maximum():
    analysis = analyze_rule_input(
        ["需求分析存在缺失，扣 2 到 6 分"],
        criterion_code="T02",
    )

    assert analysis["input_state"] == "parsed"
    assert analysis["needs_severity_expansion"] is True
    assert analysis["parsed_rules"][0]["points"] == 6
    assert analysis["parsed_rules"][0]["source_refs"] == [
        "/criteria/T02/deduction_rules/0"
    ]


def test_ai_drafter_preserves_user_input_and_returns_confirmable_severity_rules():
    scorer = _DraftScorer(
        {
            "rule_groups": [
                {
                    "group_code": "T02-REQUIREMENTS",
                    "issue": "需求分析不完整",
                    "mutex_group": "T02-REQUIREMENTS-SEVERITY",
                    "cap_points": 6,
                    "rules": [
                        {
                            "severity": "minor",
                            "trigger": "个别非核心需求描述不完整",
                            "points": 2,
                            "reason": "需求说明存在局部缺失",
                            "repeat_policy": "once",
                            "source": "ai_interpreted_user_text",
                            "source_refs": ["/criteria/T02/deduction_rules/0"],
                        },
                        {
                            "severity": "moderate",
                            "trigger": "多个需求边界不清",
                            "points": 4,
                            "reason": "需求边界影响后续设计",
                            "repeat_policy": "once",
                            "source": "ai_interpreted_user_text",
                            "source_refs": ["/criteria/T02/deduction_rules/0"],
                        },
                        {
                            "severity": "severe",
                            "trigger": "核心需求缺失",
                            "points": 6,
                            "reason": "无法支撑后续系统设计",
                            "repeat_policy": "once",
                            "source": "ai_interpreted_user_text",
                            "source_refs": ["/criteria/T02/deduction_rules/0"],
                        },
                    ],
                }
            ]
        }
    )
    criterion = _criterion(deduction_rules=["需求分析不完整，扣 2 到 6 分"])
    analysis = analyze_rule_input(
        criterion["deduction_rules"],
        criterion_code=criterion["code"],
    )

    draft = draft_deduction_rules(
        criterion=criterion,
        input_analysis=analysis,
        scorer=scorer,
        business_profile_key="thesis",
    )

    assert draft["schema_version"] == "ai-deduction-draft@1"
    assert draft["criterion_code"] == "T02"
    assert draft["requires_confirmation"] is True
    assert [item["points"] for item in draft["rule_groups"][0]["rules"]] == [2, 4, 6]
    assert all(
        item["confirmed"] is False
        for item in draft["rule_groups"][0]["rules"]
    )
    sent_payload = scorer.payloads[0][1]
    assert sent_payload["input_analysis"]["raw_segments"] == [
        "需求分析不完整，扣 2 到 6 分"
    ]


def test_ai_rule_validator_rejects_non_monotonic_severity_points():
    invalid = {
        "schema_version": "ai-deduction-draft@1",
        "criterion_code": "T02",
        "requires_confirmation": True,
        "input_assessment": {"input_state": "absent"},
        "rule_groups": [
            {
                "group_code": "T02-R1",
                "issue": "需求缺失",
                "mutex_group": "T02-R1-SEVERITY",
                "cap_points": 6,
                "rules": [
                    {
                        "severity": "minor",
                        "trigger": "轻微缺失",
                        "points": 4,
                        "reason": "轻微",
                        "repeat_policy": "once",
                        "source": "ai_inferred",
                        "source_refs": ["/criteria/T02/description"],
                    },
                    {
                        "severity": "severe",
                        "trigger": "严重缺失",
                        "points": 3,
                        "reason": "严重",
                        "repeat_policy": "once",
                        "source": "ai_inferred",
                        "source_refs": ["/criteria/T02/description"],
                    },
                ],
            }
        ],
    }

    with pytest.raises(AIRuleDraftValidationError) as exc_info:
        validate_ai_rule_draft(invalid, criterion=_criterion())

    assert exc_info.value.code == "SEVERITY_RULES_NOT_MONOTONIC"


def test_draft_endpoint_rejects_mock_llm_with_actionable_problem(client):
    created = client.post(
        "/api/rubrics",
        json={
            "name": "AI draft unavailable",
            "version": "v1",
            "total_score": 20,
            "criteria": [_criterion(scoring_mode="review_only")],
        },
    )
    assert created.status_code == 200, created.text

    response = client.post(
        f"/api/rubrics/{created.json()['id']}/draft-deduction-rules",
        json={"criteria": [_criterion()]},
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "AI_DRAFT_CONNECTION_MISSING"
    assert detail["user_action"]
    assert detail["retryable"] is False


def test_draft_endpoint_returns_structured_suggestions_without_mutating_rubric(
    client,
    monkeypatch,
):
    from backend.app.api.routes import rubrics as rubric_routes

    created = client.post(
        "/api/rubrics",
        json={
            "name": "AI draft suggestion",
            "version": "v1",
            "total_score": 20,
            "criteria": [_criterion(scoring_mode="review_only")],
        },
    )
    assert created.status_code == 200, created.text
    rubric_id = created.json()["id"]
    before = client.get(f"/api/rubrics/{rubric_id}/execution-draft").json()
    scorer = _DraftScorer(
        {
            "rule_groups": [
                {
                    "group_code": "T02-R1",
                    "issue": "核心需求缺失",
                    "mutex_group": "T02-R1-SEVERITY",
                    "cap_points": 6,
                    "rules": [
                        {
                            "severity": "minor",
                            "trigger": "个别需求描述不完整",
                            "points": 2,
                            "reason": "局部缺失",
                            "repeat_policy": "once",
                            "source": "ai_inferred",
                            "source_refs": ["/criteria/T02/description"],
                        },
                        {
                            "severity": "severe",
                            "trigger": "核心需求完全缺失",
                            "points": 6,
                            "reason": "核心缺失",
                            "repeat_policy": "once",
                            "source": "ai_inferred",
                            "source_refs": ["/criteria/T02/description"],
                        },
                    ],
                }
            ]
        }
    )
    monkeypatch.setattr(rubric_routes, "get_llm_scorer", lambda *a, **k: scorer)

    response = client.post(
        f"/api/rubrics/{rubric_id}/draft-deduction-rules",
        json={"criteria": [_criterion()]},
    )

    assert response.status_code == 200, response.text
    draft = response.json()["items"][0]["draft"]
    assert draft["requires_confirmation"] is True
    assert draft["generation_metadata"]["fingerprint"]
    assert all(
        item["confirmed"] is False
        for item in draft["rule_groups"][0]["rules"]
    )
    after = client.get(f"/api/rubrics/{rubric_id}/execution-draft").json()
    assert after["active_compilation"]["id"] == before["active_compilation"]["id"]
    assert len(after["compilations"]) == 1


def test_ai_drafter_reports_non_retryable_provider_rejection_without_body():
    with pytest.raises(AIRuleDraftValidationError) as captured:
        draft_deduction_rules(
            criterion=_criterion(),
            input_analysis=analyze_rule_input([], criterion_code="T02"),
            scorer=_RejectedDraftScorer(),
            business_profile_key="thesis",
        )

    assert captured.value.code == "AI_DRAFT_PROVIDER_REJECTED"
    assert "拒绝" in captured.value.message
    assert "测试当前 AI 连接" in captured.value.user_action
    assert "groq" not in str(captured.value).lower()


def test_draft_endpoint_maps_provider_rejection_to_safe_non_retryable_problem(
    client,
    monkeypatch,
):
    from backend.app.api.routes import rubrics as rubric_routes

    created = client.post(
        "/api/rubrics",
        json={
            "name": "AI provider rejection",
            "version": "v1",
            "total_score": 20,
            "criteria": [_criterion(scoring_mode="review_only")],
        },
    )
    assert created.status_code == 200, created.text
    monkeypatch.setattr(
        rubric_routes, "get_llm_scorer", lambda *a, **k: _RejectedDraftScorer()
    )

    response = client.post(
        f"/api/rubrics/{created.json()['id']}/draft-deduction-rules",
        json={"criteria": [_criterion()]},
    )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "AI_DRAFT_PROVIDER_REJECTED"
    assert detail["retryable"] is False
    assert "测试当前 AI 连接" in detail["user_action"]


def test_range_and_unconfirmed_ai_rules_remain_blocked_until_confirmation(client):
    created = client.post(
        "/api/rubrics",
        json={
            "name": "Pending severity confirmation",
            "version": "v1",
            "total_score": 20,
            "criteria": [_criterion(scoring_mode="review_only")],
        },
    )
    assert created.status_code == 200, created.text
    rubric_id = created.json()["id"]
    predecessor = client.get(f"/api/rubrics/{rubric_id}/execution-draft").json()[
        "active_compilation"
    ]["id"]

    range_only = client.post(
        f"/api/rubrics/{rubric_id}/recompile",
        json={
            "supersedes_compilation_id": predecessor,
            "version": "v1",
            "criteria": [
                _criterion(
                    scoring_mode="deductive",
                    deduction_rules=["需求不完整，扣 2 到 6 分"],
                )
            ],
        },
    )
    assert range_only.status_code == 200, range_only.text
    range_draft = client.get(f"/api/rubrics/{rubric_id}/execution-draft").json()
    assert range_draft["active_compilation"]["status"] == "blocked"
    assert {
        item["code"] for item in range_draft["active_compilation"]["blockers"]
    } == {"SEVERITY_CONFIRMATION_REQUIRED"}

    predecessor = range_draft["active_compilation"]["id"]
    unconfirmed = client.post(
        f"/api/rubrics/{rubric_id}/recompile",
        json={
            "supersedes_compilation_id": predecessor,
            "version": "v1",
            "criteria": [
                _criterion(
                    scoring_mode="deductive",
                    deduction_rules_structured=[
                        {
                            "trigger": "核心需求缺失",
                            "points": 6,
                            "reason": "核心需求缺失",
                            "source": "ai_inferred",
                            "source_refs": ["/criteria/T02/description"],
                            "confirmed": False,
                            "mutex_group": "T02-R1-SEVERITY",
                            "cap_points": 6,
                        }
                    ],
                )
            ],
        },
    )
    assert unconfirmed.status_code == 200, unconfirmed.text
    ai_draft = client.get(f"/api/rubrics/{rubric_id}/execution-draft").json()
    codes = {item["code"] for item in ai_draft["active_compilation"]["blockers"]}
    assert "AI_DRAFT_PENDING_CONFIRMATION" in codes


def test_recompile_converts_explicit_natural_language_rules_and_publishes(client):
    created = client.post(
        "/api/rubrics",
        json={
            "name": "Natural language recovery",
            "version": "v1",
            "total_score": 20,
            "criteria": [_criterion(scoring_mode="llm_direct")],
        },
    )
    assert created.status_code == 200, created.text
    rubric_id = created.json()["id"]
    before = client.get(f"/api/rubrics/{rubric_id}/execution-draft").json()
    predecessor_id = before["active_compilation"]["id"]
    assert before["active_compilation"]["status"] == "blocked"

    recovered = client.post(
        f"/api/rubrics/{rubric_id}/recompile",
        json={
            "supersedes_compilation_id": predecessor_id,
            "version": "v2",
            "reason": "用户确认自然语言扣分规则",
            "criteria": [
                _criterion(
                    scoring_mode="deductive",
                    deduction_rules=["核心需求缺失，扣 6 分"],
                )
            ],
        },
    )
    assert recovered.status_code == 200, recovered.text

    after = client.get(f"/api/rubrics/{rubric_id}/execution-draft").json()
    active = after["active_compilation"]
    assert active["id"] != predecessor_id
    assert active["status"] == "validated"
    assert active["blockers"] == []
    assert {item["status"] for item in after["compilations"]} == {
        "superseded",
        "validated",
    }

    for rule in active["rules"]:
        submitted = client.post(
            f"/api/rubrics/{rubric_id}/rules/{rule['rule_code']}/submit-review",
            json={"reason": "提交评分规则审核"},
        )
        assert submitted.status_code == 200, submitted.text
        approved = client.post(
            f"/api/rubrics/{rubric_id}/rules/{rule['rule_code']}/approve",
            json={"reason": "确认评分规则"},
        )
        assert approved.status_code == 200, approved.text

    submitted_rubric = client.post(f"/api/rubrics/{rubric_id}/submit-review")
    assert submitted_rubric.status_code == 200, submitted_rubric.text
    published = client.post(
        f"/api/rubrics/{rubric_id}/publish",
        json={"compilation_id": active["id"], "reason": "发布已确认模板"},
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"


def test_blocked_execution_draft_cannot_enter_review(client):
    created = client.post(
        "/api/rubrics",
        json={
            "name": "Blocked review",
            "version": "v1",
            "total_score": 20,
            "criteria": [_criterion(scoring_mode="llm_direct")],
        },
    )
    assert created.status_code == 200, created.text

    response = client.post(f"/api/rubrics/{created.json()['id']}/submit-review")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "RUBRIC_REVIEW_BLOCKED"
    assert detail["user_action"]
    rubric = client.get(f"/api/rubrics/{created.json()['id']}").json()
    assert rubric["status"] == "draft"


def test_template_center_frontend_uses_recompile_and_persistent_errors():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    html = (root / "frontend/web/index.html").read_text(encoding="utf-8")
    script = (root / "frontend/web/assets/app.js").read_text(encoding="utf-8")

    assert 'id="rubric-edit-error"' in html
    assert 'id="rubric-edit-status"' in html
    assert "draft-deduction-rules" in script
    assert "supersedes_compilation_id" in script
    assert "renderRubricOperationError" in script
    assert "保存并重新校验" in html
    edit_handler = script[
        script.index(
            'document.querySelector("#rubric-edit-form").addEventListener("submit"'
        ) :
    ]
    edit_handler = edit_handler[: edit_handler.index('document.querySelector("#batch-form")')]
    assert "method: \"PATCH\"" not in edit_handler
    assert "/recompile" in edit_handler


@pytest.mark.parametrize("body,reason", [
    ({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}, "output_truncated"),
    ({"choices": [{"message": {"content": "secret invalid output"}}]}, "invalid_json"),
    ({"choices": [{"message": {"content": ""}}]}, "empty_content"),
    ({"error": {"message": "secret provider body"}}, "error_envelope"),
    ({"choices": []}, "missing_choices"),
])
def test_chat_output_failure_is_safe_and_not_service_unavailable(body, reason, caplog):
    from backend.app.services.llm.openai_compatible_adapter import _parse_chat_json_output

    class Scorer(_DraftScorer):
        def complete_json(self, instructions, payload):
            return _parse_chat_json_output(body)

    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=Scorer(None), business_profile_key="thesis")
    assert caught.value.code == ("AI_DRAFT_OUTPUT_TRUNCATED" if reason == "output_truncated" else "AI_DRAFT_PROVIDER_ERROR" if reason == "error_envelope" else "AI_DRAFT_OUTPUT_INVALID")
    assert reason in caplog.text
    assert "secret" not in caplog.text


def test_wrapped_provider_rejection_remains_rejection(caplog):
    from backend.app.services.llm.errors import raise_provider_call_error

    class Scorer(_RejectedDraftScorer):
        def complete_json(self, instructions, payload):
            try:
                super().complete_json(instructions, payload)
            except httpx.HTTPStatusError as exc:
                raise_provider_call_error("openai_compatible", exc)

    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=Scorer(), business_profile_key="thesis")
    assert caught.value.code == "AI_DRAFT_PROVIDER_REJECTED"
    assert "status=400" in caplog.text


def test_missing_mutex_is_repaired_once_without_relaxing_validation():
    class Scorer(_DraftScorer):
        def complete_json(self, instructions, payload):
            self.payloads.append((instructions, payload))
            return {"rule_groups": [{"group_code": "G1", "issue": "需求缺失",
                "mutex_group": "G1-severity" if len(self.payloads) == 2 else "",
                "cap_points": 2, "rules": [{"severity": "minor", "trigger": "需求缺失",
                "points": 2, "reason": "需求不完整", "repeat_policy": "once", "source": "ai_inferred",
                "source_refs": ["/criterion/description"]}]}]}
    scorer = Scorer(None)
    result = draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=scorer, business_profile_key="thesis")
    assert len(scorer.payloads) == 2
    assert "MUTEX_GROUP_MISSING" in scorer.payloads[1][0]
    assert result["requires_confirmation"]
    assert not result["rule_groups"][0]["rules"][0]["confirmed"]


@pytest.mark.parametrize("host,openrouter", [("openrouter.ai", True), ("generativelanguage.googleapis.com", False)])
def test_drafting_uses_strict_bounded_schema_for_compatible_providers(host, openrouter):
    import json
    from backend.app.services.llm.openai_compatible_adapter import OpenAICompatibleChatScorer
    calls = []
    class Scorer(OpenAICompatibleChatScorer):
        def _post_with_retry(self, body, **kwargs):
            calls.append(body)
            value = {"rule_groups": [{"group_code": "G1", "issue": "缺失",
                "mutex_group": "G1", "cap_points": 2, "rules": [{"severity": "minor",
                "trigger": "需求缺失", "points": 2, "reason": "不完整",
                "repeat_policy": "once", "source": "ai_inferred",
                "source_refs": ["/criterion/description"]}]}]}
            return httpx.Response(200, request=httpx.Request("POST", self.base_url),
                json={"choices": [{"message": {"content": json.dumps(value)}}]})
    scorer = Scorer(api_key="test-key", base_url="https://" + host + "/api/v1", response_format_json=False)
    draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=scorer, business_profile_key="thesis")
    assert len(calls) == 1
    schema = calls[0]["response_format"]["json_schema"]
    assert schema["strict"]
    groups = schema["schema"]["properties"]["rule_groups"]
    assert groups["maxItems"] == 2
    assert groups["items"]["properties"]["rules"]["maxItems"] == 3
    assert groups["items"]["properties"]["issue"]["maxLength"] == 320
    assert "mutex_group" in groups["items"]["required"]
    assert calls[0]["max_tokens"] == AI_RULE_DRAFT_MAX_OUTPUT_TOKENS
    if openrouter:
        assert calls[0]["reasoning"] == {"enabled": False}
    else:
        assert "reasoning" not in calls[0]


def test_complex_criterion_is_generated_in_bounded_batches_and_merged():
    class Scorer(_DraftScorer):
        def complete_json(self, instructions, payload):
            self.payloads.append((instructions, payload))
            ref = payload["input_analysis"]["source_refs"][0]
            index = payload["batch"]["index"]
            return {"rule_groups": [{
                "group_code": "G1", "issue": f"问题{index}", "mutex_group": "M1", "cap_points": 2,
                "rules": [{"severity": "minor", "trigger": f"触发条件{index}", "points": 2,
                           "reason": f"原因{index}", "repeat_policy": "once",
                           "source": "ai_interpreted_user_text", "source_refs": [ref]}],
            }]}

    rules = [f"要求{chr(64 + index)}表达不清" for index in range(1, 5)]
    criterion = _criterion(deduction_rules=rules)
    analysis = analyze_rule_input(rules, criterion_code="T02")
    scorer = Scorer(None)

    result = draft_deduction_rules(
        criterion=criterion, input_analysis=analysis, scorer=scorer, business_profile_key="thesis"
    )

    assert len(scorer.payloads) == 4
    assert result["generation_metadata"]["batch_count"] == 4
    assert result["generation_metadata"]["default_max_output_tokens"] == AI_RULE_DRAFT_MAX_OUTPUT_TOKENS
    assert [group["group_code"] for group in result["rule_groups"]] == [
        "T02-B01-G01", "T02-B02-G01", "T02-B03-G01", "T02-B04-G01",
    ]
    assert all(len(payload["batch"]["focus_units"]) == 1 for _, payload in scorer.payloads)


def test_business_validation_rejects_more_than_three_severity_rules():
    draft = {
        "schema_version": "ai-deduction-draft@1", "criterion_code": "T02",
        "input_assessment": {}, "requires_confirmation": True,
        "rule_groups": [{
            "group_code": "G1", "issue": "问题", "mutex_group": "M1", "cap_points": 4,
            "rules": [
                {"severity": "minor", "trigger": f"条件{index}", "points": index + 1,
                 "reason": "原因", "repeat_policy": "once", "source": "ai_inferred",
                 "source_refs": ["/criterion/description"]}
                for index in range(4)
            ],
        }],
    }

    with pytest.raises(AIRuleDraftValidationError) as caught:
        validate_ai_rule_draft(draft, criterion=_criterion())
    assert caught.value.code == "AI_DRAFT_OUTPUT_TOO_LARGE"


def test_openai_responses_drafting_uses_same_schema_and_dedicated_budget():
    import json
    from backend.app.services.llm.openai_adapter import OpenAIResponsesScorer

    calls = []
    value = {"rule_groups": [{
        "group_code": "G1", "issue": "需求缺失", "mutex_group": "M1", "cap_points": 2,
        "rules": [{"severity": "minor", "trigger": "需求描述不完整", "points": 2,
                   "reason": "无法验证需求", "repeat_policy": "once", "source": "ai_inferred",
                   "source_refs": ["/criterion/description"]}],
    }]}

    options = []

    class Scorer(OpenAIResponsesScorer):
        def _post_with_retry(self, body, **kwargs):
            calls.append(body)
            options.append(kwargs)
            return httpx.Response(200, request=httpx.Request("POST", self.base_url),
                                  json={"output_text": json.dumps(value, ensure_ascii=False)})

    scorer = Scorer(api_key="test", base_url="https://api.openai.com/v1")
    draft_deduction_rules(
        criterion=_criterion(), input_analysis={}, scorer=scorer, business_profile_key="thesis"
    )

    assert calls[0]["max_output_tokens"] == AI_RULE_DRAFT_MAX_OUTPUT_TOKENS
    assert calls[0]["text"]["format"]["type"] == "json_schema"
    assert calls[0]["text"]["format"]["strict"] is True
    # 超时不重试（attempts_limit=1），429 另有两次按 Retry-After 的等待。
    assert options[0] == {
        "attempts_limit": 1,
        "timeout_seconds": AI_RULE_DRAFT_TIMEOUT_SECONDS,
        "rate_limit_retries": 2,
    }


@pytest.mark.parametrize("groups", [None, 4, {}, [4], [{"group_code": "G", "mutex_group": "M", "cap_points": 2, "rules": 4}]])
def test_malformed_model_arrays_fail_closed_with_bounded_repair(groups):
    scorer = _DraftScorer({"rule_groups": groups})
    with pytest.raises(AIRuleDraftValidationError):
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=scorer, business_profile_key="thesis")
    assert len(scorer.payloads) == 2


@pytest.mark.parametrize("configured,expected", [(None, 8192), (512, 512), (16384, 16384)])
def test_draft_budget_is_separate_and_respects_explicit_connection_limit(configured, expected, monkeypatch):
    from backend.app.core.config import settings
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_MAX_TOKENS", 1200)
    from backend.app.services.llm.openai_compatible_adapter import OpenAICompatibleChatScorer
    calls = []
    class Scorer(OpenAICompatibleChatScorer):
        def _post_with_retry(self, body, **kwargs):
            calls.append(body)
            return httpx.Response(200, request=httpx.Request("POST", self.base_url),
                json={"choices": [{"message": {"content": "{}"}}]})
    scorer = Scorer(api_key="test", max_tokens=configured)
    original = scorer.max_tokens
    scorer.complete_json("draft", {}, response_schema={}, default_max_tokens=8192)
    assert calls[-1]["max_tokens"] == expected
    scorer.complete_json("other task", {})
    assert calls[-1]["max_tokens"] == original
    assert scorer.max_tokens == original


def test_schema_drafting_does_not_multiply_transport_retries(monkeypatch):
    from backend.app.core.config import settings
    from backend.app.services.llm.errors import ProviderCallError
    from backend.app.services.llm.openai_compatible_adapter import OpenAICompatibleChatScorer
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_MAX_RETRIES", 3)
    calls = []
    class Client:
        def post(self, url, **kwargs):
            calls.append(kwargs["json"])
            raise httpx.ReadTimeout("synthetic timeout")
    scorer = OpenAICompatibleChatScorer(api_key="test", client=Client(), thinking_type="enabled")
    with pytest.raises(ProviderCallError):
        scorer.complete_json("draft", {}, response_schema={})
    assert len(calls) == 1
    assert calls[0]["thinking"] == {"type": "enabled"}
    assert "reasoning" not in calls[0]


@pytest.mark.parametrize("status,code", [(400, "invalid_request"), (429, "rate_limited"), (503, "provider_unavailable")])
def test_numeric_openrouter_error_code_is_projected_without_type_error(status, code):
    from backend.app.services.llm.errors import project_provider_error
    response = httpx.Response(status, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        json={"error": {"code": status, "message": "Provider returned error"}})
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()
    error = project_provider_error(caught.value)
    assert error.code == code
    assert error.provider_error_code == str(status)


def test_nested_provider_error_fields_are_not_reflected():
    from backend.app.services.llm.errors import project_provider_error
    response = httpx.Response(400, request=httpx.Request("POST", "https://example.com"),
        json={"error": {"code": {"private": "secret"}, "type": [], "message": {"private": "secret"}}})
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()
    error = project_provider_error(caught.value)
    assert error.provider_error_code is None
    assert "secret" not in str(error.to_mapping())


def _timeout_client(calls):
    class Client:
        def post(self, url, **kwargs):
            calls.append(kwargs)
            raise httpx.ReadTimeout("synthetic timeout")
    return Client()


def test_responses_drafting_timeout_is_one_attempt_with_draft_wait_and_one_breaker_failure(monkeypatch):
    """百炼 Responses 连接：一次超时只等一轮、只计一次熔断，提示实际等待秒数。"""
    from backend.app.core.config import settings
    from backend.app.services.llm import rate_limit
    from backend.app.services.llm.openai_adapter import OpenAIResponsesScorer
    monkeypatch.setattr(settings, "OPENAI_MAX_RETRIES", 2)
    rate_limit.reset_provider_runtime_for_tests()
    calls = []
    scorer = OpenAIResponsesScorer(api_key="test", base_url="https://example.invalid/v1", client=_timeout_client(calls))

    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=scorer, business_profile_key="thesis")

    assert len(calls) == 1
    assert calls[0]["timeout"] == AI_RULE_DRAFT_TIMEOUT_SECONDS
    assert caught.value.code == "AI_DRAFT_PROVIDER_ERROR"
    assert f"{AI_RULE_DRAFT_TIMEOUT_SECONDS} 秒" in caught.value.message
    breaker = rate_limit._runtime(
        (
            scorer.provider,
            rate_limit.provider_circuit_key(
                None, base_url=scorer.base_url, model_name=scorer.model_name
            ),
        )
    ).breaker.snapshot()
    assert breaker["transient_failures"] == 1
    assert breaker["state"] == "closed"
    rate_limit.reset_provider_runtime_for_tests()


def test_explicit_connection_timeout_wins_over_draft_default():
    from backend.app.services.llm import rate_limit
    from backend.app.services.llm.openai_adapter import OpenAIResponsesScorer
    rate_limit.reset_provider_runtime_for_tests()
    calls = []
    scorer = OpenAIResponsesScorer(api_key="test", base_url="https://example.invalid/v1",
                                   client=_timeout_client(calls), timeout_seconds=45)

    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=scorer, business_profile_key="thesis")

    assert len(calls) == 1
    assert "timeout" not in calls[0]  # 使用连接自己的客户端超时
    assert "45 秒" in caught.value.message
    rate_limit.reset_provider_runtime_for_tests()


def test_open_circuit_is_reported_as_a_pause_not_a_generic_failure():
    from backend.app.services.llm.rate_limit import CircuitOpenError

    class Scorer(_DraftScorer):
        def complete_json(self, instructions, payload):
            raise CircuitOpenError()

    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=Scorer(None), business_profile_key="thesis")

    assert caught.value.code == "AI_DRAFT_PROVIDER_ERROR"
    assert "暂停调用" in caught.value.message
    assert "账户与连接" in caught.value.user_action


def _batch_group(payload):
    ref = payload["input_analysis"]["source_refs"][0]
    index = payload["batch"]["index"]
    return {"rule_groups": [{
        "group_code": "G1", "issue": f"问题{index}", "mutex_group": "M1", "cap_points": 2,
        "rules": [{"severity": "minor", "trigger": f"触发条件{index}", "points": 2,
                   "reason": f"原因{index}", "repeat_policy": "once",
                   "source": "ai_interpreted_user_text", "source_refs": [ref]}],
    }]}


def test_batches_run_with_bounded_concurrency_and_keep_batch_order():
    import threading
    import time

    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    class Scorer(_DraftScorer):
        def complete_json(self, instructions, payload):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                # 后发的批次先返回，验证合并仍按批次顺序。
                time.sleep(0.02 * (7 - payload["batch"]["index"]))
                return _batch_group(payload)
            finally:
                with lock:
                    state["active"] -= 1

    rules = [f"要求{chr(64 + index)}表达不清" for index in range(1, 7)]
    analysis = analyze_rule_input(rules, criterion_code="T02")
    scorer = Scorer(None)

    result = draft_deduction_rules(criterion=_criterion(deduction_rules=rules), input_analysis=analysis,
                                   scorer=scorer, business_profile_key="thesis")

    assert result["generation_metadata"]["batch_count"] == 6
    assert state["peak"] == AI_RULE_DRAFT_MAX_CONCURRENCY
    assert [group["issue"] for group in result["rule_groups"]] == [f"问题{index}" for index in range(1, 7)]


def test_failed_batch_stops_new_batches_and_reports_the_first_failure():
    import threading
    from backend.app.services.llm.rate_limit import CircuitOpenError

    release = threading.Event()
    started = []

    class Scorer(_DraftScorer):
        def complete_json(self, instructions, payload):
            index = payload["batch"]["index"]
            started.append(index)
            if index == 1:
                raise CircuitOpenError()
            release.wait(timeout=5)
            return _batch_group(payload)

    rules = [f"要求{chr(64 + index)}表达不清" for index in range(1, 7)]
    analysis = analyze_rule_input(rules, criterion_code="T02")
    timer = threading.Timer(0.2, release.set)
    timer.start()
    try:
        with pytest.raises(AIRuleDraftValidationError) as caught:
            draft_deduction_rules(criterion=_criterion(deduction_rules=rules), input_analysis=analysis,
                                  scorer=Scorer(None), business_profile_key="thesis")
    finally:
        timer.cancel()
        release.set()

    assert "暂停调用" in caught.value.message
    assert sorted(started) == [1, 2, 3]


@pytest.mark.parametrize("failure,expected", [
    (httpx.Response(429, request=httpx.Request("POST", "https://x.example/v1/responses")), "AI 服务限流，请求被拒绝（rate_limited，HTTP 429，等待"),
    (httpx.Response(504, request=httpx.Request("POST", "https://x.example/v1/responses")), "AI 服务端出错或网关超时（provider_unavailable，HTTP 504，等待"),
    (httpx.RemoteProtocolError("Server disconnected without sending a response."), "与 AI 服务的连接中断（network_error，等待"),
])
def test_provider_failures_name_the_safe_error_code_status_and_wait(failure, expected):
    from backend.app.services.llm.errors import raise_provider_call_error

    class Scorer(_DraftScorer):
        def complete_json(self, instructions, payload):
            if isinstance(failure, httpx.Response):
                try:
                    failure.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise_provider_call_error("openai_responses", exc)
            raise_provider_call_error("openai_responses", failure)

    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=Scorer(None), business_profile_key="thesis")

    assert caught.value.code == "AI_DRAFT_PROVIDER_ERROR"
    assert caught.value.message.startswith(expected)
    assert caught.value.message.endswith("秒后失败）。")
