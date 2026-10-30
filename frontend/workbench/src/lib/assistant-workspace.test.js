import { describe, expect, it } from "vitest";

import {
  artifactsFrom, clampWorkspace, conversationMeta, entryStep, flowContext, scoreBands, stepEyebrow, stepper, tabLayout,
  taskBar, todoItems, viewStepKey, viewingPast,
} from "./assistant-workspace.js";

const steps = (statuses) => ["import", "publish", "model", "files", "upload", "confirm", "scoring", "report"]
  .map((key, index) => ({ key, label: key, status: statuses[index] || "todo", detail: null }));
const planCard = (statuses, status = "running") => ({ id: "plan-1", type: "plan", status, flow: "import", steps: steps(statuses) });
const msg = (id, role, cards = [], text = "") => ({ id, role, text, cards, created_at: `2026-10-29T02:0${id.length}:00` });

describe("工作区：任务标签来自最近的计划卡片", () => {
  it("流程上下文只看计划卡片之后的流程卡片，问别的任务不会把任务标签带偏", () => {
    const messages = [
      msg("a", "assistant", [{ id: "old", type: "upload_papers", step: "upload", batch_id: "old-batch" }]),
      msg("b", "assistant", [planCard(["done", "done", "done", "done", "current"])]),
      msg("c", "assistant", [{ id: "u", type: "upload_papers", step: "upload", batch_id: "b1", file_count: 2 }]),
      msg("dd", "assistant", [{ id: "q", type: "job_progress", batch_id: "other" }]), // 查询出来的，不带 step
    ];
    const context = flowContext(messages);
    expect(context.plan.id).toBe("plan-1");
    expect(context.batchId).toBe("b1");
    expect(context.cards.upload_papers.id).toBe("u");
  });

  it("旧会话里的流程卡片没有 step，按类型推断；查询卡片不是入口", () => {
    expect(entryStep({ type: "confirm_start" })).toBe("confirm");
    expect(entryStep({ type: "rubric_wait" })).toBe("publish");
    expect(entryStep({ type: "job_progress", step: "scoring" })).toBe("scoring");
    expect(entryStep({ type: "job_progress" })).toBeNull();
  });

  it("看哪一步：点开的能点开才算，否则跟着当前步骤；看的不是当前那一步时提示回到当前", () => {
    const plan = planCard(["done", "done", "done", "current"]);
    expect(viewStepKey(plan, null)).toBe("files");
    expect(viewStepKey(plan, "publish")).toBe("publish");
    expect(viewStepKey(plan, "report")).toBe("files"); // 还没到的不能点开
    expect(viewingPast(plan, "publish")).toBe(true);
    // 选文件、上传预检、确认开始共用一个视图：不算“看已完成的步骤”。
    expect(viewingPast(planCard(["done", "done", "done", "done", "current"]), "files")).toBe(false);
    expect(viewStepKey(planCard(Array(8).fill("done"), "done"), null)).toBe("report");
  });

  it("步骤条：宽度不够时只给正在看的那一步显示名字", () => {
    const items = stepper(planCard(["done", "current"]).steps, "publish", false);
    expect(items.map((item) => item.showLabel)).toEqual([false, true, false, false, false, false, false, false]);
    expect(items[1]).toMatchObject({ num: "02", short: "核对发布", wide: true, reachable: true });
    expect(items[2].reachable).toBe(false);
  });

  it("视图上方：等老师的当前步骤写“待你处理”，后台进行的写“进行中”", () => {
    const plan = planCard(["done", "current"]);
    expect(stepEyebrow(plan, "publish", "wait_publish")).toEqual({ text: "步骤 2 / 8 · 待你处理", tone: "warn" });
    expect(stepEyebrow(plan, "import", null)).toEqual({ text: "步骤 1 / 8 · 已完成", tone: "accent" });
    expect(stepEyebrow(planCard(["done", "done", "done", "done", "current"]), "upload", "upload_papers").text).toBe("步骤 5 / 8 · 进行中");
  });
});

