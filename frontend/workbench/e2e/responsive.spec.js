import { expect, test } from "@playwright/test";

/**
 * V12 · 窄屏与可达性（前端 v2 计划 §8、§12.1）。
 *
 * 设计稿的三栏固定宽度不是唯一布局：窄屏必须折叠成可用形态，而不是横向
 * 滚动出一个够不着的评分表。
 */
test.describe("V12 窄屏", () => {
  test("工作台在窄屏下不产生横向滚动", async ({ page }) => {
    await page.goto("/workbench/");
    await expect(page.getByRole("heading", { name: "工作台" })).toBeVisible();

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);
  });

  test("评分工作区在窄屏折叠为单列", async ({ page }) => {
    await page.goto("/workbench/tasks");
    await page.locator("tbody tr", { hasText: "2026 届毕业论文评分" }).click();
    // 先等工作区真的挂上：evaluate 不重试，抢在渲染前跑只会拿到 null，报成
    // getComputedStyle 的类型错误，掩盖「到底有没有折叠」这个真正的问题。
    await expect(page.locator(".cols")).toBeVisible();

    const columns = await page.evaluate(() => {
      const cols = document.querySelector(".cols");
      return getComputedStyle(cols).gridTemplateColumns.split(" ").length;
    });
    expect(columns).toBe(1);
  });
});

test.describe("V12 键盘可达性", () => {
  test("导航链接可被键盘聚焦", async ({ page }) => {
    await page.goto("/workbench/");
    // 同理：SPA 挂载前页面里没有可聚焦元素，Tab 停在 body，测出来的是加载
    // 时序而不是可达性。
    await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
    await page.keyboard.press("Tab");

    const tag = await page.evaluate(() => document.activeElement?.tagName);
    expect(["A", "BUTTON", "SELECT", "INPUT"]).toContain(tag);
  });
});

test.describe("V12 窄屏表单", () => {
  const FORM_PAGES = [
    ["/workbench/rubrics", "评分标准"],
    ["/workbench/ops", "运维与质量"],
    ["/workbench/account", "账户与连接"],
    ["/workbench/tasks/new", "新建评分任务"],
  ];

  for (const [path, title] of FORM_PAGES) {
    test(`${title}：窄屏下表单不撑破页面`, async ({ page }) => {
      await page.goto(path);
      await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible();

      /*
       * `.input` / `.select` 是 `width: 100%`，父容器一旦有固定宽度或并排布局
       * 没折叠，就会把页面撑出横向滚动条——窄屏上表现为整页能左右拖动，输入框
       * 一半在屏幕外。
       */
      const overflow = await page.evaluate(
        () =>
          document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
      );

      expect(overflow).toBeLessThanOrEqual(1);
    });

    test(`${title}：窄屏下并排字段折叠为单列`, async ({ page }) => {
      await page.goto(path);
      await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible();

      // `.form-grid` 用的是 auto-fit + minmax(210px)，窄屏应当自然落到一列。
      const columns = await page.evaluate(() => {
        const grid = document.querySelector(".form-grid");
        if (!grid) return 1;
        return getComputedStyle(grid).gridTemplateColumns.split(" ").length;
      });

      expect(columns).toBe(1);
    });
  }
});
