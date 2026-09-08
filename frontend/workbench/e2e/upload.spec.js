import { expect, test } from "@playwright/test";

/**
 * V08 · 多文件上传的浏览器编排（前端 v2 计划 §12.1、§5-E）。
 *
 * **不覆盖 Supabase 直传与 TUS**：那需要真实对象存储，而 §12.2 明写「Mock 网络
 * 路由不冒充真实 Supabase 验证」。造一个签名上传的桩会让这组用例看起来覆盖了
 * V08，实际验证的只是桩自己。真实存储的证据另行归档。
 *
 * 这里覆盖的是不依赖对象存储的那一半：浏览器侧预检、逐文件状态、以及**一个
 * 文件被拒不影响其它文件**——批量上传里最容易被写成「一错全停」的地方。
 */
const DOCX_TYPE =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

async function openNewTask(page) {
  await page.goto("/workbench/tasks/new");
  await expect(page.getByRole("heading", { name: "新建评分任务" })).toBeVisible();
}

function docx(name, size = 2048) {
  return { name, mimeType: DOCX_TYPE, buffer: Buffer.alloc(size, 7) };
}

test.describe("V08 上传预检与逐文件状态", () => {
  test("不支持的扩展名当场被拒并说明接受哪些", async ({ page }) => {
    await openNewTask(page);

    await page.locator('input[type="file"]').setInputFiles({
      name: "scan.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("不是论文"),
    });

    const row = page.locator("ul.queue li", { hasText: "scan.txt" });
    await expect(row).toContainText("不支持的文件类型");
    // 文案要说清接受什么，否则用户只知道错了、不知道该换成什么。
    await expect(row).toContainText(".docx");
  });

  test("被拒的文件不进入待上传计数", async ({ page }) => {
    await openNewTask(page);

    await page.locator('input[type="file"]').setInputFiles([
      docx("good.docx"),
      { name: "bad.txt", mimeType: "text/plain", buffer: Buffer.from("x") },
    ]);

    // 一个被拒不影响另一个：批量上传最容易被写成「一错全停」。
    await expect(page.locator("ul.queue li", { hasText: "good.docx" })).toContainText(
      "pending",
    );
    await expect(page.locator("ul.queue li", { hasText: "bad.txt" })).toContainText(
      "rejected",
    );
  });

  test("多个文件各自一行，逐文件显示状态", async ({ page }) => {
    await openNewTask(page);

    await page
      .locator('input[type="file"]')
      .setInputFiles([docx("a.docx"), docx("b.docx"), docx("c.docx")]);

    await expect(page.locator("ul.queue li")).toHaveCount(3);
  });

  test("同一个文件选两次不会被静默合并", async ({ page }) => {
    await openNewTask(page);
    const input = page.locator('input[type="file"]');

    await input.setInputFiles([docx("same.docx")]);
    await input.setInputFiles([docx("same.docx")]);

    // 合并会让用户以为漏选了；如实显示两行，重复由用户自己决定怎么处理。
    await expect(page.locator("ul.queue li", { hasText: "same.docx" })).toHaveCount(2);
  });

  test("上传入口在没有选择文件时不可点", async ({ page }) => {
    await openNewTask(page);

    await expect(page.getByRole("button", { name: "上传并解析" })).toBeDisabled();
  });
});
