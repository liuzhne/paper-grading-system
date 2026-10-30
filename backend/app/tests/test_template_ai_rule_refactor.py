import logging
import time

import httpx
import pytest

from backend.app.core.config import settings

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

    def __init__(self, status=400):
        self.status = status

    def complete_json(self, _instructions, _payload):
        request = httpx.Request(
            "POST", "https://api.groq.com/openai/v1/chat/completions"
        )
        response = httpx.Response(self.status, request=request)
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


def _draft_task(client, rubric_id):
    from backend.app.services.work_queue.runner import execute_next

    created = client.post(
        f"/api/rubrics/{rubric_id}/ai-tasks",
        json={"kind": "rule_draft", "params": {"criterion": _criterion()}},
    )
    if created.status_code >= 400:
        return created, None
    while execute_next(client.session_factory) is not None:
        pass
    return created, client.get(f"/api/ai-tasks/{created.json()['id']}").json()


def test_draft_task_rejects_mock_llm_with_actionable_problem(client):
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

    response, _task = _draft_task(client, created.json()["id"])

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "AI_DRAFT_CONNECTION_MISSING"
    assert detail["user_action"]
    assert detail["retryable"] is False


def test_draft_task_returns_structured_suggestions_without_mutating_rubric(
    client,
    monkeypatch,
):
    from backend.app.services.ai_tasks import service as ai_task_service

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
    monkeypatch.setattr(ai_task_service, "get_llm_scorer", lambda *a, **k: scorer)

    response, task = _draft_task(client, rubric_id)

    assert response.status_code == 202, response.text
    assert task["status"] == "succeeded"
    draft = task["result"]["draft"]
    assert draft["requires_confirmation"] is True
    assert draft["generation_metadata"]["fingerprint"]
    assert all(
        item["confirmed"] is False
        for item in draft["rule_groups"][0]["rules"]
    )
    after = client.get(f"/api/rubrics/{rubric_id}/execution-draft").json()
    assert after["active_compilation"]["id"] == before["active_compilation"]["id"]
    assert len(after["compilations"]) == 1


@pytest.mark.parametrize(
    ("status", "connection_hint"),
    [(400, False), (422, False), (401, True), (403, True), (404, True)],
)
def test_ai_drafter_reports_non_retryable_provider_rejection_without_body(status, connection_hint):
    with pytest.raises(AIRuleDraftValidationError) as captured:
        draft_deduction_rules(
            criterion=_criterion(),
            input_analysis=analyze_rule_input([], criterion_code="T02"),
            scorer=_RejectedDraftScorer(status),
            business_profile_key="thesis",
        )

    assert captured.value.code == "AI_DRAFT_PROVIDER_REJECTED"
    assert "拒绝" in captured.value.message
    assert f"HTTP {status}" in captured.value.message
    # 请求被拒时连接是好的，测连接也会通过；只有连接被拒才让用户去测连接。
    assert ("测试当前 AI 连接" in captured.value.user_action) is connection_hint
    if not connection_hint:
        assert "被拒的是这一批请求" in captured.value.user_action
        assert "内容限制" not in captured.value.user_action  # 400 的原因不止内容，不替用户猜
        # 重试会原样重发冻结的输入：引导重新起草或手写规则，而不是改原文后重试。
        assert "重新起草" in captured.value.user_action
        assert "原样重发" in captured.value.user_action
    assert "groq" not in str(captured.value).lower()


