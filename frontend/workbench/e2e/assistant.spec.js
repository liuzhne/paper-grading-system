import { expect, test } from "@playwright/test";

/**
 * 评分助手（对话评分助手方案）：对话推进，工作区就是现有页面。
 *
 * 验收后端没有评分 worker，这里不跑“评完再汇报”——那段由前端单测
 * （stores/assistant.test.js）与本地全流程验证覆盖。这里证明的是真实托管下：
 * 意图识别走规则、论文由代码定位、扣分与证据从接口现取、工作区 iframe 能嵌入
 * 现有页面且页面在框架里隐藏了外壳侧栏。
 */
const DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

async function openAssistant(page) {
  await page.goto("/workbench/assistant");
  await expect(page.getByRole("heading", { name: "今天要评哪些论文？" })).toBeVisible();
}

async function ask(page, text) {
  const box = page.getByRole("textbox", { name: "输入消息" });
  await box.fill(text);
  await box.press("Enter");
}

test.describe("评分助手", () => {
  test("三栏布局：会话列表、对话、工作区", async ({ page }) => {
    await openAssistant(page);
    await expect(page.getByRole("navigation", { name: "历史会话" })).toBeVisible();
    await expect(page.getByRole("region", { name: "对话" })).toBeVisible();
    await expect(page.getByRole("region", { name: "工作区" })).toContainText("评分的每一步会显示在这里");
  });

  test("按姓名查扣分：代码定位论文，卡片列出扣分与证据，工作区切到评分工作区", async ({ page }) => {
    await openAssistant(page);
    await ask(page, "示例学生乙为什么扣分");

    // 有多个评分任务时先让用户选，不猜。
    await page.getByRole("button", { name: "2026 届毕业论文评分 · 批次 1" }).click();

    const result = page.locator(".acard-paper_result").last();
    await expect(page.locator(".message").last()).toContainText("示例学生乙");
    await expect(result.locator(".item").first()).toBeVisible();

    const frame = page.frameLocator("iframe.frame");
    await expect(frame.getByText("评分表")).toBeVisible();
    // 嵌在工作区里的页面不再显示工作台外壳的侧栏。
    await expect(frame.getByRole("navigation", { name: "主导航" })).toHaveCount(0);
  });

  test("查进度：给出进度卡片，工作区打开现有的评分进度页", async ({ page }) => {
    await openAssistant(page);
    await ask(page, "评分进度");
    await page.getByRole("button", { name: "后台评分验收批次" }).click();

    await expect(page.locator(".acard-job_progress").last()).toContainText("/2");
    const frame = page.frameLocator("iframe.frame");
    await expect(frame.getByRole("heading", { name: "后台评分验收批次" })).toBeVisible();
  });

  test("没有已发布的评分标准时，附件不会建任务，而是引导导入评分标准", async ({ page }) => {
    await openAssistant(page);
    let createdBatch = false;
    page.on("request", (request) => {
      if (request.method() === "POST" && new URL(request.url()).pathname === "/api/batches") createdBatch = true;
    });

    await page.locator(".composer input[type=file]").setInputFiles([
      { name: "a.docx", mimeType: DOCX_TYPE, buffer: Buffer.alloc(2048, 7) },
    ]);

    await expect(page.locator(".message").last()).toContainText("还没有已发布的评分标准");
    await expect(page.getByRole("button", { name: "用我的评分规则和模板" }).last()).toBeVisible();
    expect(createdBatch).toBe(false);
  });

  test("规则识别不了的话：说没理解，并给出快捷按钮", async ({ page }) => {
    await openAssistant(page);
    await ask(page, "今天天气怎么样");
    const reply = page.locator(".message").last();
    await expect(reply).toContainText("没理解");
    await expect(reply.getByRole("button", { name: "开始评分" })).toBeVisible();
  });
});
