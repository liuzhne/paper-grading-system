import { mount, flushPromises } from "@vue/test-utils";
import { beforeEach, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { createRouter, createMemoryHistory } from "vue-router";
import NewTaskView from "./NewTaskView.vue";
import { api } from "@/api/client.js";
import { useSessionStore } from "@/stores/session.js";

vi.mock("@/api/client.js", () => ({
  api: { get: vi.fn(), post: vi.fn() }, request: vi.fn(),
  ApiError: class extends Error {}, StaleContextError: class extends Error {},
}));
beforeEach(() => { setActivePinia(createPinia()); vi.clearAllMocks(); });
it("有平台默认模型时仍自动显示唯一启用的个人连接", async () => {
  useSessionStore().capabilities = { llm: { platform_model_available: true } };
  api.get.mockImplementation(async path => {
    if (path === "/ai-connections") return [
      { id: "disabled", status: "disabled", name: "旧模型" },
      { id: "enabled", status: "active", name: "百炼", model_name: "fixture", provider_type: "openai_compatible" },
    ];
    if (path === "/system/capabilities") return { upload: { accepted_extensions: [".docx"], max_size_mb: 20 } };
    return [];
  });
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/", component: NewTaskView },
    { path: "/tasks", name: "tasks", component: { template: "<div />" } },
    { path: "/account", name: "account", component: { template: "<div />" } },
  ] });
  await router.push("/");
  await router.isReady();
  const wrapper = mount(NewTaskView, { global: { plugins: [router] } });
  await flushPromises();
  const select = wrapper.find("select");
  expect(select.element.value).toBe("enabled");
  expect(select.attributes("disabled")).toBeDefined();
  expect(select.text()).toContain("百炼");
  expect(select.text()).not.toContain("旧模型");
});

it("开始评分前显示本地用量估算，超过上限时提前提示", async () => {
  useSessionStore().capabilities = { llm: { platform_model_available: true } };
  api.get.mockImplementation(async path => {
    if (path === "/system/capabilities") return { upload: { accepted_extensions: [".docx"], max_size_mb: 20 } };
    if (path === "/batches/b1") return { id: "b1", name: "批次" };
    if (path === "/papers?batch_id=b1") return [{ id: "p1", file_name: "a.docx", status: "parsed" }];
    if (path === "/batches/b1/score-estimate") return {
      calls: 49, estimated_input_tokens: 84000, reused_rules: 12, unsupported_papers: 0,
      violations: [{ kind: "batch", estimated: 84000, cap: 50000 }],
    };
    return [];
  });
  api.post.mockResolvedValue({ ready_count: 1, blocking_count: 0, warning_count: 0, findings: [] });
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/", component: NewTaskView },
    { path: "/tasks", name: "tasks", component: { template: "<div />" } },
    { path: "/account", name: "account", component: { template: "<div />" } },
  ] });
  await router.push("/?batch=b1");
  await router.isReady();
  const wrapper = mount(NewTaskView, { global: { plugins: [router] } });
  await flushPromises();
  expect(wrapper.text()).toContain("49 次");
  expect(wrapper.find("[data-test=estimate-tokens]").text()).toBe("约 8.4 万");
  expect(wrapper.text()).toContain("12 条规则可复用");
  expect(wrapper.find("[data-test=estimate-over-cap]").exists()).toBe(true);
});

it("评分标准后显示文档类型；技术方案类型的标准不可选", async () => {
  useSessionStore().capabilities = { llm: { platform_model_available: true } };
  api.get.mockImplementation(async path => {
    if (path === "/rubrics") return [
      { id: "r1", name: "采购方案评审标准", version: "v1.0", status: "published", business_profile_key: "general_document" },
      { id: "r2", name: "本科毕业论文评分标准", version: "v2.1", status: "published", business_profile_key: "thesis" },
      { id: "r3", name: "技术方案", version: "v1", status: "published", business_profile_key: "technical_proposal" },
    ];
    if (path === "/system/capabilities") return { upload: { accepted_extensions: [".docx"], max_size_mb: 20 } };
    return [];
  });
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/", component: NewTaskView },
    { path: "/tasks", name: "tasks", component: { template: "<div />" } },
    { path: "/account", name: "account", component: { template: "<div />" } },
  ] });
  await router.push("/");
  await router.isReady();
  const wrapper = mount(NewTaskView, { global: { plugins: [router] } });
  await flushPromises();
  const radios = wrapper.findAll("input.rubric-radio");
  expect(radios.map(radio => radio.element.value)).toEqual(["r1", "r2"]);
  expect(wrapper.findAll('[data-test="rubric-type"]').map(chip => chip.text())).toEqual(["通用文档", "毕业论文"]);
  expect(wrapper.text()).toContain("技术方案只能通过提交接口评分");
  await radios[0].setValue(true);
  expect(wrapper.get('[data-test="type-note"]').text()).toContain("按通用文档处理");
  await radios[1].setValue(true);
  expect(wrapper.get('[data-test="type-note"]').text()).toContain("按毕业论文处理");
});

it("评分标准与连接还在加载时不说“没有”", async () => {
  useSessionStore().capabilities = { llm: { platform_model_available: false } };
  let releaseRubrics;
  api.get.mockImplementation(path => {
    if (path === "/rubrics") return new Promise(resolve => { releaseRubrics = () => resolve([]); });
    if (path === "/ai-connections") return new Promise(() => {});
    if (path === "/system/capabilities") return Promise.resolve({ upload: { accepted_extensions: [".docx"], max_size_mb: 20 } });
    return Promise.resolve([]);
  });
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/", component: NewTaskView },
    { path: "/tasks", name: "tasks", component: { template: "<div />" } },
    { path: "/account", name: "account", component: { template: "<div />" } },
  ] });
  await router.push("/");
  await router.isReady();
  const wrapper = mount(NewTaskView, { global: { plugins: [router] } });
  await flushPromises();
  expect(wrapper.text()).toContain("正在加载评分标准");
  expect(wrapper.text()).not.toContain("还没有已发布的评分标准");
  expect(wrapper.text()).not.toContain("你还没有可用的 AI 连接");
  releaseRubrics();
  await flushPromises();
  expect(wrapper.text()).toContain("还没有已发布的评分标准");
});