describe("工作区：结果标签", () => {
  it("结果卡片开成标签，最新的在前；解释类用提问那句话做标题；流程里的结果卡片不另开标签", () => {
    const list = artifactsFrom([
      msg("u1", "user", [], "评分进度"),
      msg("a1", "assistant", [{ id: "j1", type: "job_progress", batch_id: "b1" }]),
      msg("u2", "user", [], "第 8 篇为什么只有 64 分？"),
      msg("a2", "assistant", [{ id: "p1", type: "paper_result", batch_id: "b1", paper_id: "p", run_id: "r" }]),
      msg("a3", "assistant", [{ id: "o1", type: "overview", batch_id: "b1", step: "report" }]),
    ]);
    expect(list.map((item) => [item.id, item.kind, item.title])).toEqual([
      ["p1", "explain", "第 8 篇为什么只有 64 分？"], ["j1", "progress", "评分进度"],
    ]);
  });

  it("标签放不下收进“更多”，正在看的那个总在外面", () => {
    // 默认宽度 560 放 2 个，870 放 4 个（设计稿）。
    expect(tabLayout(["task", "a"], "task", 560)).toEqual({ visible: ["task", "a"], more: [] });
    expect(tabLayout(["task", "a", "b", "c"], "c", 560)).toEqual({ visible: ["task", "c"], more: ["a", "b"] });
    expect(tabLayout(["task", "a", "b", "c", "d"], "task", 870)).toEqual({ visible: ["task", "a", "b", "c"], more: ["d"] });
  });

  it("工作区宽度在 400 到“总宽 − 400”之间，没存过是 560", () => {
    expect(clampWorkspace(null, 1400)).toBe(560);
    expect(clampWorkspace(1200, 1400)).toBe(1000);
    expect(clampWorkspace(200, 1400)).toBe(400);
    expect(clampWorkspace(900, 600)).toBe(400);
  });
});

describe("输入区的任务条与待办", () => {
  it("等老师时“待你处理”带主动作；工作区已经显示这一步时只提示", () => {
    const plan = planCard(["done", "done", "done", "done", "done", "current"]);
    const confirm = { ready_count: 11, file_count: 12, removable: [{ paper_id: "x" }], warnings: [] };
    expect(taskBar(plan, { kind: "confirm_start" }, { confirm })).toMatchObject({
      tone: "wait", text: "确认开始评分 · 11 份，1 份无法评分", action: { kind: "confirm", label: "开始评分" },
    });
    expect(taskBar(plan, { kind: "confirm_start" }, { confirm, visibleStep: "upload" })).toMatchObject({ action: null, note: "已在右侧打开" });
  });

  it("后台进行时“进行中”带进度；没有进行中的计划不显示", () => {
    const plan = planCard(["done", "done", "done", "done", "done", "done", "current"]);
    const job = { total_items: 12, succeeded_count: 5, skipped_count: 0, failed_count: 1, canceled_count: 0 };
    expect(taskBar(plan, { kind: "wait_job" }, { job })).toMatchObject({ tone: "run", text: "正在评分 6/12", pct: 50 });
    expect(taskBar(planCard(Array(8).fill("done"), "done"), null)).toBeNull();
  });

  it("待办只列老师要做的，排在最前、现在能做的那一项才有按钮；模型就绪时不列", () => {
    const plan = planCard(["done", "done", "done", "current"]);
    const items = todoItems(plan, { kind: "pick_files" });
    expect(items.map((item) => item.key)).toEqual(["import", "publish", "files", "confirm", "report"]);
    expect(items.find((item) => item.isNext)).toMatchObject({ key: "files", action: { kind: "pick" } });
    expect(items.filter((item) => item.action)).toHaveLength(1);
    const missing = todoItems(planCard(["done", "done", "blocked"]), null);
    expect(missing.find((item) => item.isNext)).toMatchObject({ key: "model", action: { kind: "link", path: "/account" } });
  });
});

describe("会话列表与汇报", () => {
  it("会话说明：进行中写第几步，结束写状态，再接时间", () => {
    const now = new Date(2026, 9, 29, 18, 0);
    const today = new Date(2026, 9, 29, 10, 21).toISOString();
    expect(conversationMeta({ updated_at: today, focus: { progress: { status: "running", step: 2, total: 8 } } }, now))
      .toBe("步骤 2/8 · 今天 10:21");
    expect(conversationMeta({ updated_at: new Date(2026, 9, 24, 9, 0).toISOString(), focus: { progress: { status: "done", step: 8, total: 8 } } }, now))
      .toBe("已完成 · 10月24日");
    expect(conversationMeta({ updated_at: today, focus: {} }, now)).toBe("今天 10:21");
  });

  it("分数分布五档，条长按最多的那档", () => {
    const bands = scoreBands([91, 86, 88, 72, 64, null]);
    expect(bands.map((band) => [band.label, band.n, band.pct])).toEqual([
      ["90–100", 1, 50], ["80–89", 2, 100], ["70–79", 1, 50], ["60–69", 1, 50], ["< 60", 0, 0],
    ]);
  });
});
