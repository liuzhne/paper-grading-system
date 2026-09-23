import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

const root = resolve(process.cwd(), "../..");
const workbook = execFileSync(process.env.PGS_PYTHON || resolve(root, ".venv/bin/python"), ["-c", `
import sys
from backend.app.tests.test_rubric_import_sessions import _rules_with_decimal_scores
sys.stdout.buffer.write(_rules_with_decimal_scores(40, 60))
`], { cwd: root });

test("确认后返回第一步仍为原型工作区，保存成功才进入评分规则", async ({ page, request }) => {
  const name = `原型工作区-${Date.now()}`;
  const created = await request.post("/api/rubrics/import-sessions", { multipart: {
    name, version: "v1", rules_file: { name: "标准.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: workbook },
  } });
  expect(created.ok()).toBeTruthy();
  const draft = await created.json();
  const confirmed = await request.post(`/api/rubrics/import-sessions/${draft.id}/confirm`, { data: {
    expected_state_version: draft.state_version, idempotency_key: name,
  } });
  expect(confirmed.ok()).toBeTruthy();
  const rubricId = (await confirmed.json()).rubric.id;

  await page.goto("/workbench/rubrics");
  await page.getByRole("button", { name: "模板库", exact: true }).click();
  await page.locator(".library-menu .lib-item", { hasText: name }).click();
  const steps = page.getByRole("navigation", { name: "评分标准编辑步骤" });
  await expect(steps.getByRole("button", { name: /基本信息与评分项/ })).toHaveAttribute("aria-current", "step");
  const workspace = page.locator("[data-test=rubric-import-workspace]");
  await expect(workspace).toBeVisible();
  await expect(workspace).toContainText("标准.xlsx");
  await expect(workspace.locator("tbody tr")).toHaveCount(2);
  await expect(page.locator(".criteria-nav")).toHaveCount(0);
  await expect(page.locator(".blockers")).toHaveCount(0);
  await workspace.getByRole("button", { name: "预览原文", exact: true }).click();
  await expect(workspace.locator("[data-test=source-preview]")).toContainText("评分项1");
  await workspace.getByRole("button", { name: "关闭", exact: true }).click();
  await page.screenshot({ path: test.info().outputPath("rubric-step-one-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await expect(workspace.getByRole("button", { name: "保存评分项，下一步", exact: true })).toBeEnabled();
  await page.screenshot({ path: test.info().outputPath("rubric-step-one-mobile.png"), fullPage: true });
  await page.setViewportSize({ width: 1280, height: 720 });

  await workspace.getByLabel("标准名称", { exact: true }).fill(`${name}-修改`);
  // 保存失败必须保留输入，不能跳到下一步。
  await page.route(`**/rubrics/${rubricId}/recompile`, route => route.fulfill({
    status: 409, contentType: "application/json", body: JSON.stringify({ detail: "合成保存冲突" }),
  }));
  await workspace.getByRole("button", { name: "保存评分项，下一步", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "合成保存冲突" })).toBeVisible();
  await expect(workspace.getByLabel("标准名称", { exact: true })).toHaveValue(`${name}-修改`);
  await expect(steps.getByRole("button", { name: /基本信息与评分项/ })).toHaveAttribute("aria-current", "step");
  await page.unroute(`**/rubrics/${rubricId}/recompile`);
  await workspace.getByRole("button", { name: "保存评分项，下一步", exact: true }).click();
  await expect(steps.getByRole("button", { name: /评分规则/ })).toHaveAttribute("aria-current", "step");
  await expect(page.locator("[data-test=rule-review-panel]")).toBeVisible();
  expect((await (await request.get(`/api/rubrics/${rubricId}`)).json()).name).toBe(`${name}-修改`);
});
