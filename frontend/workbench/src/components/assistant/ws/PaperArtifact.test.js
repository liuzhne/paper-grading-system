import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createMemoryHistory, createRouter } from "vue-router";

import PaperArtifact from "./PaperArtifact.vue";

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

it("单篇得分解释：总分、各项得分、扣分最多的几处带原文与位置，以及修改建议（方案 §31.3）", async () => {
  stubItems([
    { id: "i1", criterion_code: "T04", criterion_name: "研究方法", max_score: 20, final_score: 14, need_manual_review: true,
      deduction_items: [{ reason: "未说明研究方法", points: 6 }], deductions: [], reason: "方法部分偏简略。",
      revision_suggestion: "补充研究方法的步骤与依据。",
      evidence_view: [{ quote: "本文采用问卷调查。", section_title: "第 3 章", page_start: 14 }] },
    { id: "i2", criterion_code: "T05", criterion_name: "文献综述", max_score: 20, final_score: 18, need_manual_review: false,
      deduction_items: [{ reason: "外文文献不足", points: 2 }], deductions: [], reason: "综述基本完整。",
      revision_suggestion: null, evidence_view: [] },
  ]);
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:any(.*)", component: { template: "<div />" } }] });
  const wrapper = mount(PaperArtifact, {
    props: { card: { id: "c1", type: "paper_result", batch_id: "b1", paper_id: "p1", run_id: "r1" }, head: { title: "第 1 份" } },
    global: { plugins: [router] },
  });
  await flushPromises();

  expect(wrapper.get(".headline").text()).toContain("32");
  expect(wrapper.get(".headline").text()).toContain("2 处扣分 · 合计 −8");
  const hits = wrapper.findAll(".hit");
  expect(hits[0].text()).toContain("未说明研究方法");
  expect(hits[0].text()).toContain("−6");
  expect(hits[0].get(".quote").text()).toBe("“本文采用问卷调查。”");
  expect(hits[0].get(".where").text()).toBe("第 3 章 · 第 14 页");
  const suggestions = wrapper.findAll('[data-test="card-suggestion"]');
  expect(suggestions).toHaveLength(1);
  expect(suggestions[0].text()).toContain("补充研究方法的步骤与依据。");
  expect(wrapper.get("a.btn-primary").attributes("href")).toBe("/batches/b1/grade?paper=p1");
  wrapper.unmount();
});
