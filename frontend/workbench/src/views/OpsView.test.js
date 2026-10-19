import { mount, flushPromises } from "@vue/test-utils";
import { beforeEach, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import OpsView from "./OpsView.vue";
import { api } from "@/api/client.js";
import { useSessionStore } from "@/stores/session.js";

vi.mock("@/lib/anchor-highlight.js", () => ({ useAnchorHighlight: () => ({ highlighted: false }) }));
vi.mock("@/api/client.js", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
  ApiError: class extends Error {}, StaleContextError: class extends Error {},
}));
beforeEach(() => { setActivePinia(createPinia()); vi.clearAllMocks(); });

it("平台模型可选 Claude，思考强度与结构化输出进 provider_options，其它协议不带", async () => {
  api.get.mockImplementation(async (path) => (path === "/system/platform-llm" ? { configured: false, status: "unconfigured" } : null));
  api.post.mockResolvedValue({ configured: true, status: "active", provider_type: "anthropic_messages" });
  const session = useSessionStore();
  vi.spyOn(session, "loadCapabilities").mockResolvedValue();
  vi.spyOn(session, "can").mockImplementation((ability) => ability === "view_platform_ops");
  const wrapper = mount(OpsView);
  await flushPromises();

  const form = wrapper.get("#platform-llm-form");
  expect(form.find('[data-test="platform-effort"]').exists()).toBe(false);
  await form.get("select").setValue("anthropic_messages");
  await form.get('[data-test="platform-effort"]').setValue("low");
  await form.get('input[type="url"]').setValue("https://bedrock-runtime.us-east-1.amazonaws.com/anthropic");
  await form.findAll('input[type="text"]')[0].setValue("anthropic.claude-opus-5-5");
  await form.get('input[type="password"]').setValue("bedrock-key-1234");
  await form.trigger("submit");
  await flushPromises();

  const [, body] = api.post.mock.calls.find(([url]) => url === "/system/platform-llm");
  expect(body).toMatchObject({
    provider_type: "anthropic_messages",
    model_name: "anthropic.claude-opus-5-5",
    provider_options: { effort: "low" },
  });
  expect(body).not.toHaveProperty("effort");
  expect(body).not.toHaveProperty("structured_output");
});

it("组织视图显示近 24 小时的模型输出观测与各模型明细", async () => {
  const batchJobs = {
    active_count: 0, stale_count: 0, sweep: null,
    llm_failure_rate: { status: "unavailable", maximum_observed: null, maximum_allowed: "0.05" },
    model_output: {
      status: "fail", runs: 1, completion_tokens: 3304, reasoning_tokens: 36918,
      truncated_count: 13, thinking_mismatch_items: 0,
      models: [{ provider: "openai_compatible", model: "google/gemini-2.5-flash", runs: 1, reasoning_tokens: 36918, truncated_count: 13 }],
    },
  };
  api.get.mockImplementation(async (path) => (
    path === "/system/organization-readiness" ? { signals: { batch_jobs: batchJobs } } : null
  ));
  const session = useSessionStore();
  vi.spyOn(session, "loadCapabilities").mockResolvedValue();
  vi.spyOn(session, "can").mockImplementation((ability) => ability === "view_organization_ops");
  const wrapper = mount(OpsView);
  await flushPromises();

  const cell = wrapper.get('[data-test="org-model-output"]');
  expect(cell.text()).toContain("思考 3.7 万（占输出 92%）");
  expect(cell.text()).toContain("截断 13 次");
  expect(cell.find(".chip-danger").exists()).toBe(true);
  expect(wrapper.get(".model-list").text()).toContain("google/gemini-2.5-flash");
});
