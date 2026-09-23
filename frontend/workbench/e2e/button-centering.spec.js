import { expect, test } from "@playwright/test";

async function selectSeedRubric(page) {
  await page.getByRole("button", { name: "模板库", exact: true }).click();
  await page.locator(".library-menu .lib-item", { hasText: "本科毕业论文评分标准" }).click();
  await expect(page.getByRole("heading", { name: "本科毕业论文评分标准", exact: true })).toBeVisible();
}
async function openSeedRubric(page) {
  await page.goto("/workbench/rubrics");
  await selectSeedRubric(page);
}

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
  ["/workbench/rubrics", "本科毕业论文评分标准"],
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
    if (path === "/workbench/rubrics") await selectSeedRubric(page);
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
 * 文件卡片采用设计稿的上传按钮与整块拖放区。
 *
 * 真实文件输入覆盖在 label 内，保持原生文件选择能力；透明输入必须仍可通过
 * 键盘到达，且焦点要显示在可见的 label 上。契约验证实际可见卡片与交互，
 * 不再约束浏览器内部 ::file-selector-button 的绘制。
 */
test("导入文件卡片的可见按钮按设计系统渲染并能选择文件", async ({ page }) => {
  await openSeedRubric(page);
  await page.getByRole("button", { name: "导入评分模板" }).click();

  const panel = page.locator(".import-panel");
  const input = panel.getByLabel("评分标准文档", { exact: true });
  const button = panel.locator("label.file-button", { has: page.getByLabel("评分标准文档", { exact: true }) });
  await expect(input).toBeVisible();
  await expect(button).toHaveText("选择文件");
  await expect(panel.getByText("尚未上传评分标准文档")).toBeVisible();
  await expect(panel.getByText("拖入 .xlsx 文件，或点击选择", { exact: true })).toBeVisible();

  const style = await button.evaluate((el) => {
    const visibleButton = getComputedStyle(el);
    return {
      radius: visibleButton.borderTopLeftRadius,
      height: el.getBoundingClientRect().height,
      weight: visibleButton.fontWeight,
      cursor: visibleButton.cursor,
      background: visibleButton.backgroundColor,
    };
  });

  expect(style.radius).not.toBe("0px");
  expect(style.height).toBeGreaterThanOrEqual(30);
  expect(Number(style.weight)).toBeGreaterThanOrEqual(500);
  expect(style.cursor).toBe("pointer");
  expect(style.background).toBe("rgb(255, 255, 255)");
  const chooser = page.waitForEvent("filechooser");
  await button.click();
  expect(await (await chooser).element().getAttribute("aria-label")).toBe("评分标准文档");
});

test("上传输入可用键盘到达与激活，焦点显示在可见文件卡片上", async ({ page }) => {
  await openSeedRubric(page);
  await page.getByRole("button", { name: "导入评分模板" }).click();

  const panel = page.locator(".import-panel");
  for (const name of ["评分标准文档", "评分表"]) {
    const input = panel.getByLabel(name, { exact: true });
    await input.focus();
    await page.keyboard.press("Shift+Tab");
    await expect(input).not.toBeFocused();
    await page.keyboard.press("Tab");
    await expect(input).toBeFocused();
    const focusStyle = await input.evaluate((el) => {
      const label = el.closest("label");
      const style = getComputedStyle(label);
      return { focused: label.matches(":focus-within"), outline: style.outlineStyle, width: Number.parseFloat(style.outlineWidth) };
    });
    expect(focusStyle.focused).toBe(true);
    expect(focusStyle.outline).not.toBe("none");
    expect(focusStyle.width).toBeGreaterThanOrEqual(2);
    const chooser = page.waitForEvent("filechooser");
    await input.press("Enter");
    expect(await (await chooser).element().getAttribute("aria-label")).toBe(name);
  }
});
