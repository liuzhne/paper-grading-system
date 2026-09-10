import { expect, test } from "@playwright/test";

/**
 * 没有可用模型时把用户引导去配置 BYOK（D-027、D-028；用户决定 2026-09-09）。
 *
 * 单独一个后端，种子里**不配平台模型**——主鉴权后端配了（否则 V01/V02/V10 全挂
 * 在第一步），验不了这一条。
 */
const PASSWORD = "e2e-Acceptance-1";

async function login(page, username) {
  await page.goto("/login");
  await page.getByLabel("用户名").fill(username);
  await page.getByLabel("密码", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "登录" }).click();
  // 等**离开登录页**再返回。只等 `/workbench/` 会匹配到登录页自身的地址，
  // 于是在会话还没建立时就放行，后续 goto 全被守卫弹回登录页。
  // 平台管理员同样没有可用模型，也会被引导到账户页——这里等的是「不在登录页」。
  await expect(page.getByRole("button", { name: "登录" })).toHaveCount(0);
}

test.describe("未配置模型时的引导", () => {
  test("登录后弹窗说清原因，而不是把人默默送走", async ({ page }) => {
    await login(page, "teacher");

    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("请先配置 API Key");
  });

  test("按钮文字居中", async ({ page }) => {
    await login(page, "teacher");
    const link = page.getByRole("alertdialog").getByRole("link").first();

    /*
     * `.btn` 没有设 display：原生 `<button>` 自己居中，而 RouterLink 渲染成 `<a>`
     * 是行内元素，文字会左对齐。
     */
    const centered = await link.evaluate((el) => {
      const cs = getComputedStyle(el);
      return cs.justifyContent === "center" && cs.display.includes("flex");
    });

    expect(centered).toBe(true);
  });

  test("有关闭按钮，点了就收起", async ({ page }) => {
    await login(page, "teacher");
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();

    await page.getByRole("button", { name: "关闭" }).click();

    await expect(dialog).toHaveCount(0);
  });

  test("点操作链接后弹窗收起，目的地可以直接操作", async ({ page }) => {
    await login(page, "teacher");

    await page.getByRole("alertdialog").getByRole("link", { name: /账户与连接/ }).click();

    await expect(page).toHaveURL(/\/workbench\/account(#ai-connections)?$/);
    // 弹窗必须收起：留着的话遮罩铺满视口，用户点得到链接却填不了表。
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
  });

  test("平台管理员点进运维页后能真正填表", async ({ page }) => {
    await login(page, "platform");

    const dialog = page.getByRole("alertdialog");
    await expect(dialog.getByRole("link", { name: /运维与质量/ })).toBeVisible();
    await dialog.getByRole("link", { name: /运维与质量/ }).click();

    await expect(page).toHaveURL(/\/workbench\/ops(#platform-llm)?$/);
    /*
     * **不是断言「可见」**：遮罩层 `inset: 0` 之下表单照样 visible，但点不到。
     * 直接填一次——Playwright 的可操作性检查会因为遮挡而失败。
     */
    await page.getByLabel("Base URL").fill("https://api.example.com/v1");
    await expect(page.getByLabel("Base URL")).toHaveValue("https://api.example.com/v1");
  });
});


test.describe("关掉弹窗不等于可以用系统", () => {
  test("关掉后直接访问功能页仍被送回配置页", async ({ page }) => {
    await login(page, "teacher");
    await page.getByRole("button", { name: "关闭" }).click();
    await expect(page.getByRole("alertdialog")).toHaveCount(0);

    await page.goto("/workbench/tasks");

    // 上一版把拦截交给了弹窗，关掉后全站畅通——用户能进去，但页面上的动作在
    // 后端一律失败，看到的是「能进去，但什么都做不成」。
    await expect(page).toHaveURL(/\/workbench\/account(#ai-connections)?$/);
  });

  test("关掉后工作台也进不去", async ({ page }) => {
    await login(page, "teacher");
    await page.getByRole("button", { name: "关闭" }).click();

    await page.goto("/workbench/");

    await expect(page).toHaveURL(/\/workbench\/account(#ai-connections)?$/);
  });

  test("配置页始终可达，且能真正填表", async ({ page }) => {
    await login(page, "platform");
    await page.getByRole("button", { name: "关闭" }).click();

    await page.goto("/workbench/ops");

    await expect(page).toHaveURL(/\/workbench\/ops(#platform-llm)?$/);
    // 不是断言可见：遮罩之下表单照样 visible。直接填一次。
    await page.getByLabel("Base URL").fill("https://api.example.com/v1");
    await expect(page.getByLabel("Base URL")).toHaveValue("https://api.example.com/v1");
  });

  test("平台管理员被送到运维页，不是账户页", async ({ page }) => {
    await login(page, "platform");
    await page.getByRole("button", { name: "关闭" }).click();

    await page.goto("/workbench/tasks");

    // 配平台默认模型能让所有人都能用，优先送他去那里。
    await expect(page).toHaveURL(/\/workbench\/ops(#platform-llm)?$/);
  });
});

test.describe("拦截按实时状态，不按「是否已弹过一次」", () => {
  test("关掉弹窗后再点导航，弹窗重新出现", async ({ page }) => {
    await login(page, "teacher");
    await page.getByRole("button", { name: "关闭" }).click();
    await expect(page.getByRole("alertdialog")).toHaveCount(0);

    // 用户没配置就想去别处。上一版这里是**静默弹回配置页**：拦是拦住了，但屏幕上
    // 没有任何解释，用户不知道自己为什么点不动。
    await page.goto("/workbench/tasks");

    await expect(page.getByRole("alertdialog")).toBeVisible();
    await expect(page).toHaveURL(/\/workbench\/account(#ai-connections)?$/);
  });

  test("反复关掉、反复点，每一次都要重新提示", async ({ page }) => {
    await login(page, "teacher");

    for (const target of ["/workbench/tasks", "/workbench/exports", "/workbench/review"]) {
      await page.getByRole("button", { name: "关闭" }).click();
      await expect(page.getByRole("alertdialog")).toHaveCount(0);
      await page.goto(target);
      await expect(page.getByRole("alertdialog")).toBeVisible();
    }
  });

  test("弹窗说出用户本来想去哪一页", async ({ page }) => {
    await login(page, "teacher");
    await page.getByRole("button", { name: "关闭" }).click();
    await page.goto("/workbench/exports");

    await expect(page.getByRole("alertdialog")).toContainText("输出中心");
  });

  test("点确认后跳到配置页，并定位到对应配置项", async ({ page }) => {
    await login(page, "teacher");
    const link = page.getByRole("alertdialog").getByRole("link").first();
    await link.click();

    await expect(page).toHaveURL(/\/workbench\/account/);
    // 光跳到页顶不够：账户页还有成员、邀请、重置等区块，用户得自己找。
    const target = page.locator("#ai-connections");
    await expect(target).toBeVisible();
    await expect(target).toHaveClass(/highlight/);
  });

  test("主动走到配置页不弹窗——没被拦就不该被挡住填表", async ({ page }) => {
    await login(page, "teacher");
    await page.getByRole("button", { name: "关闭" }).click();

    await page.goto("/workbench/account");

    await expect(page.getByRole("alertdialog")).toHaveCount(0);
  });

  test("平台管理员配好平台模型后，拦截立即解除，无需刷新", async ({ page }) => {
    await login(page, "platform");
    await page.getByRole("button", { name: "关闭" }).click();
    await page.goto("/workbench/ops");

    await page.getByLabel("Base URL").fill("https://api.example.com/v1");
    await page.getByLabel("模型名", { exact: true }).fill("acceptance-model");
    await page.getByLabel("API Key").fill("sk-acceptance-placeholder");
    await page.getByRole("button", { name: "保存并启用" }).click();
    await expect(page.getByText(/已保存|已启用/).first()).toBeVisible();

    // 不 reload：能力表在保存后就该刷新，否则用户配好了还是进不去。
    await page.getByRole("link", { name: "评分任务" }).click();

    await expect(page).toHaveURL(/\/workbench\/tasks$/);
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
  });
});