def test_draft_task_fails_a_provider_rejection_without_retrying_or_exposing_the_body(
    client,
    monkeypatch,
):
    from backend.app.services.ai_tasks import service as ai_task_service

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
        ai_task_service, "get_llm_scorer", lambda *a, **k: _RejectedDraftScorer()
    )

    _response, task = _draft_task(client, created.json()["id"])

    assert task["status"] == "failed"
    assert task["error_code"] == "AI_DRAFT_PROVIDER_REJECTED"
    assert "模型拒绝处理这一批起草请求（invalid_request，HTTP 400）" in task["error_message"]
    assert "测试当前 AI 连接" not in task["error_message"]
    assert "groq" not in task["error_message"].lower()
    # 请求被拒（400）立即判失败，不再执行第二次；剩下的批次不再发出。
    first, second = sorted(task["items"], key=lambda item: item["ordinal"])
    assert (first["status"], first["attempt_count"]) == ("failed", 1)
    assert (second["status"], second["attempt_count"]) == ("canceled", 0)


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
    assert "invalid_request，HTTP 400" in caught.value.message
    assert "被拒的是这一批请求" in caught.value.user_action
    assert "status=400" in caplog.text

    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=Scorer(401), business_profile_key="thesis")
    assert caught.value.code == "AI_DRAFT_PROVIDER_REJECTED"
    assert "测试当前 AI 连接" in caught.value.user_action


def test_wrapped_context_overflow_points_at_the_batch_length():
    from backend.app.services.llm.errors import raise_provider_call_error

    class Scorer:
        provider = "openai_compatible"
        model_name = "m"
        model_version = "v"

        def complete_json(self, _instructions, _payload):
            request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
            response = httpx.Response(
                400, json={"error": {"message": "maximum context length is 8192 tokens"}}, request=request,
            )
            raise_provider_call_error(
                "openai_compatible", httpx.HTTPStatusError("x", request=request, response=response),
            )

    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=Scorer(), business_profile_key="thesis")
    assert caught.value.code == "AI_DRAFT_PROVIDER_REJECTED"
    assert "context_length_exceeded，HTTP 400" in caught.value.message
    assert "精简该评分项的规则来源" in caught.value.user_action
    assert "重新起草" in caught.value.user_action


@pytest.mark.parametrize(
    ("status", "code", "text"),
    [(402, "AI_DRAFT_PROVIDER_ERROR", "额度已用完"), (413, "AI_DRAFT_PROVIDER_REJECTED", "超出了模型一次能处理的长度")],
)
def test_direct_http_errors_share_the_provider_classification(status, code, text):
    # 没经过传输层、直接抛出的 HTTP 错误，也按同一套分类给提示。
    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(
            criterion=_criterion(), input_analysis={}, scorer=_RejectedDraftScorer(status),
            business_profile_key="thesis",
        )
    assert caught.value.code == code
    assert text in caught.value.message


def test_wrapped_payment_required_uses_the_quota_hint():
    from backend.app.services.llm.errors import raise_provider_call_error

    class Scorer(_RejectedDraftScorer):
        def complete_json(self, instructions, payload):
            try:
                super().complete_json(instructions, payload)
            except httpx.HTTPStatusError as exc:
                raise_provider_call_error("openai_compatible", exc)

    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=Scorer(402), business_profile_key="thesis")
    # 402 是额度问题，不是“请求被拒”：提示充值或换连接，而不是去测连接。
    assert caught.value.code == "AI_DRAFT_PROVIDER_ERROR"
    assert "额度已用完" in caught.value.message


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
    # 截止时间一路传到适配器，约束每次调用与重试（不越过请求的时间预算）。
    deadline = options[0].pop("deadline")
    assert 0 < deadline - time.monotonic() <= settings.RUBRIC_AI_DRAFT_TIME_BUDGET_SECONDS
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
    # 生效的是连接自己的 45 秒，而不是起草默认的 120 秒；有截止时间时取两者与剩余预算的较小值。
    assert calls[0]["timeout"] == 45
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


_TOO_MANY_STATES = [{"error": {
    "code": 400, "status": "INVALID_ARGUMENT",
    "message": "The specified schema produces a constraint that has too many states for serving.",
}}]


def _schema_fallback_scorer(responses, calls):
    """Chat 适配器：按顺序返回 ``responses``；元素是 (状态码, 正文)。"""

    import json
    from backend.app.services.llm.errors import raise_provider_call_error
    from backend.app.services.llm.openai_compatible_adapter import OpenAICompatibleChatScorer

    class Scorer(OpenAICompatibleChatScorer):
        def _post_with_retry(self, body, **kwargs):
            calls.append(body)
            status, value = responses.pop(0)
            request = httpx.Request("POST", self.base_url)
            if status != 200:
                response = httpx.Response(status, json=value, request=request)
                raise_provider_call_error(
                    "openai_compatible", httpx.HTTPStatusError("x", request=request, response=response),
                )
            return httpx.Response(200, request=request,
                                  json={"choices": [{"message": {"content": json.dumps(value)}}]})

    return Scorer(api_key="test-key", base_url="https://aiplatform.googleapis.com/v1/openapi",
                  model_name="google/gemini-2.5-flash", response_format_json=False)


