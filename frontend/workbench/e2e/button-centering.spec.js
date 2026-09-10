import { expect, test } from "@playwright/test";

/**
 * `.btn` 渲染成 `<a>` 时文字必须居中（2026-09-10 生产走查，第三次出现）。
 *
 * `.btn` 只设了 `height: 36px`，**没有设 `display`**。原生 `<button>` 由 UA 样式
 * 自己居中内容；`RouterLink` 渲染出来的 `<a>` 是行内元素，`height` 直接不生效，
 * 盒子高度由 line-height 决定，文字于是贴在里面某个位置——看起来就是「没居中」。
 *
 * 前两次（弹窗按钮、复核行内动作）都是在单个视图里补 `display: inline-flex`。
 * 补丁修得掉那一处，修不掉下一处：这次是工作台与评分任务的「新建评分任务」。
 * 所以这条契约**按几何量**，不看用什么写法达成，并且扫全部页面。
 */

/** 开发模式后端（AUTH_ENABLED=false）下可直接到达的页面。 */
const PAGES = [
  ["/workbench/", "工作台"],
  ["/workbench/tasks", "评分任务"],
  ["/workbench/review", "结果复核"],
  ["/workbench/rubrics", "评分标准"],
  ["/workbench/exports", "输出中心"],
  ["/workbench/tasks/new", "新建评分任务"],
];

/**
 * 量一个元素里文字的可视中心与元素中心的偏差。
 *
 * 用 `Range` 而不是 `getBoundingClientRect()` 直接读元素：元素的盒子就是我们要
 * 拿来做参照的那个东西，量它自己等于什么都没量。
 */
const MEASURE = (el) => {
  const box = el.getBoundingClientRect();
  const range = document.createRange();
  range.selectNodeContents(el);
  const text = range.getBoundingClientRect();
  if (!text.width || !text.height) return null;
  return {
    label: (el.textContent || "").trim().slice(0, 12),
    dx: text.left + text.width / 2 - (box.left + box.width / 2),
    dy: text.top + text.height / 2 - (box.top + box.height / 2),
  };
};

for (const [path, name] of PAGES) {
  test(`${name}：链接型按钮的文字在两个方向上都居中`, async ({ page }) => {
    await page.goto(path);
    // 等首屏请求落定再数：`h1` 出现只说明壳渲染了，复核页的「查看原文」是表格
    // 数据回来之后才有的。数早了得到空集合，用例会在**什么都没检查**的情况下
    // 跳过——比失败更难发现。
    await expect(page.locator("h1")).toBeVisible();
    await page.waitForLoadState("networkidle");

    const links = page.locator("a.btn");
    const count = await links.count();
    if (count === 0) test.skip(true, `${name} 没有链接型按钮`);

    const offsets = [];
    for (let i = 0; i < count; i += 1) {
      const measured = await links.nth(i).evaluate(MEASURE);
      if (measured) offsets.push(measured);
    }

    expect(offsets.length).toBeGreaterThan(0);
    // 1px 容差：文字的字形盒与视觉重心本就有零点几像素的差。
    const offenders = offsets.filter(
      (item) => Math.abs(item.dx) > 1 || Math.abs(item.dy) > 1,
    );
    expect(offenders).toEqual([]);
  });
}

/**
 * 文件选择框要长得像设计系统的按钮（2026-09-10 生产走查）。
 *
 * 原生 `<input type="file">` 在 Chromium 上渲染成一个灰色系统按钮加一句
 * 「未选择任何文件」。它和同一张表单里的 `.btn` 并排时格外突兀——不同的圆角、
 * 不同的字重、不同的灰。`::file-selector-button` 可以直接改样式，不必藏掉输入框
 * 再用 label 冒充（那样会丢掉键盘可达性，除非另外补一整套焦点处理）。
 */
test("导入表单的文件选择框按设计系统渲染", async ({ page }) => {
  await page.goto("/workbench/rubrics");
  await page.getByRole("button", { name: "导入评分模板" }).click();

  const input = page.locator('.import-panel input[type="file"]').first();
  await expect(input).toBeVisible();

  const style = await input.evaluate((el) => {
    const button = getComputedStyle(el, "::file-selector-button");
    return {
      radius: button.borderTopLeftRadius,
      height: button.height,
      weight: button.fontWeight,
      cursor: button.cursor,
      // 与 `.btn` 的次要样式同源：白底 + 描边，不是系统灰。
      background: button.backgroundColor,
    };
  });

  expect(style.radius).not.toBe("0px");
  expect(style.height).toBe("30px");
  expect(Number(style.weight)).toBeGreaterThanOrEqual(500);
  expect(style.cursor).toBe("pointer");
  expect(style.background).toBe("rgb(255, 255, 255)");
});

test("文件选择框的说明文字与表单其它文字同源，不是系统默认的黑", async ({ page }) => {
  await page.goto("/workbench/rubrics");
  await page.getByRole("button", { name: "导入评分模板" }).click();

  const input = page.locator('.import-panel input[type="file"]').first();
  // 「未选择任何文件」是浏览器画的，改不掉文案，但颜色与字号跟着输入框走。
  const color = await input.evaluate((el) => getComputedStyle(el).color);

  expect(color).not.toBe("rgb(0, 0, 0)");
});
