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

    // 此前是直接跳到账户页 + 一条横幅：横幅容易被忽略，而且用户已经被送到一个
    // 自己没主动去的页面，不知道发生了什么。
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("尚未配置可用模型");
  });

  test("弹窗不可关闭：没有模型时整套能力都用不了", async ({ page }) => {
    await login(page, "teacher");
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();

    // 给一个「关闭」等于放人进去撞一连串失败。
    await expect(dialog.getByRole("button", { name: /关闭|稍后|知道了/ })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(dialog).toBeVisible();
  });

  test("普通用户被指向账户与连接", async ({ page }) => {
    await login(page, "teacher");

    const link = page.getByRole("alertdialog").getByRole("link", {
      name: /账户与连接/,
    });
    await expect(link).toBeVisible();
    await link.click();

    await expect(page).toHaveURL(/\/workbench\/account$/);
    await expect(page.getByRole("heading", { name: "账户与连接" })).toBeVisible();
  });

  test("平台管理员两条出路都给，且能进运维页配置", async ({ page }) => {
    await login(page, "platform");

    const dialog = page.getByRole("alertdialog");
    await expect(dialog.getByRole("link", { name: /运维与质量/ })).toBeVisible();
    await expect(dialog.getByRole("link", { name: /账户与连接/ })).toBeVisible();

    await dialog.getByRole("link", { name: /运维与质量/ }).click();

    await expect(page).toHaveURL(/\/workbench\/ops$/);
    await expect(page.getByRole("heading", { name: "平台默认模型" })).toBeVisible();
  });

  test("配置页本身可达，弹窗不挡住配置动作", async ({ page }) => {
    await login(page, "platform");
    await page.getByRole("alertdialog").getByRole("link", { name: /运维与质量/ }).click();

    // 弹窗仍在（还没配好），但不能盖住表单——否则用户点得到链接却填不了表。
    await expect(page.getByLabel("Base URL")).toBeVisible();
  });
});