_VALID_GROUPS = {"rule_groups": [{"group_code": "G1", "issue": "缺失", "mutex_group": "G1", "cap_points": 2,
                  "rules": [{"severity": "minor", "trigger": "需求缺失", "points": 2, "reason": "不完整",
                             "repeat_policy": "once", "source": "ai_inferred",
                             "source_refs": ["/criterion/description"]}]}]}


def test_schema_too_complex_is_resent_once_with_the_lean_schema(caplog):
    import json

    calls = []
    scorer = _schema_fallback_scorer([(400, _TOO_MANY_STATES), (200, _VALID_GROUPS)], calls)
    with caplog.at_level(logging.WARNING):
        draft = draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=scorer,
                                      business_profile_key="thesis")

    assert len(calls) == 2
    strict = calls[0]["response_format"]["json_schema"]["schema"]
    lean = calls[1]["response_format"]["json_schema"]
    assert lean["strict"]
    lean_text = json.dumps(lean["schema"])
    # 精简 Schema 去掉长度、数值范围、来源枚举与去重；结构、必填字段和组数/条数上限保留。
    for keyword in ("maxLength", "minLength", "minimum", "maximum", "uniqueItems"):
        assert keyword in json.dumps(strict) and keyword not in lean_text
    groups = lean["schema"]["properties"]["rule_groups"]
    assert (groups["maxItems"], groups["items"]["properties"]["rules"]["maxItems"]) == (2, 3)
    assert "mutex_group" in groups["items"]["required"]
    rule = groups["items"]["properties"]["rules"]["items"]
    assert rule["properties"]["source_refs"]["items"] == {"type": "string"}
    assert rule["properties"]["severity"]["enum"] == ["minor", "moderate", "severe"]
    # 其余请求内容不变：同一批、同一预算。
    assert calls[0]["messages"] == calls[1]["messages"]
    assert calls[0]["max_tokens"] == calls[1]["max_tokens"]
    # 合并后的草稿记下哪些批次用了精简 Schema，便于事后追查。
    assert draft["generation_metadata"]["lean_schema_batches"] == [1]
    assert "output_schema" not in draft["generation_metadata"]
    assert "rubric_ai_draft_schema_fallback batch=1/1 reason=schema_too_complex" in caplog.text


def test_lean_schema_output_is_still_validated_by_code():
    # 精简 Schema 不限分值：超过满分的输出仍由代码拦下，修正一次后照样失败，不会被采纳。
    group = _VALID_GROUPS["rule_groups"][0]
    too_many = {"rule_groups": [{**group, "cap_points": 99, "rules": [{**group["rules"][0], "points": 99}]}]}
    calls = []
    scorer = _schema_fallback_scorer(
        [(400, _TOO_MANY_STATES), (200, too_many), (400, _TOO_MANY_STATES), (200, too_many)], calls,
    )
    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=scorer,
                              business_profile_key="thesis")
    assert caught.value.code == "DEDUCTION_POINTS_EXCEED_MAX"
    assert len(calls) == 4  # 每次执行：完整 Schema 被拒 → 精简 Schema；修正那次同样


