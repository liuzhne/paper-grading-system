import { expect, test } from "@playwright/test";

/**
 * V01 / V02 / V10 · 登录、组织切换与角色边界（前端 v2 计划 §12.1、§2.1）。
 *
 * 这组跑在 `AUTH_ENABLED=true` 的独立后端上，带两个组织与三种角色。开发模式
 * 会放行一切，在那里跑这些断言全都会**假通过**——那比没有断言更糟。
 *
 * 种子数据是合成的：库建在 tmp 下、进程退出即弃，没有任何真实论文或学生信息。
 */
const PASSWORD = "e2e-Acceptance-1";

async function login(page, username) {
  // 工作台自己的登录页在 /workbench/login；`/login` 归旧 SPA（见
  // test_default_entrypoint：邀请与重置链接已经发出去了，切换入口不能让它们失效）。
  await page.goto("/workbench/login");
  await page.getByLabel("用户名").fill(username);
  await page.getByLabel("密码").fill(PASSWORD);
  await page.getByRole("button", { name: /登录/ }).click();
  // 等到确实进了工作台再返回：点击只是发出请求，抢在会话 cookie 落地之前发起
  // 后续请求会拿到 401，把「无权」的断言变成「没登录」。
  await expect(page.getByRole("heading", { name: "工作台" })).toBeVisible();
}

test.describe("V01 登录与会话", () => {
  test("未登录访问工作台会被引导去登录", async ({ page }) => {
    await page.goto("/workbench/");

    // 停在一个能操作的页面，而不是一个空白的壳。
    await expect(page.getByLabel("用户名")).toBeVisible();
  });

  test("口令错误时给出提示且不进入工作台", async ({ page }) => {
    await page.goto("/workbench/login");
    await page.getByLabel("用户名").fill("teacher");
    await page.getByLabel("密码").fill("wrong-password");
    await page.getByRole("button", { name: /登录/ }).click();

    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page.getByLabel("用户名")).toBeVisible();
  });

  test("登录后进入工作台并显示当前身份", async ({ page }) => {
    await login(page, "teacher");

    await expect(page.getByRole("heading", { name: "工作台" })).toBeVisible();
    await expect(page.getByText("王教师")).toBeVisible();
  });

  test("登出后回到登录页，受保护页面不再可达", async ({ page }) => {
    await login(page, "teacher");
    await expect(page.getByRole("heading", { name: "工作台" })).toBeVisible();

    // 退出在侧边栏账户菜单里，先展开。
    await page.locator("button.account").click();
    await page.getByRole("menuitem", { name: "退出登录" }).click();
    await page.goto("/workbench/review");

    await expect(page.getByLabel("用户名")).toBeVisible();
  });

  test("口令不会出现在页面地址里", async ({ page }) => {
    await login(page, "teacher");

    // 口令进 URL 会被浏览器历史、代理日志和 Referer 带走。
    expect(page.url()).not.toContain(PASSWORD);
  });
});

test.describe("V02 多组织切换", () => {
  test("平台管理员能看到自己所属的两个组织", async ({ page }) => {
    await login(page, "platform");

    // 切换器在侧边栏，成员多于一个组织时才出现。
    const switcher = page.locator(".org-switch select");
    await expect(switcher).toBeVisible();
    await expect(switcher.locator("option")).toHaveCount(2);
    await expect(switcher).toContainText("外国语学院");
  });

  test("切换组织后看不到上一个组织的批次", async ({ page }) => {
    await login(page, "platform");
    await page.goto("/workbench/tasks");
    await expect(
      page.locator("tbody tr", { hasText: "2026 届毕业论文评分" }),
    ).toBeVisible();

    const target = await page
      .locator(".org-switch option", { hasText: "外国语学院" })
      .getAttribute("value");
    await page.locator(".org-switch select").selectOption(target);
    await page.goto("/workbench/tasks");

    // 旧组织的批次留在界面上就是一次跨组织泄露。
    await expect(
      page.locator("tbody tr", { hasText: "2026 届毕业论文评分" }),
    ).toHaveCount(0);
    await expect(
      page.locator("tbody tr", { hasText: "翻译实践报告" }),
    ).toBeVisible();
  });

  test("越界访问另一个组织的批次被服务端拒绝", async ({ page }) => {
    await login(page, "teacher");
    const response = await page.request.get(
      "/api/batches?status=draft",
      { headers: { "X-Organization-ID": "00000000-0000-0000-0000-0000000000b2" } },
    );

    // 教师不属于第二个组织：指名道姓也拿不到它的数据。
    if (response.ok()) {
      const rows = await response.json();
      for (const row of rows) {
        expect(row.name).not.toContain("翻译实践报告");
      }
    } else {
      expect([403, 404]).toContain(response.status());
    }
  });
});

test.describe("V10 角色边界", () => {
  test("教师看不到平台运维视图", async ({ page }) => {
    await login(page, "teacher");
    await page.goto("/workbench/ops");

    // 导航已按能力隐藏这个入口，但深链接与书签仍然到得了——白页读起来像「系统
    // 坏了」，而不是「这不归你看」。
    await expect(page.getByText(/无权查看运维与质量视图/)).toBeVisible();
  });

  test("平台端点对教师返回 403 而不是数据", async ({ page }) => {
    await login(page, "teacher");

    const response = await page.request.get("/api/system/ops-readiness");

    // 必须是 403（已登录但无权），不是 401（没登录）——两者混在一起会让这条
    // 边界在会话失效时假通过。
    expect(response.status()).toBe(403);
  });

  test("平台管理员可以读平台运维视图", async ({ page }) => {
    await login(page, "platform");

    const response = await page.request.get("/api/system/ops-readiness");

    expect(response.status()).toBe(200);
  });

  test("组织管理员管不到平台发布门禁", async ({ page }) => {
    await login(page, "orgadmin");

    const response = await page.request.get("/api/release-gates");

    expect([403, 404]).toContain(response.status());
  });
});
