import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createMemoryHistory, createRouter } from "vue-router";

import AssistantCard from "./AssistantCard.vue";

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubItems(items) {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (url) => {
    const path = String(url).replace("/api", "").split("?")[0];
    if (path === "/scoring-runs/r1/items") {
      return new Response(JSON.stringify(items), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return new Response("{}", { status: 404 });
  }));
}

it("“得分详情”卡片显示扣分理由与修改建议（方案 §31.3）", async () => {
  stubItems([
    { id: "i1", criterion_name: "研究方法", max_score: 20, final_score: 14, need_manual_review: false,
      deduction_items: [{ reason: "未说明研究方法", points: 6 }], deductions: [], reason: "方法部分偏简略。",
      revision_suggestion: "补充研究方法的步骤与依据。", evidence_view: [] },
    { id: "i2", criterion_name: "文献综述", max_score: 20, final_score: 18, need_manual_review: false,
      deduction_items: [], deductions: [], reason: "综述基本完整。", revision_suggestion: null, evidence_view: [] },
  ]);
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:any(.*)", component: { template: "<div />" } }] });
  const wrapper = mount(AssistantCard, {
    props: { message: { id: "m1" }, card: { type: "paper_result", batch_id: "b1", paper_id: "p1", run_id: "r1" } },
    global: { plugins: [router] },
  });
  await flushPromises();

  const [method, review] = wrapper.findAll(".item");
  expect(method.get(".deductions").text()).toContain("未说明研究方法");
  expect(method.get('[data-test="card-suggestion"]').text()).toBe("修改建议：补充研究方法的步骤与依据。");
  expect(review.text()).toContain("综述基本完整。");
  expect(review.find('[data-test="card-suggestion"]').exists()).toBe(false);
  wrapper.unmount();
});

it("计划卡片按顺序列出步骤，带状态记号与说明（计划卡片方案 §4）", () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:any(.*)", component: { template: "<div />" } }] });
  const card = {
    id: "plan-1", type: "plan", status: "running", flow: "grading",
    steps: [
      { key: "rubric", label: "评分标准", status: "done", detail: "《毕业论文》v1" },
      { key: "files", label: "选择待评分文件", status: "current", detail: null },
      { key: "upload", label: "上传与预检", status: "blocked", detail: "1 份无法评分，等你决定是否移除" },
      { key: "report", label: "汇报结果", status: "todo", detail: null },
    ],
  };
  const wrapper = mount(AssistantCard, { props: { message: { id: "m1" }, card }, global: { plugins: [router] } });

  expect(wrapper.get(".plan-head").text()).toContain("评分的步骤");
  expect(wrapper.get(".plan-head").text()).toContain("进行中");
  const steps = wrapper.findAll(".plan-steps li");
  expect(steps.map((step) => step.classes().find((name) => name.startsWith("step-")))).toEqual(
    ["step-done", "step-current", "step-blocked", "step-todo"]);
  expect(steps[0].text()).toContain("1. 评分标准");
  expect(steps[0].text()).toContain("《毕业论文》v1");
  expect(steps[2].get(".mark").text()).toBe("!");
  expect(steps[2].text()).toContain("（等你决定）");
  expect(wrapper.attributes("data-card-id")).toBe("plan-1");
  wrapper.unmount();
});
