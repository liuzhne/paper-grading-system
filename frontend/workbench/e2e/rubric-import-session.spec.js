import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

const root = resolve(process.cwd(), "../..");
const python = process.env.PGS_PYTHON || resolve(root, ".venv/bin/python");
const workbook = (scores) => execFileSync(python, ["-c", `
from io import BytesIO
import sys
from openpyxl import Workbook
book = Workbook()
sheet = book.active
sheet.title = "评分规则"
sheet.append(["编号", "评分项", "分值", "评分说明"])
for index, score in enumerate(${JSON.stringify(scores)}, 1):
    sheet.append([f"C{index:02d}", f"评分项{index}", score, f"说明{index}"])
out = BytesIO()
book.save(out)
sys.stdout.buffer.write(out.getvalue())
`], { cwd: root });

test("确定性解析停在临时会话；换算、原文预览和重新上传均需用户确认", async ({ page }) => {
  const name = `临时导入会话-${Date.now()}`;
  await page.goto("/workbench/rubrics");
  await page.getByRole("button", { name: "新建评分标准", exact: true }).click();
  const panel = page.locator(".import-panel");
  await panel.getByLabel("标准名称", { exact: true }).fill(name);
  await panel.getByLabel("评分表", { exact: true }).setInputFiles({
    name: "decimal.xlsx",
    mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    buffer: workbook([10.4, 10.5]),
  });
  const parsed = page.waitForResponse((response) => response.url().endsWith("/rubrics/import-sessions") && response.request().method() === "POST");
  await panel.getByRole("button", { name: "解析文件", exact: true }).click();
  expect((await parsed).ok()).toBeTruthy();

  const workspace = page.locator("[data-test=rubric-import-workspace]");
  await expect(workspace).toBeVisible();
  await expect(workspace).toContainText("10.4 已按四舍五入调整为 10");
  await expect(workspace).toContainText("10.5 已按四舍五入调整为 11");
  await expect(workspace).not.toContainText("适用类型");
  const renamed = page.waitForResponse(item => /\/import-sessions\/[^/]+$/.test(item.url()) && item.request().method() === "PATCH");
  await workspace.getByLabel("C01 评分项名称", { exact: true }).fill("人工核对后的名称");
  await workspace.getByLabel("C01 评分项名称", { exact: true }).press("Tab");
  expect((await renamed).ok()).toBeTruthy();
  const added = page.waitForResponse(item => /\/import-sessions\/[^/]+$/.test(item.url()) && item.request().method() === "PATCH");
  await workspace.getByRole("button", { name: "新增评分项", exact: true }).click();
  expect((await added).ok()).toBeTruthy();
  await expect(workspace.getByLabel("C03 评分项名称", { exact: true })).toHaveValue("新评分项");
  await expect(workspace.locator('[data-test="confirm-import-session"]')).toBeDisabled();
  const removed = page.waitForResponse(item => /\/import-sessions\/[^/]+$/.test(item.url()) && item.request().method() === "PATCH");
  await workspace.getByRole("button", { name: "删除 C03", exact: true }).click();
  expect((await removed).ok()).toBeTruthy();
  await expect(workspace.locator('[data-test="confirm-import-session"]')).toBeEnabled();

  await workspace.getByRole("button", { name: "预览原文" }).click();
  await expect(workspace.locator("[data-test=source-preview]")).toContainText("评分项1");

  const replacement = workbook([20, 5]);
  const diffResponse = page.waitForResponse((response) => response.url().endsWith("/reupload-preview"));
  await workspace.getByLabel("评分表", { exact: true }).setInputFiles({
    name: "replacement.xlsx",
    mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    buffer: replacement,
  });
  expect((await diffResponse).ok()).toBeTruthy();
  await expect(workspace.locator("[data-test=reupload-preview]")).toContainText("重新上传差异");
  const replaced = page.waitForResponse((response) => response.url().endsWith("/reupload-confirm"));
  await workspace.locator('[data-test="confirm-reupload"]').click();
  expect((await replaced).ok()).toBeTruthy();
  await expect(workspace).toContainText("合计 25 分");

  const confirmationKeys = [];
  await page.route("**/rubrics/import-sessions/*/confirm", async route => {
    confirmationKeys.push(route.request().postDataJSON().idempotency_key);
    if (confirmationKeys.length === 1) {
      // 服务端已创建，但响应丢失：再次点击应返回同一标准。
      const committed = await route.fetch();
      expect(committed.ok()).toBeTruthy();
      return route.abort("failed");
    }
    return route.continue();
  });
  await workspace.locator('[data-test="confirm-import-session"]').click();
  await expect(page.getByRole("alert").filter({ hasText: /Failed to fetch/ })).toBeVisible();
  const confirmed = page.waitForResponse((response) => /\/rubrics\/import-sessions\/[^/]+\/confirm$/.test(response.url()));
  await workspace.locator('[data-test="confirm-import-session"]').click();
  const response = await confirmed;
  expect(response.ok(), await response.text()).toBeTruthy();
  expect(confirmationKeys[1]).toBe(confirmationKeys[0]);
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();

  await page.getByRole("navigation", { name: "评分标准编辑步骤" })
    .getByRole("button", { name: /基本信息与评分项/ }).click();
  const formal = page.locator("[data-test=rubric-import-workspace]");
  await expect(formal).toBeVisible();
  const formalPreview = page.waitForResponse((item) => item.url().endsWith("/reupload-preview"));
  await formal.getByLabel("评分表", { exact: true }).setInputFiles({
    name: "successor.xlsx",
    mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    buffer: workbook([20, 6]),
  });
  expect((await formalPreview).ok()).toBeTruthy();
  await expect(formal.locator("[data-test=reupload-preview]")).toContainText("变化");
  await expect(formal.getByRole("button", { name: "保存评分项，下一步" })).toBeDisabled();
  const successor = page.waitForResponse((item) => item.url().endsWith("/reupload-confirm"));
  await formal.getByRole("button", { name: "确认替换草稿" }).click();
  expect((await successor).ok()).toBeTruthy();
});
