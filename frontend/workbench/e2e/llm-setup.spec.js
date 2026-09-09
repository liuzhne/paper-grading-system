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
  test("登录后被送到账户与连接页并说明原因", async ({ page }) => {
    await login(page, "teacher");

    // 与其让人上传完材料再撞上失败，不如进门就说清该去哪。
    await expect(page).toHaveURL(/\/account\?setup=model/);
    await expect(page.getByRole("status")).toContainText("尚未配置可用模型");
  });

  test("账户与连接页本身可达，不会把人锁在门外", async ({ page }) => {
    await login(page, "teacher");
    await page.goto("/workbench/account");
    await expect(page.getByRole("heading", { name: "账户与连接" })).toBeVisible();
  });

  test("平台管理员仍能进运维页配置平台模型", async ({ page }) => {
    await login(page, "platform");
    await page.goto("/workbench/ops");

    // 平台模型正是在这一页配的，拦住它等于没有出路。
    await expect(page.getByRole("heading", { name: "平台默认模型" })).toBeVisible();
    await expect(page.getByText("未配置", { exact: true })).toBeVisible();
  });

  test("给平台管理员的提示要指出两条出路", async ({ page }) => {
    await login(page, "platform");
    await page.goto("/workbench/?setup=model");

    await page.goto("/workbench/account?setup=model");
    await expect(page.getByRole("status")).toContainText("运维与质量");
  });
});
