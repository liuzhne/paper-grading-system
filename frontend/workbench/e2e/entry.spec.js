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
  test("根路径直接是工作台，不再回落旧页", async ({ page }) => {
    const response = await page.goto("/");

    expect(response.status()).toBe(200);
    await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
  });

  test("旧壳路径不再提供页面", async ({ request }) => {
    const response = await request.get("/legacy/");

    expect(response.status()).toBe(404);
  });
});

test.describe("根路径进入后的应用内导航", () => {
  test("从 / 点导航会落到工作台的规范地址", async ({ page }) => {
    await page.goto("/");

    // 路由的 history base 是 /workbench/，而入口页同时挂在 /。两个地址空间
    // 并存时最容易出问题的是「进得去、走不动」：首屏渲染正常，一点导航就
    // 匹配不上路由。
    await page.getByRole("link", { name: "评分任务" }).click();

    await expect(page).toHaveURL(/\/workbench\/tasks$/);
    await expect(page.getByRole("heading", { name: "评分任务" })).toBeVisible();
  });

  test("从 / 直接刷新仍在工作台", async ({ page }) => {
    await page.goto("/");
    await page.reload();

    await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
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
