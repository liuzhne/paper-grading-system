import { expect, test } from "@playwright/test";

/**
 * V03/V04/V05/V06/V07/V11 · 评审动线（前端 v2 计划 §12.1）。
 *
 * 断言集中在**不能造假**的那几条：置信度缺失不显示成 0、引文失配不高亮、
 * 阻塞项不可批量采纳、生成不等于下载、旧日志不补造操作人。
 */

test.describe("V07 评分任务与阶段", () => {
  test("阶段标签与内部编码并列，任务故障单独显示", async ({ page }) => {
    await page.goto("/workbench/tasks");

    const row = page.locator("tbody tr", { hasText: "2026 届毕业论文评分" });
    await expect(row).toBeVisible();
    await expect(row.getByText("已评分 · 含异常项")).toBeVisible();
    // 内部编码对照，便于与后端状态和工单沟通。
    await expect(row.getByText("scored_with_errors")).toBeVisible();
  });

  test("空批次显示「暂无材料」而不是 0%", async ({ page }) => {
    await page.goto("/workbench/tasks");

    const row = page.locator("tbody tr", { hasText: "软件工程导论" });
    await expect(row.getByText("暂无材料")).toBeVisible();
    await expect(row.getByText("0%")).toHaveCount(0);
  });

  test("状态图例列出全部七态", async ({ page }) => {
    await page.goto("/workbench/tasks");

    for (const code of [
      "draft",
      "parsing",
      "scoring",
      "scored",
      "scored_with_errors",
      "reviewed",
      "archived",
    ]) {
      await expect(page.locator(".legend").getByText(code, { exact: true })).toBeVisible();
    }
  });
});

test.describe("V05/V06 复核队列", () => {
  test("阻塞任务排在普通确认之前", async ({ page }) => {
    await page.goto("/workbench/review");

    const first = page.locator("tbody tr").first();
    await expect(first.getByText("阻塞")).toBeVisible();
  });

  test("置信度缺失显示「未提供」而不是 0%", async ({ page }) => {
    await page.goto("/workbench/review");

    await expect(page.getByText("未提供").first()).toBeVisible();
    await expect(page.locator("tbody").getByText("0%")).toHaveCount(0);
  });

  test("阻塞未清空时「完成复核」不可点", async ({ page }) => {
    await page.goto("/workbench/review");

    await expect(page.getByRole("button", { name: "完成复核" })).toBeDisabled();
  });

  test("批量采纳只计入可采纳项", async ({ page }) => {
    await page.goto("/workbench/review");

    const button = page.getByRole("button", { name: /采纳本页可采纳项/ });
    // 阻塞任务与无 AI 分的项都不在其中。
    await expect(button).toContainText("（1）");
  });

  test("采纳后写入复核记录且队列缩短", async ({ page }) => {
    await page.goto("/workbench/review");

    await page.getByRole("button", { name: /采纳本页可采纳项/ }).click();
    await expect(page.getByText(/已采纳 1 项/)).toBeVisible();
    await expect(page.locator(".timeline li")).not.toHaveCount(0);
  });
});

test.describe("V03/V04 评分工作区与证据", () => {
  async function openWorkspace(page) {
    await page.goto("/workbench/tasks");
    await page.locator("tbody tr", { hasText: "2026 届毕业论文评分" }).click();
    await expect(page.getByText("材料 ·")).toBeVisible();
    // 显式选中带证据的那份。工作区默认落在列表首项，而列表按上传时间倒序，
    // 断言依赖那个顺序等于依赖一个与本用例无关的实现细节。
    await page.getByRole("button", { name: /SE-2026-009/ }).click();
    await expect(page.locator(".ident .mono")).toHaveText("SE-2026-009");
    // 标题栏先于相邻关系更新：不等这一步，↑/↓ 会拿着上一份材料的相邻信息跑。
    await expect(page.locator(".nav .mono")).toHaveText("2 / 2");
  }

  test("legacy 正文标注为当前解析结果", async ({ page }) => {
    await openWorkspace(page);

    // 不能让复核者以为自己在核对判分时的冻结快照。
    await expect(page.getByText(/当前解析得到的正文/)).toBeVisible();
  });

  test("点已验证证据后精确高亮引文", async ({ page }) => {
    await openWorkspace(page);

    await page.getByRole("button", { name: /3\.2 研究方法/ }).click();
    const marks = page.locator("mark");
    await expect(marks).toHaveCount(1);
    await expect(marks.first()).toHaveText("本文采用分层抽样方法");
  });

  test("引文与当前文本失配时跳到块但不高亮", async ({ page }) => {
    await openWorkspace(page);

    const chip = page.getByRole("button", { name: /4\.1 实验设计/ });
    await expect(chip).toHaveAttribute("title", /无法精确高亮/);
    await chip.click();
    // 高亮到错误位置比不高亮更糟。
    await expect(page.locator("mark")).toHaveCount(0);
    await expect(page.locator(".block.anchored")).toHaveCount(1);
  });

  test("键盘可切换上一份 / 下一份", async ({ page }) => {
    await openWorkspace(page);

    const before = await page.locator(".ident .mono").textContent();
    await page.keyboard.press("ArrowUp");
    await expect(page.locator(".ident .mono")).not.toHaveText(before);
  });
});

