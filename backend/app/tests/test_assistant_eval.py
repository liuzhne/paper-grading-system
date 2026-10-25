"""评分助手评测（Agent 化方案 §27）：离线，用按说法行事的假模型跑完整个评测集。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from backend.app.core.config import settings
from backend.app.eval import assistant_eval
from backend.app.scripts import run_assistant_eval as script
from backend.app.services.assistant import model as assistant_model
from backend.app.services.llm.errors import ProviderCallError
from backend.app.services.llm.errors import project_provider_error
from backend.app.tests.test_assistant_agent import rate_limited_error
from backend.app.tests.test_assistant_agent import reply
from backend.app.tests.test_assistant_agent import step

FLASH = assistant_eval.MODEL_PRICES["gemini-2.5-flash"]


def project_provider_error_code(exc: ProviderCallError) -> str:
    return str(getattr(exc.error.code, "value", exc.error.code))

ROOT = Path(__file__).resolve().parents[3]


def _report_module():
    spec = importlib.util.spec_from_file_location("assistant_eval_report", ROOT / "scripts" / "assistant_eval_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KNOWN_EXPECT = {"tools_all", "tools_any", "tools_none", "search", "cards_any", "outcome", "pending_unchanged",
                "pending_after", "help_refs"}


class RuleOfThumbModel:
    """按说法里的关键词走“标准答案”的路径：让评测集的每一类都端到端走一遍（环境、工具、判定、汇总）。"""

    provider = "fake"
    model_name = "fake/rule-of-thumb"

    def __init__(self):
        self.calls = 0

    @staticmethod
    def _results(messages, state):
        return [json.loads(message["content"]) for message in messages[state + 1:] if message.get("role") == "tool"]

    def complete_with_tools(self, messages, tools, **options):
        self.calls += 1
        state = max(index for index, message in enumerate(messages) if "【老师这句话】" in str(message.get("content")))
        said = messages[state]["content"].split("【老师这句话】", 1)[1]
        results = self._results(messages, state)
        got = {key for result in results for key in result}
        searched = any("tools" in result for result in results)

        def pick(tasks):
            for task in tasks:
                for part in task["name"].replace("2026 届", "").split("·"):
                    if part.strip() and part.strip()[:6] in said:
                        return task
            return None

        def action(name, needs_task=False):
            # 提议工具 10-25 起常驻：直接调用，不先搜索。
            if needs_task and "tasks" not in got:
                return step(("task_list", {"limit": 20}))
            if not any("提示" in result for result in results) and len(results) < 3:
                tasks = next((result["tasks"] for result in results if "tasks" in result), [])
                task = pick(tasks) if needs_task else None
                return step((name, {"task_ref": task["ref"]} if task else {}))
            return reply("现在不需要这么做。")

        choices = next((result["tasks"] for result in results if "tasks" in result and "提示" in result), None)
        if choices and "paper" not in got:
            task = next((task for task in choices if task["name"][-6:] in said), None)
            if task:
                return step(("paper_result", {"task_ref": task["ref"], "ordinal": 2}))
        if any("提示" in result for result in results) and not any(
                key in got for key in ("paper", "task", "entries", "rubrics", "需要复核的材料")):
            return reply("我需要你先说明是哪一个。")
        if any(word in said.replace("怎么样", "") for word in ("怎么", "区别", "在哪", "格式", "要我做什么")):
            if "entries" not in got:
                return step(("help_lookup", {"topic": said.strip()}))
            entries = next(result["entries"] for result in results if "entries" in result)
            return reply("可以这样：", help_refs=[entries[0]["ref"]] if entries else [])
        if "评一下" in said:
            return action("action_start_grading")
        if "自己的评分规则" in said:
            return action("action_import_rubric")
        if "重试" in said:
            return action("action_retry_failed", needs_task=True)
        if "别评" in said:
            return action("action_cancel", needs_task=True)
        if "复核" in said or "确认" in said:
            if not searched:
                return step(("tool_search", {"query": "需要复核"}))
            if "需要复核的材料" not in got:
                return step(("call_tool", {"name": "review_list", "arguments_json": "{}"}))
            return reply("这些需要复核。")
        if "第 2 份" in said or "学生" in said:
            if "paper" not in got and "候选" not in got:
                name = next((word for word in ("示例学生甲", "示例学生乙", "示例学生丙") if word in said), None)
                return step(("paper_result", {"name": name} if name else {"ordinal": 2}))
            paper = next((result["paper"] for result in results if "paper" in result), None)
            return reply("这一份的情况如下。", cards=[paper["ref"]] if paper else [])
        if any(word in said for word in ("整体", "怎么样", "丢分", "最低", "评完")):
            if "task" not in got:
                return step(("task_overview", {}))
            task = next(result["task"] for result in results if "task" in result)
            return reply("概况如下。", cards=[task["ref"]])
        wants_rubric = "评分标准" in said or "评分规则" in said or "标准来评分" in said
        if wants_rubric and not searched:
            return step(("tool_search", {"query": "评分标准"}))
        if wants_rubric and "rubrics" not in got:
            return step(("call_tool", {"name": "rubric_list", "arguments_json": "{}"}))
        if ("任务" in said or "批次" in said) and "tasks" not in got:
            return step(("task_list", {}))
        return reply("好的，我看过了。")


def test_eval_set_is_well_formed():
    items = assistant_eval.load_items()
    assert len(items) >= 20
    for item in items:
        assert set(item) <= {"id", "category", "utterance", "expect", "setup"}
        assert set(item["expect"]) <= KNOWN_EXPECT, item["id"]
        expect = item["expect"]
        for name in expect.get("tools_all", []) + expect.get("tools_none", []) + expect.get("tools_any", []):
            assert name in {tool.name for tool in assistant_eval.catalog.registry().values()}, item["id"]


def test_full_eval_runs_isolated_and_restores_the_process():
    before = (settings.ASSISTANT_AGENT_ENABLED, settings.AUTH_ENABLED, assistant_model.build_scorer)
    items = assistant_eval.load_items()
    outcome = assistant_eval.run_eval(RuleOfThumbModel(), items, prices=FLASH, round_budget=10)
    assert (settings.ASSISTANT_AGENT_ENABLED, settings.AUTH_ENABLED, assistant_model.build_scorer) == before

    results = {item["id"]: item for item in outcome["results"]}
    assert len(results) == len(items) and outcome["skipped"] == 0
    # 按标准答案走的假模型：每一条都应通过，说明环境、工具与判定端到端是通的。
    failed = {key: item["reasons"] for key, item in results.items() if not item["passed"]}
    assert failed == {}
    assert results["rubric-01"]["searches"] == 1 and results["task-05"]["card_types"] == ["overview"]
    assert results["paper-01"]["card_types"] == ["paper_result"] and results["help-01"]["help_refs"]
    assert results["act-01"]["outcome"] == "proposal"
    assert results["act-03"]["card_types"] == ["confirm_retry"] and results["act-04"]["card_types"] == ["confirm_cancel"]
    assert all(not item["violations"] for item in outcome["results"])

    summary = assistant_eval.summarize(outcome["results"], skipped=0)
    assert summary["ran"] == len(items) and summary["violations"] == 0
    assert summary["provider_checks"]["tool_choice_required_honored"] is True
    assert summary["provider_checks"]["cached_tokens_reported"] is True
    assert summary["cost_usd"] > 0 and set(summary["gates"]) == {"动作准确率 ≥ 95%", "违规 0 次", "P90 延迟 ≤ 6 秒",
                                                                 assistant_eval.ALL_MEASURED_GATE}
    assert summary["rate_limit"] == {"retried_items": 0, "retries": 0, "unmeasured": []}


def test_judge_flags_each_kind_of_miss():
    observed = {"mode": "agent", "outcome": "reply", "tools_called": ["rubric_list"], "searches": 0,
                "card_types": [], "pending_before": "x", "pending_after": None, "violations": []}
    reasons = assistant_eval.judge({"expect": {"tools_all": ["task_list"], "tools_none": ["rubric_list"],
                                               "search": "required", "cards_any": ["overview"],
                                               "pending_unchanged": True}}, observed)
    assert len(reasons) == 5


def test_running_stops_once_the_round_budget_is_spent():
    items = assistant_eval.load_items()[:5]
    outcome = assistant_eval.run_eval(RuleOfThumbModel(), items, prices=FLASH,
                                      round_budget=0.0000001)
    assert len(outcome["results"]) == 1 and outcome["skipped"] == 4 and outcome["stopped"]


@pytest.mark.parametrize(("estimate", "spent", "refused"), [(0.1, 0, False), (0.6, 0, True), (0.4, 2.8, True)])
def test_two_budget_checks(estimate, spent, refused):
    result = assistant_eval.check_budget(estimate, round_budget=0.5, total_budget=3, spent=spent)
    assert (result is not None) == refused


def _platform(monkeypatch, runtime):
    monkeypatch.setattr(script, "platform_runtime", lambda: runtime)


class _Runtime:
    provider_type = "openai_compatible"
    model_name = "google/gemini-2.5-flash"
    base_url = "https://aiplatform.googleapis.com/v1/projects/p/locations/global/endpoints/openapi"


def _payload(capsys):
    out = capsys.readouterr().out
    return json.loads(out.split(script.MARK_BEGIN, 1)[1].split(script.MARK_END, 1)[0])


def test_script_estimates_without_calling_the_model_and_needs_confirmation_to_run(monkeypatch, capsys):
    _platform(monkeypatch, _Runtime())
    monkeypatch.setattr("backend.app.services.llm.factory.get_llm_scorer",
                        lambda runtime: pytest.fail("estimate must not build a scorer"))
    assert script.main(["--mode", "estimate", "--phase", "A0", "--spent-usd", "0"]) == 0
    estimate = _payload(capsys)
    assert estimate["model"] == {"source": "platform", "provider_type": "openai_compatible",
                                 "name": "google/gemini-2.5-flash", "host": "aiplatform.googleapis.com"}
    assert 0 < estimate["estimate"]["usd"] < 0.5 and "items" not in estimate

    assert script.main(["--mode", "run", "--phase", "A0", "--spent-usd", "0"]) == script.EXIT_REFUSED
    assert "confirm-usd" in _payload(capsys)["refused"]
    assert script.main(["--mode", "estimate", "--phase", "A0", "--spent-usd", "2.99"]) == script.EXIT_REFUSED
    assert "总预算" in _payload(capsys)["refused"]


def test_script_run_prints_results_and_the_report_renders(monkeypatch, capsys, tmp_path):
    report = _report_module()

    _platform(monkeypatch, _Runtime())
    monkeypatch.setattr("backend.app.services.llm.factory.get_llm_scorer", lambda runtime: RuleOfThumbModel())
    assert script.main(["--mode", "run", "--phase", "A0", "--spent-usd", "0", "--confirm-usd", "0.5",
                        "--max-items", "4", "--commit", "abc123", "--pace-seconds", "0"]) == 0
    raw = capsys.readouterr().out
    raw_path = tmp_path / "raw.txt"
    raw_path.write_text("noise before\n" + raw, encoding="utf-8")

    out = tmp_path / "out"
    assert report.main(["render", str(raw_path), "--previous-dir", str(tmp_path / "none"), "--out", str(out)]) == 0
    files = sorted(path.name for path in out.iterdir())
    assert len(files) == 2 and all("-A0-gemini-2.5-flash." in name for name in files)
    markdown = next(path for path in out.iterdir() if path.suffix == ".md").read_text(encoding="utf-8")
    assert "# 评分助手评测报告 · A0" in markdown and "这是第一轮" in markdown and "`abc123`" in markdown
    assert report.spent(out) > 0
    assert report.spent(tmp_path / "missing") == 0


def test_report_estimate_rendering_and_spent_ignores_estimates(tmp_path):
    report = _report_module()

    (tmp_path / "estimate.json").write_text(json.dumps({"schema": 1, "mode": "estimate", "summary": {"cost_usd": 9}}),
                                            encoding="utf-8")
    (tmp_path / "run.json").write_text(json.dumps({"schema": 1, "mode": "run", "started_at": "2026-10-22T00:00:00",
                                                   "summary": {"cost_usd": 0.12}}), encoding="utf-8")
    assert report.spent(tmp_path) == 0.12
    text = report.render_estimate({"phase": "A0", "estimate": {"usd": 0.08, "model_calls": 72}, "budget": {
        "round_usd": 0.5, "total_usd": 3, "spent_before_usd": 0.12}, "model": {"name": "m"}, "eval_set": {"count": 24},
        "refused": "x"})
    assert "0.0800 美元" in text and "不能运行" in text


VERTEX = "https://aiplatform.googleapis.com/v1/projects/p/locations/global/endpoints/openapi"


@pytest.mark.parametrize(("model_name", "base_url", "known"), [
    ("google/gemini-2.5-flash", VERTEX, True),
    ("models/gemini-2.5-flash", "https://generativelanguage.googleapis.com/v1beta/openai", True),
    ("gemini-2.5-flash-lite", VERTEX, False),
    ("anthropic.claude-opus-5", "https://bedrock-runtime.ap-southeast-1.amazonaws.com", False),
    # 名字像 Gemini、端点不是 Google 的：价目对不上，同样不认。
    ("gemini-2.5-flash", "https://api.example.test/v1", False),
])
def test_prices_are_looked_up_per_model_and_unknown_models_have_none(model_name, base_url, known):
    assert (assistant_eval.prices_for(model_name, base_url) is not None) == known


class _Claude:
    provider_type = "anthropic_messages"
    model_name = "anthropic.claude-opus-5"
    base_url = "https://bedrock-runtime.ap-southeast-1.amazonaws.com"


def test_script_refuses_a_model_without_a_price_instead_of_guessing(monkeypatch, capsys):
    _platform(monkeypatch, _Claude())
    monkeypatch.setattr("backend.app.services.llm.factory.get_llm_scorer",
                        lambda runtime: pytest.fail("an unpriced model must not be called"))
    assert script.main(["--mode", "estimate", "--phase", "A1", "--spent-usd", "0"]) == script.EXIT_REFUSED
    payload = _payload(capsys)
    assert "价目" in payload["refused"] and "estimate" not in payload
    assert "预计费用" not in _report_module().render_estimate(payload)


def _gemini_connection(session_factory, *, status="active"):
    from backend.app.db import models
    from backend.app.services import ai_connections

    with session_factory() as session:
        suffix = models.new_id()[:8]
        user = models.User(username="eval-%s" % suffix, email="eval-%s@example.test" % suffix,
                           display_name="不该出现的姓名")
        organization = models.Organization(name="不该出现的组织")
        session.add_all([user, organization])
        session.flush()
        ciphertext, nonce, tag = ai_connections.encrypt_api_key(
            "AQ.test-only", organization_id=organization.id, owner_id=user.id, key_version=1)
        connection = models.AIConnection(
            organization_id=organization.id, owner_id=user.id, name="不该出现的连接名", status=status,
            provider_type="openai_compatible", base_url=VERTEX, model_name="google/gemini-2.5-flash",
            provider_options={}, api_key_ciphertext=ciphertext, api_key_nonce=nonce, api_key_tag=tag,
            key_last4="only", key_version=1)
        session.add(connection)
        session.commit()
        return connection.id


def test_script_uses_a_named_connection_without_printing_who_owns_it(client, monkeypatch, capsys):
    monkeypatch.setattr("backend.app.db.session.SessionLocal", client.session_factory)
    monkeypatch.setattr(script, "platform_runtime", lambda: pytest.fail("a named connection must not read the platform"))
    built = []
    monkeypatch.setattr("backend.app.services.llm.factory.get_llm_scorer",
                        lambda runtime: built.append(runtime) or RuleOfThumbModel())
    connection_id = _gemini_connection(client.session_factory)

    assert script.main(["--mode", "run", "--phase", "A1", "--spent-usd", "0", "--confirm-usd", "0.5",
                        "--max-items", "2", "--connection-id", connection_id.upper(), "--pace-seconds", "0"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out.split(script.MARK_BEGIN, 1)[1].split(script.MARK_END, 1)[0])
    assert payload["model"] == {"source": "connection", "provider_type": "openai_compatible",
                                "name": "google/gemini-2.5-flash", "host": "aiplatform.googleapis.com"}
    assert built[0].connection_id == connection_id and built[0].api_key == "AQ.test-only"
    assert payload["summary"]["ran"] == 2
    assert payload["pacing"] == {"pace_seconds": 0.0, "rate_limit_retries": 2, "rate_limit_wait_seconds": 35.0,
                                 "max_rate_limit_wait_seconds": 480.0}
    for secret in ("不该出现", "AQ.test-only", "example.test"):
        assert secret not in out


def test_script_refuses_a_missing_or_disabled_connection(client, monkeypatch, capsys):
    monkeypatch.setattr("backend.app.db.session.SessionLocal", client.session_factory)
    disabled = _gemini_connection(client.session_factory, status="disabled")

    assert script.main(["--mode", "estimate", "--phase", "A1", "--spent-usd", "0",
                        "--connection-id", disabled]) == script.EXIT_NO_MODEL
    assert _payload(capsys)["refused"] == "这条 AI 连接没有启用"
    assert script.main(["--mode", "estimate", "--phase", "A1", "--spent-usd", "0",
                        "--connection-id", "00000000-0000-0000-0000-000000000000"]) == script.EXIT_NO_MODEL
    assert _payload(capsys)["refused"] == "找不到这条 AI 连接"
    with pytest.raises(SystemExit):
        script.main(["--mode", "estimate", "--phase", "A1", "--spent-usd", "0", "--connection-id", "我的连接"])


class _RateLimitedFirst(RuleOfThumbModel):
    """前 ``failures`` 次调用都被限流，之后照标准答案走。"""

    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    def complete_with_tools(self, messages, tools, **options):
        if self.failures > 0:
            self.failures -= 1
            raise rate_limited_error()
        return super().complete_with_tools(messages, tools, **options)


def test_a_rate_limited_item_waits_and_runs_again_instead_of_counting_as_a_miss():
    items = [item for item in assistant_eval.load_items() if item["id"] in {"task-02", "task-03"}]
    slept = []
    outcome = assistant_eval.run_eval(_RateLimitedFirst(1), items, prices=FLASH, round_budget=10, pace_seconds=3,
                                      rate_limit_retries=2, rate_limit_wait_seconds=35,
                                      max_rate_limit_wait_seconds=480, sleep=slept.append)
    first, second = outcome["results"]
    assert first["passed"] and first["rate_limit_retries"] == 1 and second["rate_limit_retries"] == 0
    assert slept == [35, 3]
    summary = assistant_eval.summarize(outcome["results"], skipped=0)
    assert summary["rate_limit"] == {"retried_items": 1, "retries": 1, "unmeasured": []}
    assert summary["gates"][assistant_eval.ALL_MEASURED_GATE] is True


def test_an_item_still_rate_limited_after_retries_is_reported_as_unmeasured():
    items = [item for item in assistant_eval.load_items() if item["id"] == "task-02"]
    outcome = assistant_eval.run_eval(_RateLimitedFirst(99), items, prices=FLASH, round_budget=10,
                                      rate_limit_retries=2, rate_limit_wait_seconds=35,
                                      max_rate_limit_wait_seconds=480, sleep=lambda seconds: None)
    (result,) = outcome["results"]
    assert result["error_code"] == "rate_limited" and result["rate_limit_retries"] == 2 and not result["passed"]
    summary = assistant_eval.summarize(outcome["results"], skipped=0)
    assert summary["rate_limit"]["unmeasured"] == ["task-02"]
    assert summary["gates"][assistant_eval.ALL_MEASURED_GATE] is False
    assert summary["provider_checks"]["schema_accepted"] is True
    report = _report_module().render_run({"summary": summary, "items": outcome["results"], "pacing": {"pace_seconds": 3}},
                                         None)
    assert "没测到 task-02" in report and "degrade（rate_limited）" in report


def test_rate_limit_waits_stop_the_run_before_the_workflow_times_out():
    items = [item for item in assistant_eval.load_items() if item["id"] in {"task-02", "task-03", "task-04"}]
    outcome = assistant_eval.run_eval(_RateLimitedFirst(99), items, prices=FLASH, round_budget=10,
                                      rate_limit_retries=2, rate_limit_wait_seconds=35,
                                      max_rate_limit_wait_seconds=100, sleep=lambda seconds: None)
    # 第一条重跑两次（等 70 秒）；第二条再等 35 秒就超过 100 秒的上限：记下第二条，停下，第三条跳过。
    assert [item["rate_limit_retries"] for item in outcome["results"]] == [2, 0]
    assert outcome["skipped"] == 1 and outcome["stopped"] == "限流一直没有缓解"


def _quota_exhausted_error():
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response = httpx.Response(429, request=request, json={"error": {"type": "insufficient_quota", "message": "no credit"}})
    return ProviderCallError("openai_compatible", project_provider_error(
        httpx.HTTPStatusError("429", request=request, response=response)))


class _Failing(RuleOfThumbModel):
    """按调用序号失败：``fail_on`` 里的那几次调用抛 ``error()``，其它照标准答案走。"""

    def __init__(self, error, fail_on):
        super().__init__()
        self.error, self.fail_on = error, set(fail_on)

    def complete_with_tools(self, messages, tools, **options):
        if self.calls + 1 in self.fail_on:
            self.calls += 1
            raise self.error()
        return super().complete_with_tools(messages, tools, **options)


def test_exhausted_quota_is_unmeasured_but_not_retried():
    assert project_provider_error_code(_quota_exhausted_error()) == "quota_exhausted"
    items = [item for item in assistant_eval.load_items() if item["id"] == "task-02"]
    slept = []
    outcome = assistant_eval.run_eval(_Failing(_quota_exhausted_error, range(1, 99)), items, prices=FLASH,
                                      round_budget=10, rate_limit_retries=2, rate_limit_wait_seconds=35,
                                      max_rate_limit_wait_seconds=480, sleep=slept.append)
    (result,) = outcome["results"]
    assert result["error_code"] == "quota_exhausted" and result["rate_limit_retries"] == 0 and slept == []
    summary = assistant_eval.summarize(outcome["results"], skipped=0)
    assert summary["rate_limit"]["unmeasured"] == ["task-02"]
    assert summary["gates"][assistant_eval.ALL_MEASURED_GATE] is False


def test_skipped_items_fail_the_all_measured_gate():
    items = assistant_eval.load_items()[:3]
    outcome = assistant_eval.run_eval(RuleOfThumbModel(), items, prices=FLASH, round_budget=0.0000001)
    assert outcome["skipped"] == 2 and not any(assistant_eval.not_measured(item) for item in outcome["results"])
    summary = assistant_eval.summarize(outcome["results"], skipped=outcome["skipped"])
    assert summary["gates"][assistant_eval.ALL_MEASURED_GATE] is False
    report = _report_module().render_run({"summary": summary, "items": outcome["results"]}, None)
    assert "没测到 0 条，跳过 2 条" in report


def test_every_billed_attempt_is_counted_including_the_steps_before_a_rate_limit():
    # task-02 要两步（task_list → reply）。第一次：第 1 步成功、第 2 步被限流 → 降级；重跑：两步都成功。
    items = [item for item in assistant_eval.load_items() if item["id"] == "task-02"]
    outcome = assistant_eval.run_eval(_Failing(rate_limited_error, {2}), items, prices=FLASH, round_budget=10,
                                      rate_limit_retries=2, rate_limit_wait_seconds=35,
                                      max_rate_limit_wait_seconds=480, sleep=lambda seconds: None)
    (result,) = outcome["results"]
    assert result["passed"] and result["rate_limit_retries"] == 1
    # 成功的三步，每步 100 输入（60 命中缓存）、10 输出。
    assert result["tokens"] == {"prompt_tokens": 300, "completion_tokens": 30, "cached_tokens": 180}
    assert result["cost_usd"] == round(assistant_eval.cost_usd(result["tokens"], FLASH), 6)


def test_a_degraded_attempt_reports_the_usage_of_the_steps_it_completed():
    items = [item for item in assistant_eval.load_items() if item["id"] == "task-02"]
    outcome = assistant_eval.run_eval(_Failing(rate_limited_error, {2}), items, prices=FLASH, round_budget=10)
    (result,) = outcome["results"]
    assert result["outcome"] == "degrade" and result["error_code"] == "rate_limited"
    assert result["tokens"] == {"prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 60}
    assert result["cost_usd"] > 0


def test_an_obsolete_round_is_not_the_baseline_but_its_cost_still_counts(tmp_path):
    report = _report_module()
    for name, started, cost, obsolete in (("a", "2026-10-21T00:00:00", 0.05, False),
                                          ("b", "2026-10-22T00:00:00", 0.004, True)):
        data = {"schema": 1, "mode": "run", "started_at": started, "summary": {"cost_usd": cost}}
        if obsolete:
            data["obsolete"] = True
        (tmp_path / ("%s.json" % name)).write_text(json.dumps(data), encoding="utf-8")
    assert report.spent(tmp_path) == 0.054
    assert report.baseline(tmp_path)["started_at"] == "2026-10-21T00:00:00"
    (tmp_path / "a.json").unlink()
    assert report.baseline(tmp_path) is None and report.spent(tmp_path) == 0.004


def test_the_committed_a1_first_round_is_marked_obsolete():
    report = _report_module()
    directory = Path(__file__).resolve().parents[3] / "docs" / "评测报告" / "评分助手"
    first = json.loads((directory / "2026-10-22-A1-gemini-2.5-flash.json").read_text(encoding="utf-8"))
    assert first["obsolete"] is True
    assert (directory / "2026-10-22-A1-gemini-2.5-flash.md").read_text(encoding="utf-8").splitlines()[2].startswith(
        "> **⚠️ 本轮作废")
    assert report.spent(directory) >= first["summary"]["cost_usd"]
    assert report.baseline(directory) is None or not report.baseline(directory).get("obsolete")


def _wait_item():
    # 评测集里的准备消息（“上传我的评分规则和模板”“开始评分”）走快速通道、不调用模型；
    # 这里用一句会走 Agent 的准备消息，覆盖“准备步骤也计费、也会被限流”的情形。
    return [{"id": "setup-agent", "category": "等待中的步骤", "setup": ["现在有哪些评分任务？"],
             "utterance": "有几个评分任务", "expect": {"tools_any": ["task_list", "task_overview"]}}]


def test_setup_turns_are_billed_with_the_item():
    model = RuleOfThumbModel()
    outcome = assistant_eval.run_eval(model, _wait_item(), prices=FLASH, round_budget=10)
    (result,) = outcome["results"]
    assert result["passed"] and model.calls > result["model_calls"], "准备步骤也调用了模型"
    # 每一步 100 输入（60 命中缓存）、10 输出：准备步骤与被测的这句都要算进来。
    assert result["tokens"] == {"prompt_tokens": 100 * model.calls, "completion_tokens": 10 * model.calls,
                                "cached_tokens": 60 * model.calls}


def test_a_rate_limited_setup_retries_the_whole_item():
    slept = []
    model = _Failing(rate_limited_error, {1})
    outcome = assistant_eval.run_eval(model, _wait_item(), prices=FLASH, round_budget=10, rate_limit_retries=2,
                                      rate_limit_wait_seconds=35, max_rate_limit_wait_seconds=480,
                                      sleep=slept.append)
    (result,) = outcome["results"]
    assert result["passed"] and result["rate_limit_retries"] == 1 and slept == [35]
    assert result["tokens"]["prompt_tokens"] == 100 * (model.calls - 1)  # 被限流的那次没有用量


def test_a_setup_still_blocked_is_unmeasured_and_the_utterance_is_not_sent():
    model = _Failing(rate_limited_error, range(1, 99))
    outcome = assistant_eval.run_eval(model, _wait_item(), prices=FLASH, round_budget=10)
    (result,) = outcome["results"]
    assert result["reason"] == "setup" and result["error_code"] == "rate_limited" and model.calls == 1
    assert "准备步骤被挡住" in result["reasons"][0]
    assert assistant_eval.summarize(outcome["results"], skipped=0)["rate_limit"]["unmeasured"] == ["setup-agent"]


def _rendered_run(monkeypatch, capsys, tmp_path, *extra):
    _platform(monkeypatch, _Runtime())
    monkeypatch.setattr("backend.app.services.llm.factory.get_llm_scorer", lambda runtime: RuleOfThumbModel())
    assert script.main(["--mode", "run", "--phase", "A1", "--spent-usd", "0", "--confirm-usd", "0.5",
                        "--pace-seconds", "0", *extra]) == 0
    raw = tmp_path / "raw.txt"
    raw.write_text(capsys.readouterr().out, encoding="utf-8")
    reports = tmp_path / "reports"
    assert _report_module().main(["render", str(raw), "--previous-dir", str(reports), "--out", str(reports)]) == 0
    capsys.readouterr()
    (written,) = sorted(reports.glob("*.json"))
    return reports, written, json.loads(written.read_text(encoding="utf-8"))


def test_reports_record_the_prompt_version_and_the_gate_reads_it(monkeypatch, capsys, tmp_path):
    from backend.app.scripts import assistant_eval_gate as gate
    from backend.app.services.assistant.agent.context import SYSTEM_VERSION
    from backend.app.services.assistant.agent.context import prompt_fingerprint

    reports, written, data = _rendered_run(monkeypatch, capsys, tmp_path)
    total = len(assistant_eval.load_items())
    assert data["prompt"] == {"version": SYSTEM_VERSION, "fingerprint": prompt_fingerprint()}
    assert data["eval_set"] == {"version": assistant_eval.eval_set_version(), "count": total, "total": total}
    assert "指纹 `%s`" % prompt_fingerprint() in written.with_suffix(".md").read_text(encoding="utf-8")

    # 跑满评测集、门槛全过：这一轮能放行。
    assert all(data["summary"]["gates"].values())
    current = {"eval_version": assistant_eval.eval_set_version(), "eval_count": total}
    assert gate.passing_reports(reports, prompt_fingerprint(), **current) == [written.name]
    assert gate.main([str(reports)]) == 0
    # 别的指纹、别的评测集版本：都不算。
    assert gate.passing_reports(reports, "0000000000000000", **current) == []
    assert gate.passing_reports(reports, prompt_fingerprint(), eval_version="000000000000", eval_count=total) == []
    # 作废、有门槛没过、有跳过：都不算。
    summary = data["summary"]
    for change in ({"obsolete": True}, {"summary": {**summary, "gates": {**summary["gates"], "违规 0 次": False}}},
                   {"summary": {**summary, "skipped": 1}}):
        written.write_text(json.dumps({**data, **change}, ensure_ascii=False), encoding="utf-8")
        assert gate.main([str(reports)]) == 1
    assert "还没有达标的评测" in capsys.readouterr().err


def test_a_partial_run_does_not_open_the_gate_even_when_its_gates_pass(monkeypatch, capsys, tmp_path):
    from backend.app.scripts import assistant_eval_gate as gate

    reports, _written, data = _rendered_run(monkeypatch, capsys, tmp_path, "--max-items", "2")
    assert data["eval_set"]["count"] == 2 and data["eval_set"]["total"] == len(assistant_eval.load_items())
    assert all(data["summary"]["gates"].values())  # 两条都过、没跳过、没被限流挡住……
    assert gate.main([str(reports)]) == 1  # ……但只测了两条，不能当上线依据


def test_a_reply_claiming_a_write_is_a_violation_in_the_eval():
    observed_text = "我已经帮你取消了评分。"
    assert assistant_eval.claims.done_by_agent(observed_text) == "我已经帮你取消"
