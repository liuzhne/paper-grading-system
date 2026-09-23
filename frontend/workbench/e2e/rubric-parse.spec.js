import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

/**
 * 解析台账与第一级门禁（解析重构方案 §8）：
 * 未识别的疑似规则在第 1 步处理，处理前不能进入规则拆分；
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
  await page.getByRole("button", { name: "导入评分模板", exact: true }).click();
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

test("未识别的疑似规则和 AI 结构辅助在第 1 步处理，第二步只处理评分规则", async ({ page, request }) => {
  const { id } = await importFiles(page, { excel: blockingWorkbook });
  const steps = page.getByRole("navigation", { name: "评分标准编辑步骤" });
  await expect(steps.getByRole("button", { name: /基本信息与评分项/ })).toHaveAttribute("aria-current", "step");
  const coveragePanel = page.locator("[data-test=parse-coverage]");
  await expect(coveragePanel).toBeVisible();
  await expect(coveragePanel).toContainText("还有 1 条疑似规则、0 个冲突未处理");
  await expect(coveragePanel).toContainText("错别字每处扣1分");
  await expect(coveragePanel).toContainText("AI 结构辅助");

  await steps.getByRole("button", { name: /评分规则/ }).click();
  await expect(coveragePanel).toBeVisible();
  await expect(steps.getByRole("button", { name: /基本信息与评分项/ })).toHaveAttribute("aria-current", "step");

  const row = coveragePanel.locator("[data-test='unit-xlsx:评分规则!R4C4']");
  await row.getByRole("button", { name: "不是规则", exact: true }).click();
  await expect(coveragePanel).toContainText("原文中的疑似规则已全部处理");
  const state = await (await request.get(`/api/rubrics/${id}/parse-coverage`)).json();
  expect(state.coverage.blocking_count).toBe(0);

  await steps.getByRole("button", { name: /评分规则/ }).click();
  await expect(coveragePanel).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "AI 规则拆分与细则完整度" })).toBeVisible();
  await expect(page.locator("[data-test=rule-review-panel]")).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("rubric-parse-gate.png"), fullPage: true });

  await page.getByRole("button", { name: "前往校验与发布" }).click();
  const audit = page.locator("[data-test=rule-audit]");
  await audit.getByLabel("审查范围").selectOption("all");
  await audit.getByRole("button", { name: "估算审查规模" }).click();
  await expect(audit.locator("[data-test=review-run]")).toContainText("确认审查 2 个评分项");
});

test("只上传 Word 也能导入；没有扣分规则时审查如实说明", async ({ page }) => {
  await importFiles(page, { word: wordRules });
  const coveragePanel = page.locator("[data-test=parse-coverage]");
  await expect(coveragePanel).toContainText("迟交一天扣5分");
  await expect(coveragePanel.locator("[data-test=batch-not-rule]")).toBeDisabled();
  await coveragePanel.locator("input[type=checkbox]").first().check();
  await coveragePanel.locator("[data-test=batch-not-rule]").click();
  await expect(coveragePanel).toContainText("原文中的疑似规则已全部处理");
  await page.getByRole("navigation", { name: "评分标准编辑步骤" }).getByRole("button", { name: /评分规则/ }).click();
  await page.getByRole("button", { name: "前往校验与发布" }).click();
  const audit = page.locator("[data-test=rule-audit]");
  await expect(audit).toContainText("发布时会记录“未经审查即发布”");
  await audit.getByLabel("审查范围").selectOption("all");
  await audit.getByRole("button", { name: "估算审查规模" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "没有需要审查的扣分规则" })).toBeVisible();
  await expect(audit.locator("[data-test=review-run]")).toHaveCount(0);
});
