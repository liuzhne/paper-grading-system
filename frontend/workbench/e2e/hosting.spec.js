import { expect, test } from "@playwright/test";

/**
 * V12 · 托管与路由（前端 v2 计划 §12.1）。
 *
 * 这几条只在**生产托管路径**上才会出问题，Vite dev server 测不出来：它有
 * 自己的中间件与模块图，什么都能回落成 index.html。
 */
test.describe("V12 托管与路由", () => {
  test("深链接刷新返回 HTML 而不是 404", async ({ page }) => {
    const response = await page.goto("/workbench/review");

    expect(response.status()).toBe(200);
    expect(response.headers()["content-type"]).toContain("text/html");
    await expect(page.getByRole("heading", { name: "结果复核" })).toBeVisible();
  });

  test("缺失资源返回 404，绝不回落成 HTML", async ({ request }) => {
    // 回落成 HTML 时浏览器会把它当 JS 执行，故障表现为难以定位的语法错误。
    const response = await request.get("/workbench/assets/does-not-exist.js");

    expect(response.status()).toBe(404);
    expect(response.headers()["content-type"] || "").not.toContain("text/html");
  });

  test("/api 不被 SPA 回退吞掉", async ({ request }) => {
    const response = await request.get("/api/system/integrations");

    expect(response.status()).toBe(200);
    expect(response.headers()["content-type"]).toContain("application/json");
  });

  test("根路径是宣传页，/about 是官网子页", async ({ page }) => {
    // 旧 SPA 已下线（2026-09-08）；根路径 2026-10-17 起是宣传页（用户决定）。
    const response = await page.goto("/");
    expect(response.status()).toBe(200);
    await expect(page.getByRole("navigation", { name: "官网导航" })).toBeVisible();

    for (const path of ["/about", "/about/"]) {
      const about = await page.goto(path);
      expect(about.status(), path).toBe(200);
      await expect(page.getByRole("heading", { level: 1 })).toContainText("部署在学校自己的服务器上");
    }
  });

  test("未知路径显示 404 页而不是空白", async ({ page }) => {
    await page.goto("/workbench/no-such-page");

    await expect(page.getByRole("heading", { name: "页面不存在" })).toBeVisible();
  });
});