@pytest.mark.parametrize(
    ("change", "code"),
    [
        # 精简 Schema 没有来源枚举：编造的位置由代码拦下。
        ({"source_refs": ["/input_analysis/not-real"]}, "AI_DRAFT_SOURCE_INVALID"),
        # 精简 Schema 没有 minimum：小于 0.01 的扣分由代码拦下。
        ({"points": 0.001}, "DEDUCTION_POINTS_INVALID"),
    ],
)
def test_lean_schema_keeps_the_strict_bounds_in_code(change, code):
    group = _VALID_GROUPS["rule_groups"][0]
    bad = {"rule_groups": [{**group, "cap_points": 2, "rules": [{**group["rules"][0], **change}]}]}
    calls = []
    scorer = _schema_fallback_scorer(
        [(400, _TOO_MANY_STATES), (200, bad), (400, _TOO_MANY_STATES), (200, bad)], calls,
    )
    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=scorer,
                              business_profile_key="thesis")
    assert caught.value.code == code
    assert len(calls) == 4  # 修正一次后仍不合格：失败，不采纳


def test_lean_schema_rejected_too_reports_the_format_constraint_not_content(caplog):
    calls = []
    scorer = _schema_fallback_scorer([(400, _TOO_MANY_STATES), (400, _TOO_MANY_STATES)], calls)
    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=scorer,
                              business_profile_key="thesis")
    assert len(calls) == 2  # 只降级一次，不继续重发
    assert caught.value.code == "AI_DRAFT_PROVIDER_REJECTED"
    assert "输出格式约束" in caught.value.message
    assert "与原文内容和连接无关" in caught.value.user_action
    assert "测试当前 AI 连接" not in caught.value.user_action


_NO_REASON = [{"error": {
    "code": 400, "status": "INVALID_ARGUMENT", "message": "Request contains an invalid argument.",
}}]


def _source_ref_enum(body):
    schema = body["response_format"]["json_schema"]["schema"]
    rule = schema["properties"]["rule_groups"]["items"]["properties"]["rules"]["items"]
    return rule["properties"]["source_refs"]["items"].get("enum") or []


def _assigned_analysis():
    """生产上的形状：人工归入原文后第 1 批只有评分说明，从第 2 批起才带原文单元的编号。"""

    from backend.app.services.rubric_import.ai_rule_drafter import _draft_batches

    analysis = {
        "input_state": "absent", "parsed_rules": [], "needs_ai_draft": True, "needs_severity_expansion": False,
        "source_refs": ["xlsx:评分表!R3C2"],
        "unresolved_segments": [
            {"text": "需求分析应完整、明确并可验证。", "source_refs": ["/criterion/description"]},
            {"text": "缺少验收标准扣 2 分", "source_refs": ["xlsx:评分表!R3C2"], "reason": "assigned_source"},
        ],
    }
    return _draft_batches(_criterion(), analysis)


def test_rejection_without_reason_is_resent_with_the_lean_schema(caplog):
    from backend.app.services.rubric_import.ai_rule_drafter import _draft_deduction_rules_once

    first, second = _assigned_analysis()
    group = _VALID_GROUPS["rule_groups"][0]
    output = {"rule_groups": [{**group, "rules": [{**group["rules"][0], "source_refs": ["xlsx:评分表!R3C2"]}]}]}
    calls = []
    scorer = _schema_fallback_scorer([(200, _VALID_GROUPS), (400, _NO_REASON), (200, output)], calls)
    with caplog.at_level(logging.WARNING):
        drafts = [
            _draft_deduction_rules_once(criterion=_criterion(), input_analysis=batch, scorer=scorer,
                                        business_profile_key="thesis")
            for batch in (first, second)
        ]

    # 第 1 批的枚举里没有原文单元，完整 Schema 通过，一个字节都不变。
    assert not any(ref.startswith("xlsx:") for ref in _source_ref_enum(calls[0]))
    assert drafts[0]["generation_metadata"]["output_schema"] == "strict"
    # 第 2 批多了一个原文单元被拒，改发没有来源枚举的精简 Schema，其余请求内容不变。
    assert "xlsx:评分表!R3C2" in _source_ref_enum(calls[1]) and _source_ref_enum(calls[2]) == []
    assert calls[1]["messages"] == calls[2]["messages"]
    assert drafts[1]["generation_metadata"]["output_schema"] == "lean"
    assert drafts[1]["rule_groups"][0]["rules"][0]["source_refs"] == ["xlsx:评分表!R3C2"]
    # 日志记枚举的形状，方便判断拒绝与枚举是否有关；不记工作表名。
    assert "reason=rejected_without_reason enum_values=" in caplog.text
    assert "ledger_kinds=xlsx" in caplog.text
    assert "评分表" not in caplog.text