test.describe("V11 导出", () => {
  test("未完成复核挡住成绩单但不挡审计导出", async ({ page }) => {
    await page.goto("/workbench/exports");

    const grades = page.locator(".channel", { hasText: "成绩单" });
    await expect(grades.getByRole("button")).toBeDisabled();
    // 报告与结构化数据仍可导出，只是会标注未完成。
    const report = page.locator(".channel", { hasText: "HTML 评审报告" });
    await expect(report.getByRole("button")).toBeEnabled();
  });

  test("旧日志不补造操作人", async ({ page }) => {
    await page.goto("/workbench/exports");

    await expect(page.getByText("历史记录未记录操作人")).toBeVisible();
    await expect(page.getByText("（旧记录 excel）")).toBeVisible();
  });

  test("导出记录状态是「已生成」而非「已下载」", async ({ page }) => {
    await page.goto("/workbench/exports");

    await page.locator(".channel", { hasText: "HTML 评审报告" }).getByRole("button").click();
    await expect(page.locator("tbody").getByText("已生成").first()).toBeVisible();
    await expect(page.getByText("已下载")).toHaveCount(0);
  });
});

test.describe("V09 评分标准", () => {
  test("没有扣分细则的评分项显示为阻断并说明后果", async ({ page }) => {
    await page.goto("/workbench/rubrics");

    await expect(page.getByText(/存在 1 个阻断项/)).toBeVisible();
    await expect(page.getByText(/评分到该项时没有判据可用/)).toBeVisible();
  });

  test("规则来源分列原文与 AI", async ({ page }) => {
    await page.goto("/workbench/rubrics");

    const row = page.locator("tbody tr", { hasText: "研究方法与技术方案" });
    await expect(row).toContainText("原文");
    await expect(row).toContainText("AI");
    await expect(row).toContainText("待确认");
  });
});

test.describe("V07 归档与分布", () => {
  test("未复核的批次不给归档按钮", async ({ page }) => {
    await page.goto("/workbench/tasks");

    // scored_with_errors 的批次结论还没定，归档会把它冻在一个未完成状态。
    const row = page.locator("tbody tr", { hasText: "2026 届毕业论文评分" });
    await expect(row.getByRole("button", { name: "归档" })).toHaveCount(0);
    await expect(row.getByRole("button", { name: "重新打开" })).toHaveCount(0);
  });

  test("分数分布只画有效终分并标明缺结果", async ({ page }) => {
    await page.goto("/workbench/");

    const card = page.locator(".dist");
    await expect(card).toBeVisible();
    // 分桶未定，界面必须说清楚这一点，否则读者会把原始终分当成已分档结果。
    await expect(card.getByText(/分档口径与跨批次比较尚未确定/)).toBeVisible();
  });

  test("分布不把缺结果的材料算成 0 分", async ({ page }) => {
    await page.goto("/workbench/");

    const card = page.locator(".dist");
    const bars = card.locator(".bar");
    // 种子里两份材料都有终分；缺结果的计数走单独文案，不产生高度为 0 的柱子。
    await expect(bars).toHaveCount(2);
  });
});

test.describe("V07 首版评审方式", () => {
  test("双评与仲裁保持置灰，且说明为什么", async ({ page }) => {
    await page.goto("/workbench/tasks/new");

    // 决策 4：v2 首版只走单评，三选一先置灰。后端没有双评/仲裁的任何实现，
    // 误把它改成可选就是把用户送进一条不存在的路径。
    await expect(page.locator(".mode.active")).toHaveText("单评");
    const disabled = page.locator(".mode.disabled");
    await expect(disabled).toHaveCount(2);
    await expect(disabled.first()).toHaveAttribute("title", /首版未启用/);
    await expect(page.getByText(/首版仅支持单评/)).toBeVisible();
  });
});

