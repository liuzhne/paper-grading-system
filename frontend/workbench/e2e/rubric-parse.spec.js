import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

/**
 * 解析台账与第一级门禁（解析重构方案 §8）：
 * 未识别的疑似规则在第 2 步「待归类原文」处理，处理前不能进入校验与发布；
 * 只上传 Word 也能导入；第 3 步提供可跳过的规则审查。合成数据，不含真实 PII。
 */
const root = resolve(process.cwd(), "../..");
const python = process.env.PGS_PYTHON || resolve(root, ".venv/bin/python");
const build = (expression) => execFileSync(python, ["-c", `
import sys
from backend.app.tests import rubric_parse_fixtures as fx
from backend.app.tests.test_rubric_parse_coverage_api import _rules_with_blocking_row
sys.stdout.buffer.write(${expression})
`], { cwd: root });
const blockingWorkbook = build("_rules_with_blocking_row()");
const wordRules = build("fx.rules_docx()");
const unmappedWorkbook = build("fx.unmapped_header_xlsx()");
const connection = { id: "synthetic", name: "合成测试连接", model_name: "fixture", status: "active" };

/** 方案 C：审查与结构识别是后台任务，浏览器只看到任务；这里按真实接口的形状合成任务状态。 */
function taskView(task) {
  return {
    rubric_id: null, scope: {}, model_name: "fixture", ai_connection_id: connection.id,
    total_items: task.items.length, pending_count: 0, canceled_count: 0,
    running_count: task.items.filter((item) => item.status === "running").length,
    succeeded_count: task.items.filter((item) => item.status === "succeeded").length,
    failed_count: task.items.filter((item) => item.status === "failed").length,
    result: null, error_code: null, error_message: null, state_version: 1,
    created_at: "2026-10-10T00:00:00", updated_at: "2026-10-10T00:00:00", ...task,
    items: task.items.map((item, ordinal) => ({ id: `${task.id}-${ordinal}`, ordinal, attempt_count: 1,
      deferral_count: 0, unit_count: 0, label: null, ...item })),
  };
}

