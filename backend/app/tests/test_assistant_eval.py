"""评分助手评测（Agent 化方案 §27）：离线，用按说法行事的假模型跑完整个评测集。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from backend.app.core.config import settings
from backend.app.eval import assistant_eval
from backend.app.scripts import run_assistant_eval as script
from backend.app.services.assistant import model as assistant_model
from backend.app.tests.test_assistant_agent import rate_limited_error
from backend.app.tests.test_assistant_agent import reply
from backend.app.tests.test_assistant_agent import step

FLASH = assistant_eval.MODEL_PRICES["gemini-2.5-flash"]

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
            if needs_task and "tasks" not in got:
                return step(("task_list", {"limit": 20}))
            if not searched:
                return step(("tool_search", {"query": "select:%s" % name}))
            if not any("提示" in result for result in results) and len(results) < 4:
                tasks = next((result["tasks"] for result in results if "tasks" in result), [])
                task = pick(tasks) if needs_task else None
                arguments = {"task_ref": task["ref"]} if task else {}
                return step(("call_tool", {"name": name, "arguments_json": json.dumps(arguments)}))
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
                                                                 "每条都测到（没有被限流挡住）"}
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
    assert summary["gates"]["每条都测到（没有被限流挡住）"] is True


def test_an_item_still_rate_limited_after_retries_is_reported_as_unmeasured():
    items = [item for item in assistant_eval.load_items() if item["id"] == "task-02"]
    outcome = assistant_eval.run_eval(_RateLimitedFirst(99), items, prices=FLASH, round_budget=10,
                                      rate_limit_retries=2, rate_limit_wait_seconds=35,
                                      max_rate_limit_wait_seconds=480, sleep=lambda seconds: None)
    (result,) = outcome["results"]
    assert result["error_code"] == "rate_limited" and result["rate_limit_retries"] == 2 and not result["passed"]
    summary = assistant_eval.summarize(outcome["results"], skipped=0)
    assert summary["rate_limit"]["unmeasured"] == ["task-02"]
    assert summary["gates"]["每条都测到（没有被限流挡住）"] is False
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