test.describe("V08 草稿续传", () => {
  test("草稿批次给出继续上传入口", async ({ page }) => {
    await page.goto("/workbench/tasks");

    const row = page.locator("tbody tr", { hasText: "软件工程导论" });
    await expect(row.getByRole("link", { name: "继续上传" })).toBeVisible();
  });

  test("继续上传回到新建流程并带上批次", async ({ page }) => {
    await page.goto("/workbench/tasks");
    await page.locator("tbody tr", { hasText: "软件工程导论" })
      .getByRole("link", { name: "继续上传" })
      .click();

    await expect(page).toHaveURL(/\/workbench\/tasks\/new\?batch=/);
    await expect(page.getByRole("heading", { name: "新建评分任务" })).toBeVisible();
  });

  test("已评分批次不给续传入口", async ({ page }) => {
    await page.goto("/workbench/tasks");

    // 材料已经在评分/复核里，回到上传流程只会造成混淆。
    const row = page.locator("tbody tr", { hasText: "2026 届毕业论文评分" });
    await expect(row.getByRole("link", { name: "继续上传" })).toHaveCount(0);
  });
});


test.describe("V3-1 评分标准导入", () => {
  test("列表页有导入入口，且没有空白新建", async ({ page }) => {
    await page.goto("/workbench/rubrics");

    await expect(page.getByRole("button", { name: "导入评分模板" })).toBeVisible();
    // D-026：标准只能由导入产生。留一个「新建」按钮会让人建出没有模板溯源的标准。
    await expect(page.getByRole("button", { name: /新建评分标准/ })).toHaveCount(0);
  });

  test("导入面板要求规则 Excel，并说明可见范围", async ({ page }) => {
    await page.goto("/workbench/rubrics");
    await page.getByRole("button", { name: "导入评分模板" }).click();

    await expect(page.getByLabel(/规则 Excel/)).toBeVisible();
    // 用户要知道导进来之后谁能看见。
    await expect(page.getByText(/默认仅自己可见/)).toBeVisible();
  });

  test("Word 模板是可选的，不选也能提交", async ({ page }) => {
    await page.goto("/workbench/rubrics");
    await page.getByRole("button", { name: "导入评分模板" }).click();

    const template = page.getByLabel(/Word 模板/);
    await expect(template).toBeVisible();
    await expect(template).not.toHaveAttribute("required", "");
  });
});


test.describe("V3-4 新建任务入口", () => {
  test("评分任务页有新建入口", async ({ page }) => {
    await page.goto("/workbench/tasks");

    // 页面与路由一直都在，此前全站没有任何链接指向它。
    await page.getByRole("link", { name: "新建评分任务" }).click();

    await expect(page).toHaveURL(/\/workbench\/tasks\/new$/);
    await expect(page.getByRole("heading", { name: "新建评分任务" })).toBeVisible();
  });

  test("工作台也有新建入口", async ({ page }) => {
    await page.goto("/workbench/");

    await expect(page.getByRole("link", { name: "新建评分任务" })).toBeVisible();
  });

  test("平台已配模型时不强制选连接", async ({ page }) => {
    await page.goto("/workbench/tasks/new");

    // 非鉴权后端走环境变量，等同「平台有模型」，不该出现连接必选区。
    await expect(page.getByRole("heading", { name: "AI 连接" })).toHaveCount(0);
  });
});


test.describe("V3-2 AI 起草缺失细则", () => {
  test("有阻断项时给出起草入口，且必须先选连接", async ({ page }) => {
    await page.goto("/workbench/rubrics");

    const button = page.getByRole("button", { name: /生成全部缺失细则/ });
    await expect(button).toBeVisible();
    // 不选连接就起草会走平台默认；平台是 mock 时得到的是编出来的规则，
    // 却以「AI 起草 · 待确认」呈现，确认后进入正式标准（D-027）。
    await expect(button).toBeDisabled();
  });

  test("说明 AI 只补缺失部分，用户原文保留", async ({ page }) => {
    await page.goto("/workbench/rubrics");

    await expect(page.getByText(/AI 只补缺失部分/)).toBeVisible();
  });
});


