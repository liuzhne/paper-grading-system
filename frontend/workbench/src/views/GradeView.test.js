import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createMemoryHistory, createRouter } from "vue-router";

import GradeView from "./GradeView.vue";

/** 按路径返回的假接口；`gate` 可以让材料列表先挂起，模拟冷启动。 */
function stubFetch(table, gate = null) {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (url) => {
    const path = String(url).replace("/api", "").split("?")[0];
    if (gate && path === "/papers") await gate;
    const body = table[path];
    if (body === undefined) return new Response("{}", { status: 404 });
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

async function mountGrade() {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/batches/:batchId/grade", name: "grade", component: GradeView },
    { path: "/tasks/:batchId/run", name: "task-run", component: { template: "<div />" } },
    { path: "/tasks", name: "tasks", component: { template: "<div />" } },
  ] });
  await router.push("/batches/b1/grade");
  await router.isReady();
  return mount(GradeView, { global: { plugins: [router] } });
}

beforeEach(() => {
  setActivePinia(createPinia());
  globalThis.__PGS_CONFIG__ = undefined;
});
afterEach(() => vi.unstubAllGlobals());

const TABLE = {
  "/papers": [{ id: "p1", student_id: "22008020625", title: "老年安全监测系统", status: "parsed", file_name: "a.docx" }],
  "/batches/b1": { id: "b1", business_profile_key: "thesis" },
  "/scoring-runs": [],
  "/papers/p1/neighbors": { batch_id: "b1", paper_id: "p1", position: 1, total: 1, previous_paper_id: null, next_paper_id: null },
};

it("材料还没回来时显示加载中，而不是“0 份”“未命名材料”", async () => {
  let release;
  stubFetch(TABLE, new Promise((resolve) => { release = resolve; }));
  const wrapper = await mountGrade();
  await flushPromises();
  expect(wrapper.text()).toContain("正在加载");
  expect(wrapper.text()).not.toContain("未命名材料");
  expect(wrapper.text()).not.toContain("材料 · 0");
  release();
  await flushPromises();
  expect(wrapper.text()).toContain("22008020625");
  wrapper.unmount();
});

it("没有评分结果的材料给出下一步：到评分进度页查看原因或开始评分", async () => {
  stubFetch(TABLE);
  const wrapper = await mountGrade();
  await flushPromises();
  const hint = wrapper.get('[data-test="no-result"]');
  expect(hint.text()).toContain("还没有评分结果");
  expect(hint.get("a").attributes("href")).toBe("/tasks/b1/run");
  wrapper.unmount();
});
