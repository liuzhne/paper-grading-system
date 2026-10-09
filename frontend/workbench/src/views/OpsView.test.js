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