def test_lean_schema_output_after_rejection_without_reason_is_still_validated():
    # 同样靠代码校验兜底：精简 Schema 不限来源，编造的编号修正一次后仍不被采纳。
    group = _VALID_GROUPS["rule_groups"][0]
    bad = {"rule_groups": [{**group, "rules": [{**group["rules"][0], "source_refs": ["xlsx:评分表!R9C9"]}]}]}
    calls = []
    scorer = _schema_fallback_scorer(
        [(400, _NO_REASON), (200, bad), (400, _NO_REASON), (200, bad)], calls,
    )
    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=scorer,
                              business_profile_key="thesis")
    assert caught.value.code == "AI_DRAFT_SOURCE_INVALID"
    assert len(calls) == 4
    # 修正那次执行同样先发完整 Schema，被拒后再发精简 Schema。
    assert [bool(_source_ref_enum(call)) for call in calls] == [True, False, True, False]


def test_lean_schema_rejected_without_reason_does_not_blame_the_schema():
    # 精简版也被拒、厂商还是不说原因：不能断言“与原文内容无关”，走请求被拒的通用提示。
    calls = []
    scorer = _schema_fallback_scorer([(400, _NO_REASON), (400, _NO_REASON)], calls)
    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=scorer,
                              business_profile_key="thesis")
    assert len(calls) == 2  # 只降级一次
    assert caught.value.code == "AI_DRAFT_PROVIDER_REJECTED"
    assert "输出格式约束" not in caught.value.message
    assert "与原文内容和连接无关" not in caught.value.user_action
    assert "参数、输出格式或内容" in caught.value.user_action


def test_other_invalid_requests_are_not_resent():
    calls = []
    other = [{"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "Invalid value at 'temperature'"}}]
    scorer = _schema_fallback_scorer([(400, other)], calls)
    with pytest.raises(AIRuleDraftValidationError) as caught:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=scorer,
                              business_profile_key="thesis")
    assert len(calls) == 1
    assert caught.value.code == "AI_DRAFT_PROVIDER_REJECTED"
    assert "输出格式约束" not in caught.value.message


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


@pytest.mark.parametrize("protocol", ["responses", "claude"])
def test_every_protocol_reports_drafting_truncation_and_refusal_precisely(protocol):
    """三个协议的输出错误共用一个基类；以前只接住 Chat 的，Responses 的截断落到通用失败。"""
    from backend.app.services.llm.anthropic_messages_adapter import AnthropicMessagesScorer
    from backend.app.services.llm.openai_adapter import OpenAIResponsesScorer

    def respond(kind):
        if protocol == "responses":
            return {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
        return {"type": "message", "content": [{"type": "text", "text": "{}"}],
                "stop_reason": "max_tokens" if kind == "truncated" else "refusal"}

    def scorer_for(kind):
        base = OpenAIResponsesScorer if protocol == "responses" else AnthropicMessagesScorer

        class Scorer(base):
            def _post_with_retry(self, body, **kwargs):
                return httpx.Response(200, request=httpx.Request("POST", self.base_url), json=respond(kind))

        if protocol == "responses":
            return Scorer(api_key="test-key", base_url="https://api.openai.com/v1", model_name="m")
        return Scorer(api_key="test-key", base_url="https://api.anthropic.com/v1", model_name="claude")

    with pytest.raises(AIRuleDraftValidationError) as truncated:
        draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=scorer_for("truncated"),
                              business_profile_key="thesis")
    assert truncated.value.code == "AI_DRAFT_OUTPUT_TRUNCATED"
    if protocol == "claude":
        with pytest.raises(AIRuleDraftValidationError) as refused:
            draft_deduction_rules(criterion=_criterion(), input_analysis={}, scorer=scorer_for("refused"),
                                  business_profile_key="thesis")
        assert "拒绝" in refused.value.message
