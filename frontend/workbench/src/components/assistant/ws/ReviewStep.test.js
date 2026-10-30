import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createMemoryHistory, createRouter } from "vue-router";

import { useAssistantStore } from "@/stores/assistant.js";

import ReviewStep from "./ReviewStep.vue";

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** 按“方法 路径”路由的假服务器；规则状态随确认、发布而变。 */
function fakeServer() {
  const state = { rubricStatus: "draft", rules: {
    a: { id: "a", criterion_id: "c1", rule_code: "r.a", name: "未说明课题来源", status: "draft", content_token: "ta",
      direction: "deduct", max_points: "4", origin: { severity: "moderate", trigger: "绪论缺少课题来源" },
      sources: [{ sheet: "评分细则", row: 3, text: "绪论需阐明课题来源。" }] },
    b: { id: "b", criterion_id: "c2", rule_code: "r.b", name: "图表无编号", status: "approved", content_token: "tb",
      direction: "deduct", max_points: "1", origin: { severity: "minor" }, sources: [] },
  } };
  const calls = [];
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (url, init = {}) => {
    const method = (init.method || "GET").toUpperCase();
    const path = String(url).replace(/^\/api/, "");
    calls.push(`${method} ${path}`);
    const json = (payload) => new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    if (path === "/rubrics/import-sessions/s1") return json({ id: "s1", status: "confirmed", rubric_id: "r1", criteria: [] });
    if (path === "/rubrics/r1") return json({ id: "r1", status: state.rubricStatus, version: "v1.0", criteria: [
      { id: "c1", code: "T01", name: "选题与开题", max_score: 20, description: "选题来源于专业相关问题。" },
      { id: "c2", code: "T06", name: "论文规范", max_score: 20, description: null },
    ] });
    if (path === "/rubrics/r1/review-workspace") return json({ compilation_id: "comp1", structural_blockers: [], rules: Object.values(state.rules) });
    if (path === "/rubrics/r1/rules/r.a/confirm") { state.rules.a.status = "approved"; return json({}); }
    if (path === "/rubrics/r1/submit-review") { state.rubricStatus = "review"; return json({}); }
    if (path === "/rubrics/r1/publish") { state.rubricStatus = "published"; return json({}); }
    if (path === "/assistant/conversations/c1/runs") return json({ messages: [], pending: null, conversation: { id: "c1", title: "t", focus: {} } });
    return new Response("{}", { status: 404 });
  }));
  return calls;
}

it("核对评分规则：逐项确认后发布，确认与发布走评分标准页同一组接口，发布后马上接着走流程", async () => {
  const calls = fakeServer();
  const store = useAssistantStore();
  store.conversation = { id: "c1", title: "t", focus: {} };
  store.pending = { kind: "wait_publish", card_id: "wait-1" };
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:any(.*)", component: { template: "<div />" } }] });
  const wrapper = mount(ReviewStep, {
    props: { context: { importSessionId: "s1", cards: {} }, head: { title: "核对评分规则" }, wide: true },
    global: { plugins: [router] },
  });
  await flushPromises();

  // 默认选中第一项还有待确认细则的评分项，评分依据列出评分说明与原文。
  expect(wrapper.get(".nav-item.on").text()).toContain("选题与开题");
  expect(wrapper.get(".basis-body").text()).toContain("选题来源于专业相关问题。");
  expect(wrapper.get(".basis-body").text()).toContain("绪论需阐明课题来源。");
  expect(wrapper.get(".rule-card").text()).toContain("未说明课题来源");
  expect(wrapper.get(".rule-card").text()).toContain("−4");
  const primary = wrapper.get('[data-test="review-primary"]');
  expect(primary.text()).toBe("确认本项 1 条");

  await primary.trigger("click");
  await flushPromises();
  expect(calls).toContain("POST /rubrics/r1/rules/r.a/confirm");
  expect(wrapper.get('[data-test="review-primary"]').text()).toBe("发布评分标准 v1.0");

  await wrapper.get('[data-test="review-primary"]').trigger("click");
  await flushPromises();
  const writes = calls.filter((call) => call.startsWith("POST"));
  expect(writes).toEqual(["POST /rubrics/r1/rules/r.a/confirm", "POST /rubrics/r1/submit-review", "POST /rubrics/r1/publish",
    "POST /assistant/conversations/c1/runs"]);
  expect(wrapper.find('[data-test="review-primary"]').exists()).toBe(false);
  expect(wrapper.text()).toContain("v1.0 已发布");
  wrapper.unmount();
});
