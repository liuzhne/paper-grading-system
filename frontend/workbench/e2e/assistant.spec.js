import { expect, test } from "@playwright/test";

/**
 * 评分助手（评分助手 v2 设计稿，docs/评分助手v2界面改造方案.md）：会话栏 · 对话 · 原生工作区。
 *
 * 验收后端没有评分 worker、也没有已发布的评分标准，这里不跑“评完再汇报”——那段由后端测试与前端单测
 * （stores/assistant.test.js、components/assistant/*.test.js）覆盖。这里证明的是真实托管下：
 * 意图识别走规则、论文由代码定位、结果开成工作区标签且分数与证据从接口现取、流程步骤条与任务条按计划卡片画。
 */
const DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

async function openAssistant(page) {
  await page.goto("/workbench/assistant");
  await expect(page.getByRole("heading", { name: "有什么要处理的？" })).toBeVisible();
}

async function ask(page, text) {
  const box = page.getByRole("textbox", { name: "输入消息" });
  await box.fill(text);
  await box.press("Enter");
}

const workspace = (page) => page.getByRole("region", { name: "工作区" });

test.describe("评分助手", () => {
  test("三栏布局：会话栏、对话、工作区；工作区可收起再打开", async ({ page }) => {
    await openAssistant(page);
    await expect(page.getByRole("navigation", { name: "历史会话" })).toBeVisible();
    await expect(page.getByRole("region", { name: "对话" })).toBeVisible();
    // 新对话还没有结果：工作区先收着，按钮上写“无结果”。
    await expect(workspace(page)).toHaveCount(0);
    await page.getByRole("button", { name: "打开工作区 无结果" }).click();
    await expect(workspace(page)).toContainText("这次对话还没有结果");
    await workspace(page).getByRole("button", { name: "收起" }).click();
    await expect(workspace(page)).toHaveCount(0);
  });

  test("按姓名查扣分：代码定位论文，结果开成“解释”标签，分数与扣分从接口现取", async ({ page }) => {
    await openAssistant(page);
    await ask(page, "示例学生乙为什么扣分");

    // 有多个评分任务时先让用户选，不猜。
    await page.getByRole("button", { name: "2026 届毕业论文评分 · 批次 1" }).click();

    await expect(page.locator(".message").last()).toContainText("示例学生乙");
    const tab = workspace(page).getByRole("tab", { name: /示例学生乙为什么扣分/ });
    await expect(tab).toHaveAttribute("aria-selected", "true");
    await expect(workspace(page).locator(".score-row").first()).toBeVisible();
    // 对话里只留入口，指向右侧正在显示的结果。
    await expect(page.getByRole("region", { name: "对话" }).getByRole("button", { name: /正在右侧显示/ })).toBeVisible();
  });

  test("查进度：进度开成标签，按份列出", async ({ page }) => {
    await openAssistant(page);
    await ask(page, "评分进度");
    await page.getByRole("button", { name: "后台评分验收批次" }).click();

    await expect(workspace(page).getByRole("tab", { name: /评分进度/ })).toHaveAttribute("aria-selected", "true");
    await expect(workspace(page)).toContainText("/ 2 份已完成");
    await expect(workspace(page).locator(".row-list .row")).toHaveCount(2);
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

  test("导入评分标准：8 步步骤条画在工作区顶部，导入表单在第 1 步，任务条提示已在右侧打开", async ({ page }) => {
    await openAssistant(page);
    await ask(page, "上传我的评分规则和模板");

    const steps = workspace(page).locator(".stepper .step");
    await expect(steps).toHaveCount(8);
    await expect(steps.first()).toHaveAttribute("aria-current", "step");
    await expect(workspace(page)).toContainText("步骤 1 / 8 · 待你处理");
    await expect(workspace(page).getByLabel("评分标准名称")).toBeVisible();
    await expect(page.locator('[data-test="task-bar"]')).toContainText("上传评分规则和模板");
    await expect(page.locator('[data-test="task-bar"]')).toContainText("已在右侧打开");
    // 会话栏这一条写着走到第几步。
    await expect(page.getByRole("navigation", { name: "历史会话" })).toContainText("步骤 1/8");
  });

  test("打断保护：导入还没完成时说“开始评分”，先问要不要放弃，选继续则导入表单原样等着", async ({ page }) => {
    await openAssistant(page);
    await ask(page, "上传我的评分规则和模板");
    await expect(workspace(page).getByLabel("评分标准名称")).toBeVisible();

    // 问“怎么做”不会开始别的流程（10-15 的生产问题）。
    await ask(page, "模板怎么上传？");
    await expect(page.locator(".message").last()).toContainText("当前还有一步没完成：上传评分规则与模板");

    await ask(page, "开始评分");
    const switchCard = page.locator(".acard-confirm_switch_flow").last();
    await expect(switchCard.getByRole("button", { name: "放弃并开始评分" })).toBeVisible();
    await switchCard.getByRole("button", { name: "继续“上传评分规则与模板”" }).click();

    await expect(switchCard).toContainText("已继续上一步");
    await expect(workspace(page).getByLabel("评分标准名称")).toBeVisible();
  });

  test("规则识别不了的话：说没理解，并给出快捷按钮", async ({ page }) => {
    await openAssistant(page);
    await ask(page, "今天天气怎么样");
    const reply = page.locator(".message").last();
    await expect(reply).toContainText("没理解");
    await expect(reply.getByRole("button", { name: "开始评分" })).toBeVisible();
  });
});