async function importFiles(page, files) {
  const name = `解析台账合成模板-${Date.now()}`;
  await page.goto("/workbench/rubrics");
  await page.getByRole("button", { name: "新建评分标准", exact: true }).click();
  const panel = page.locator(".import-panel");
  await panel.getByLabel("标准名称", { exact: true }).fill(name);
  if (files.word) await panel.getByLabel(/评分标准文档/).setInputFiles({ name: "rules.docx", mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", buffer: files.word });
  if (files.excel) await panel.getByLabel("评分表", { exact: true }).setInputFiles({ name: "rules.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: files.excel });
  const imported = page.waitForResponse((r) => r.url().endsWith("/rubrics/import-sessions") && r.request().method() === "POST");
  await panel.getByRole("button", { name: "解析文件", exact: true }).click();
  const response = await imported;
  expect(response.ok(), await response.text()).toBeTruthy();
  await expect(page.locator("[data-test=rubric-import-workspace]")).toBeVisible();
  const confirmed = page.waitForResponse((r) => /\/rubrics\/import-sessions\/[^/]+\/confirm$/.test(r.url()) && r.request().method() === "POST");
  await page.locator('[data-test="confirm-import-session"]').click();
  const confirmResponse = await confirmed;
  expect(confirmResponse.ok(), await confirmResponse.text()).toBeTruthy();
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
  return { id: (await confirmResponse.json()).rubric.id, name };
}

// 09-29 分步改造：第 1 步只核对评分项；未归入的原文在第 2 步「待归类原文」里处理。
async function saveStepOne(page) {
  const steps = page.getByRole("navigation", { name: "评分标准编辑步骤" });
  await expect(steps.getByRole("button", { name: /基本信息与评分项/ })).toHaveAttribute("aria-current", "step");
  await expect(page.locator(".source-review")).toHaveCount(0);
  await page.locator("[data-test=rubric-import-workspace]").getByRole("button", { name: "保存评分项，下一步", exact: true }).click();
  await expect(steps.getByRole("button", { name: /评分规则/ })).toHaveAttribute("aria-current", "step");
  return steps;
}

test("未识别的疑似规则在第 2 步待归类原文中处理，处理前不能进入校验与发布", async ({ page, request }) => {
  const { id } = await importFiles(page, { excel: blockingWorkbook });
  const steps = await saveStepOne(page);
  const ruleBlocker = page.locator("[data-test=step-two-blockers]").getByRole("button", { name: "待归类 · 1 条疑似规则", exact: true });
  await expect(ruleBlocker).toBeVisible();

  await page.getByRole("button", { name: "前往校验与发布", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "请先处理疑似规则" })).toBeVisible();
  await expect(steps.getByRole("button", { name: /评分规则/ })).toHaveAttribute("aria-current", "step");

  await ruleBlocker.click();
  await expect(page.getByRole("tab", { name: "疑似规则", exact: true })).toHaveAttribute("aria-selected", "true");
  const context = page.locator(".source-review .context-panel");
  await expect(context).toContainText("错别字每处扣1分");
  await context.getByRole("button", { name: /^不是规则/ }).click();
  await expect(ruleBlocker).toHaveCount(0);
  const state = await (await request.get(`/api/rubrics/${id}/parse-coverage`)).json();
  expect(state.coverage.blocking_count).toBe(0);
  await page.screenshot({ path: test.info().outputPath("rubric-parse-gate.png"), fullPage: true });

  await page.getByRole("button", { name: "前往校验与发布", exact: true }).click();
  const audit = page.locator("[data-test=rule-audit]");
  await audit.getByLabel("审查范围").selectOption("all");
  await audit.getByRole("button", { name: "估算审查规模" }).click();
  await expect(audit.locator("[data-test=review-run]")).toContainText("确认审查 2 个评分项");
});

test("只上传 Word 也能导入；没有扣分规则时审查如实说明", async ({ page }) => {
  await importFiles(page, { word: wordRules });
  await saveStepOne(page);
  await page.locator("[data-test=step-two-blockers]").getByRole("button", { name: /^待归类 · \d+ 条疑似规则$/ }).click();
  const review = page.locator(".source-review");
  await expect(review.locator(".paragraph-list")).toContainText("迟交一天扣5分");
  await expect(review.locator("[data-test=batch-not-rule]")).toHaveCount(0);
  await review.locator(".paragraph-list input[type=checkbox]").first().check();
  await review.locator("[data-test=batch-not-rule]").click();
  await expect(page.locator("[data-test=step-two-blockers]").getByRole("button", { name: /条疑似规则$/ })).toHaveCount(0);
  await page.getByRole("button", { name: "前往校验与发布", exact: true }).click();
  const audit = page.locator("[data-test=rule-audit]");
  await expect(audit).toContainText("发布时会记录“未经审查即发布”");
  await audit.getByLabel("审查范围").selectOption("all");
  await audit.getByRole("button", { name: "估算审查规模" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "没有需要审查的扣分规则" })).toBeVisible();
  await expect(audit.locator("[data-test=review-run]")).toHaveCount(0);
});

test("规则审查是后台任务：提交后显示进度，失败只重试失败的评分项，完成后显示审查结果", async ({ page, request }) => {
  await page.route("**/api/ai-connections", (route) => route.fulfill({ json: [connection] }));
  const submitted = [];
  let task = null;
  await page.route("**/api/rubrics/*/ai-tasks**", async (route) => {
    if (route.request().method() === "GET") return route.fulfill({ json: [] });
    const body = route.request().postDataJSON();
    submitted.push(body);
    const rubricId = new URL(route.request().url()).pathname.split("/").at(-2);
    task = { id: "review-task", kind: "rule_review", rubric_id: rubricId, status: "running", polls: 0, items: [
      { label: "C01", status: "running" }, { label: "C02", status: "pending" }, { label: "__cross__", status: "pending" },
    ] };
    await route.fulfill({ status: 202, json: taskView(task) });
  });
  await page.route("**/api/ai-tasks/**", async (route) => {
    const action = new URL(route.request().url()).pathname.split("/").pop();
    if (action === "retry") {
      task = { ...task, status: "running", retried: true, error_code: null, error_message: null,
        items: task.items.map((item) => item.status === "succeeded" ? item : { ...item, status: "pending" }) };
      return route.fulfill({ json: taskView(task) });
    }
    task.polls += 1;
    // 第一轮：C01 完成、C02 因额度失败；重试后全部完成。
    if (!task.retried && task.polls > 1) {
      task = { ...task, status: "failed", error_code: "AI_PROVIDER_ERROR", error_message: "AI 连接的额度已用完。",
        items: [{ label: "C01", status: "succeeded" }, { label: "C02", status: "failed" }, { label: "__cross__", status: "canceled" }] };
    } else if (task.retried && task.polls > 2) {
      task = { ...task, status: "succeeded", items: task.items.map((item) => ({ ...item, status: "succeeded" })) };
    }
    await route.fulfill({ json: taskView(task) });
  });
  const stored = { reviewed: true, stale: false, prechecks: [], failed: [], findings: [
    { id: "F1", criterion_code: "C02", type: "undecidable", severity: "low", quote: "错别字", problem: "条件难以客观判断", example: "", status: "open" },
  ] };
  await page.route("**/api/rubrics/*/rule-review", (route) => route.request().method() === "GET"
    ? route.fulfill({ json: task?.status === "succeeded" ? stored : { reviewed: false } })
    : route.fallback());

  await importFiles(page, { excel: blockingWorkbook });
  await saveStepOne(page);
  await page.locator("[data-test=step-two-blockers]").getByRole("button", { name: "待归类 · 1 条疑似规则", exact: true }).click();
  await page.locator(".source-review .context-panel").getByRole("button", { name: /^不是规则/ }).click();
  await page.getByRole("button", { name: "前往校验与发布", exact: true }).click();
  const audit = page.locator("[data-test=rule-audit]");
  await audit.getByLabel("审查范围").selectOption("all");
  await audit.getByRole("button", { name: "估算审查规模" }).click();
  await audit.locator("[data-test=review-run]").click();
  expect(submitted).toEqual([{ kind: "rule_review", params: { scope: "all" }, ai_connection_id: connection.id, regenerate: false }]);
  await expect(audit.locator("[data-test=review-task]")).toContainText("规则审查中");
  await expect(audit.locator("[data-test=review-task]")).toContainText("C02 AI 连接的额度已用完。");
  await audit.locator("[data-test=review-retry]").click();
  await expect(audit).toContainText("审查发现（1）");
  await expect(audit.locator("[data-test=finding-F1]")).toContainText("条件难以客观判断");
  await expect(audit.locator("[data-test=review-task]")).toHaveCount(0);
  expect(submitted).toHaveLength(1);
});

test("导入前的表格结构识别是后台任务：估算后提交，完成后按识别出的结构导入", async ({ page }) => {
  await page.route("**/api/ai-connections", (route) => route.fulfill({ json: [connection] }));
  let task = null;
  await page.route("**/api/ai-tasks/import-structure", async (route) => {
    const form = route.request().postData() || "";
    expect(form).toContain('name="ai_connection_id"');
    task = { id: "structure-task", kind: "structure_suggestion", status: "running", polls: 0, items: [{ label: "import", status: "running" }] };
    await route.fulfill({ status: 202, json: taskView(task) });
  });
  await page.route("**/api/ai-tasks/structure-task", async (route) => {
    task.polls += 1;
    if (task.polls > 1) {
      task = { ...task, status: "succeeded", items: [{ label: "import", status: "succeeded" }], result: {
        target: "import", failure_codes: ["E1"], prompt_version: "rubric-structure@2", reasons: {}, unresolved: [],
        override: { sheet: "Sheet", header_row: 1, column_mapping: { name: 1, max_score: 2, description: 3 }, row_types: {} },
        preview: [{ name: "选题", max_score: 10, row_number: 2 }],
      } };
    }
    await route.fulfill({ json: taskView(task) });
  });
  const name = `结构识别合成模板-${Date.now()}`;
  await page.goto("/workbench/rubrics");
  await page.getByRole("button", { name: "新建评分标准", exact: true }).click();
  const panel = page.locator(".import-panel");
  await panel.getByLabel("标准名称", { exact: true }).fill(name);
  await panel.getByLabel("评分表", { exact: true }).setInputFiles({ name: "rules.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: unmappedWorkbook });
  await panel.getByRole("button", { name: "解析文件", exact: true }).click();
  const box = page.locator("[data-test=import-structure]");
  await expect(box).toBeVisible();
  const estimated = page.waitForResponse((r) => r.url().endsWith("/rubrics/import-files/structure-suggestions/estimate"));
  await box.getByRole("button", { name: "估算识别规模" }).click();
  expect((await estimated).ok()).toBeTruthy();
  await expect(box).toContainText("调用 1 次模型");
  await box.getByLabel("用哪个 AI 连接").selectOption(connection.id);
  await box.getByRole("button", { name: "确认调用 AI 识别结构" }).click();
  await expect(box).toContainText("将导入 1 个评分项");
  await expect(box).toContainText("第 2 行 · 选题（10 分）");
  const imported = page.waitForResponse((r) => r.url().endsWith("/rubrics/import-sessions") && r.request().method() === "POST");
  await box.getByRole("button", { name: "按此结构导入" }).click();
  const response = await imported;
  expect(response.ok(), await response.text()).toBeTruthy();
  await expect(page.locator("[data-test=rubric-import-workspace]")).toBeVisible();
});

test("草稿的表格结构识别是后台任务：刷新后找回进度，可以取消", async ({ page }) => {
  await page.route("**/api/ai-connections", (route) => route.fulfill({ json: [connection] }));
  let task = null;
  await page.route("**/api/rubrics/*/ai-tasks**", async (route) => {
    if (route.request().method() === "GET") {
      const active = task && ["queued", "running"].includes(task.status);
      const kind = new URL(route.request().url()).searchParams.get("kind");
      return route.fulfill({ json: active && (!kind || kind === task.kind) ? [taskView(task)] : [] });
    }
    const body = route.request().postDataJSON();
    expect(body).toEqual({ kind: "structure_suggestion", params: { target: "draft" }, ai_connection_id: connection.id, regenerate: false });
    const rubricId = new URL(route.request().url()).pathname.split("/").at(-2);
    task = { id: "draft-structure", kind: "structure_suggestion", rubric_id: rubricId, status: "running", items: [{ label: "draft", status: "running" }] };
    await route.fulfill({ status: 202, json: taskView(task) });
  });
  await page.route("**/api/ai-tasks/draft-structure**", async (route) => {
    if (route.request().url().endsWith("/cancel")) {
      task = { ...task, status: "canceled", items: [{ label: "draft", status: "canceled" }] };
    }
    await route.fulfill({ json: taskView(task) });
  });
  const { name } = await importFiles(page, { excel: blockingWorkbook });
  const recognition = page.locator(".table-recognition");
  await recognition.locator("[data-test=structure-estimate]").click();
  await expect(recognition).toContainText("调用 1 次模型");
  await recognition.locator("[data-test=structure-run]").click();
  await expect(recognition.locator("[data-test=structure-task]")).toContainText("AI 正在识别表格结构");

  // 刷新或关页面不影响执行：回到这份标准时找回进行中的任务。
  await page.reload();
  await page.getByRole("button", { name: "模板库", exact: true }).click();
  await page.locator(".library-menu .lib-item", { hasText: name }).click();
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
  await expect(recognition.locator("[data-test=structure-task]")).toContainText("关闭页面不会中断");
  await expect(recognition.locator("[data-test=structure-estimate]")).toBeDisabled();
  await recognition.locator("[data-test=structure-cancel]").click();
  await expect(recognition.locator("[data-test=structure-task]")).toContainText("已取消识别");
  await expect(recognition.locator("[data-test=structure-estimate]")).toBeEnabled();
});