test.describe("V3-5 工作区写能力", () => {
  async function open(page) {
    await page.goto("/workbench/tasks");
    await page.locator("tbody tr", { hasText: "2026 届毕业论文评分" }).click();
    await expect(page.getByText("材料 ·")).toBeVisible();
  }

  test("每个评分项都能改分", async ({ page }) => {
    await open(page);

    await expect(page.getByRole("button", { name: "改分" }).first()).toBeVisible();
  });

  test("改分理由必填，空理由被拦下", async ({ page }) => {
    await open(page);
    await page.getByRole("button", { name: "改分" }).first().click();
    await page.getByRole("button", { name: "保存" }).click();

    // 改分会写进复核记录；没有理由的记录事后无法判断当初为什么改。
    await expect(page.getByRole("alert")).toContainText("理由");
  });

  test("有确认此份与确认并进入下一份两个动作", async ({ page }) => {
    await open(page);

    await expect(page.getByRole("button", { name: "确认并进入下一份" })).toBeVisible();
    await expect(page.getByRole("button", { name: "确认此份评分" })).toBeVisible();
  });

  test("可以写评语", async ({ page }) => {
    await open(page);

    await expect(page.getByLabel(/评语/)).toBeVisible();
  });
});

test.describe("V3-5 工作区布局", () => {
  test("展开改分框时不被底部总分区盖住", async ({ page }) => {
    await page.goto("/workbench/tasks");
    await page.locator("tbody tr", { hasText: "2026 届毕业论文评分" }).click();
    await expect(page.getByText("材料 ·")).toBeVisible();

    await page.getByRole("button", { name: "改分" }).first().click();

    // 底部总分区是 sticky 的；往里加评语与两个按钮后它变高，会盖住上方展开的
    // 改分框——「保存」按钮点不到，而页面看起来一切正常。
    const overlap = await page.evaluate(() => {
      const box = document.querySelector(".edit-box");
      const total = document.querySelector(".total");
      if (!box || !total) return null;
      const b = box.getBoundingClientRect();
      const t = total.getBoundingClientRect();
      return b.bottom > t.top;
    });

    expect(overlap).toBe(false);
  });

  test("保存按钮可点，没有被遮挡", async ({ page }) => {
    await page.goto("/workbench/tasks");
    await page.locator("tbody tr", { hasText: "2026 届毕业论文评分" }).click();
    await expect(page.getByText("材料 ·")).toBeVisible();
    await page.getByRole("button", { name: "改分" }).first().click();

    // Playwright 的可操作性检查会拒绝点被遮挡的元素——这条比看截图可靠。
    await expect(page.getByRole("button", { name: "保存" })).toBeEnabled();
    await page.getByRole("button", { name: "保存" }).click({ timeout: 5000 });
  });
});

test.describe("全页视觉契约", () => {
  const PAGES = [
    ["/workbench/", "工作台"],
    ["/workbench/tasks", "评分任务"],
    ["/workbench/tasks/new", "新建评分任务"],
    ["/workbench/review", "结果复核"],
    ["/workbench/rubrics", "评分标准"],
    ["/workbench/exports", "输出中心"],
    ["/workbench/account", "账户与连接"],
    ["/workbench/ops", "运维与质量"],
  ];

  for (const [path, title] of PAGES) {
    test(`${title}：表单控件都带设计系统的类`, async ({ page }) => {
      await page.goto(path);
      await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible();

      /*
       * 源码契约查的是模板文本，查不到运行时才出现的控件（v-if 展开的面板、
       * 条件渲染的区块）。这里在真实 DOM 上再查一遍。
       */
      const bare = await page.evaluate(() => {
        const offenders = [];
        for (const el of document.querySelectorAll("input, select, textarea")) {
          if (["radio", "checkbox", "file"].includes(el.type)) continue;
          if (!/\b(input|select)\b/.test(el.className || "")) {
            offenders.push(`${el.tagName}#${el.id || "(no id)"}`);
          }
        }
        return offenders;
      });

      expect(bare).toEqual([]);
    });

    test(`${title}：没有横向溢出`, async ({ page }) => {
      await page.goto(path);
      await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible();

      // 控件宽度失控最典型的表现就是把页面撑出横向滚动条。
      const overflow = await page.evaluate(
        () =>
          document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
      );

      expect(overflow).toBeLessThanOrEqual(1);
    });
  }
});
