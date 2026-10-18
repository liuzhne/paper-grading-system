import { expect, test } from "@playwright/test";

/**
 * 新页顶上、旧页下线（用户决定，2026-09-08）。
 *
 * 下线旧壳最容易被忽略的后果是**已经发出去的链接**：邀请注册与密码重置的
 * 邮件里带的是 `/register?token=...` 和 `/reset-password?token=...`，它们此前
 * 一直由旧壳承接。工作台不接这两条路由就下线旧页，等于把所有在途邀请作废，
 * 而收件人只会看到一个 404，不知道该找谁。
 */
test.describe("默认入口", () => {
  test("根路径是宣传页（用户决定，2026-10-17）", async ({ page }) => {
    const response = await page.goto("/");

    expect(response.status()).toBe(200);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("每一个分数");
    await expect(page.getByRole("navigation", { name: "官网导航" })).toBeVisible();
    // 默认后端没开申请开关（与 Vercel 一样）：首屏不出现试用表单。
    await expect(page.locator('[data-test="trial-form"]')).toHaveCount(0);
    // 默认 e2e 后端关闭鉴权：/auth/me 对谁都是 200，但那不是「已登录」，不能跳走。
    await page.waitForLoadState("networkidle");
    await expect(page).toHaveURL(/\/$/);
  });

  test("旧壳路径不再提供页面", async ({ request }) => {
    const response = await request.get("/legacy/");

    expect(response.status()).toBe(404);
  });
});

test.describe("从宣传页进入工作台", () => {
  test("「直接进入工作台」落到工作台的规范地址，应用内导航走得动", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("link", { name: "直接进入工作台 →" }).click();

    await expect(page).toHaveURL(/\/workbench\/$/);
    // 限定在侧栏导航里：页头的「新建评分任务」也含「评分任务」四个字。
    await page
      .getByRole("navigation", { name: "主导航" })
      .getByRole("link", { name: "评分任务" })
      .click();

    await expect(page).toHaveURL(/\/workbench\/tasks$/);
    await expect(
      page.getByRole("heading", { name: "评分任务", exact: true }),
    ).toBeVisible();
  });

  test("工作台首页直接刷新仍在工作台", async ({ page }) => {
    await page.goto("/workbench/");
    await page.reload();

    await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
  });

  test("「探索更多」滚过转场，露出产品演示", async ({ page }) => {
    // 箭头按原型压在首屏底部的波浪上，而不是视口底部。1280 宽时产品卡片换行到文案
    // 下方（原型如此），首屏比一屏高，箭头在折叠线下；固定定位的元素也没法被自动
    // 滚进视口。用卡片与文案并排、首屏正好一屏的宽屏。
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/");
    await page.getByRole("button", { name: "探索更多" }).click();

    await expect(page.getByRole("heading", { name: "上传评分表，评分项自动成形" })).toBeInViewport();
  });
});

test.describe("减少动态效果", () => {
  test("四步演示平铺成静态画面，不留长滚动舞台", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/");

    const product = page.getByRole("region", { name: "产品演示" });
    await expect(product.locator(".window")).toHaveCount(4);
    // 580vh 的吸顶舞台不再存在：四行画面的总高度远小于它。
    const height = await product.evaluate((el) => el.getBoundingClientRect().height);
    expect(height).toBeLessThan(900 * 5.8);
    for (const title of ["上传评分表，评分项自动成形", "每一项给分，都能回到原文", "拿不准的，交给老师", "复核完，成绩单一次导出"]) {
      await product.getByRole("heading", { name: title }).scrollIntoViewIfNeeded();
      await expect(product.getByRole("heading", { name: title })).toBeInViewport();
    }
  });
});

test.describe("官网子页", () => {
  test("按地址里的 # 打开对应分页，导航切换分页", async ({ page }) => {
    await page.goto("/about#security");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("学生材料只用于评分");
    await expect(page).toHaveTitle("数据安全 · 有据智评");

    await page.getByRole("navigation", { name: "官网导航" }).getByRole("link", { name: "帮助文档" }).click();
    await expect(page).toHaveURL(/\/about#docs$/);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("快速开始");
  });

  test("宣传页的「申请试用」落到院系部署页的预约区", async ({ page }) => {
    await page.goto("/");
    await page.locator('[data-test="hero-trial"]').click();

    await expect(page).toHaveURL(/\/about#contact$/);
    await expect(page.getByRole("heading", { name: "预约部署评估", level: 2 })).toBeInViewport();
    await expect(page.locator("#contact a[href^='mailto:']")).toBeVisible();
  });
});

test.describe("已发出的链接仍然可用", () => {
  test("邀请注册页可达且能读出 token", async ({ page }) => {
    await page.goto("/register?token=demo-invitation-token");

    await expect(page.getByRole("heading", { name: "接受邀请" })).toBeVisible();
    // 邀请无效时也要说清楚，而不是白屏或静默停在一个提交必然失败的表单上。
    await expect(page.getByRole("alert")).toContainText("邀请链接无效或已过期");
  });

  test("密码重置页可达", async ({ page }) => {
    await page.goto("/reset-password?token=demo-reset-token");

    await expect(page.getByRole("heading", { name: "重置密码" })).toBeVisible();
    await expect(page.getByLabel("新密码", { exact: true })).toBeVisible();
  });

  test("重置页两次密码不一致时当场拦下", async ({ page }) => {
    await page.goto("/reset-password?token=demo-reset-token");

    await page.getByLabel("新密码", { exact: true }).fill("Acceptance-1234");
    await page.getByLabel("确认新密码").fill("Acceptance-9999");
    await page.getByRole("button", { name: "重置密码" }).click();

    await expect(page.getByText("两次输入的密码不一致")).toBeVisible();
  });
});
