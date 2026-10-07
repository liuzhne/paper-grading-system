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
