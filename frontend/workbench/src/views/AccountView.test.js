import { mount, flushPromises } from "@vue/test-utils";
import { beforeEach, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import AccountView from "./AccountView.vue";
import { api } from "@/api/client.js";
import { useSessionStore } from "@/stores/session.js";

vi.mock("@/lib/anchor-highlight.js", () => ({ useAnchorHighlight: () => ({ highlighted: false }) }));
vi.mock("@/api/client.js", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
  ApiError: class extends Error {}, StaleContextError: class extends Error {},
}));
beforeEach(() => { setActivePinia(createPinia()); vi.clearAllMocks(); });
it("显示唯一启用连接，切换后刷新全部连接和能力", async () => {
  let rows = [
    { id: "a", name: "模型 A", status: "active" },
    { id: "b", name: "模型 B", status: "disabled" },
  ];
  api.get.mockImplementation(async () => rows);
  api.post.mockImplementation(async () => { rows = rows.map(r => ({ ...r, status: r.id === "b" ? "active" : "disabled" })); });
  const session = useSessionStore();
  const refresh = vi.spyOn(session, "loadCapabilities").mockResolvedValue();
  const wrapper = mount(AccountView);
  await flushPromises();
  const row = wrapper.findAll("tbody tr").find(r => r.text().includes("模型 B"));
  await row.findAll("button").find(b => b.text() === "启用").trigger("click");
  await flushPromises();
  expect(api.post).toHaveBeenCalledWith("/ai-connections/b/activate", {}, expect.any(Object));
  expect(refresh).toHaveBeenCalled();
  expect(wrapper.findAll("tbody tr").find(r => r.text().includes("模型 A")).text()).toContain("未启用");
  expect(wrapper.findAll("tbody tr").find(r => r.text().includes("模型 B")).text()).toContain("已启用");
});

it("新建连接默认自动识别协议，测试后显示识别结果，手动选择放在高级设置", async () => {
  api.get.mockResolvedValue([]);
  api.post.mockImplementation(async (url) => url === "/ai-connections/test-draft"
    ? { provider_type: "openai_compatible", model_name: "gpt-4.1-mini", status: "verified", detection: "url_suffix", base_url: "https://gw.example/v1" }
    : {});
  vi.spyOn(useSessionStore(), "loadCapabilities").mockResolvedValue();
  const wrapper = mount(AccountView);
  await flushPromises();
  const protocol = () => wrapper.get('[data-test="protocol-value"]').text();
  expect(protocol()).toBe("自动识别（测试或保存时确定）");
  expect(wrapper.find('select[data-test="protocol-select"]').element.closest("details").open).toBe(false);

  await wrapper.get('input[type="url"]').setValue("https://gw.example/v1/chat/completions");
  await wrapper.findAll("button").find(b => b.text() === "测试配置").trigger("click");
  await flushPromises();
  expect(api.post.mock.calls[0][1]).toMatchObject({ provider_type: "auto", base_url: "https://gw.example/v1/chat/completions" });
  expect(wrapper.get('input[type="url"]').element.value).toBe("https://gw.example/v1");
  expect(protocol()).toBe("OpenAI 兼容（Chat Completions） · 按接口地址后缀识别");

  const model = wrapper.findAll("label.field").find(l => l.text().includes("模型")).get("input");
  await model.setValue("other-model");
  expect(protocol()).toBe("自动识别（测试或保存时确定）");

  await wrapper.get('select[data-test="protocol-select"]').setValue("openai_responses");
  expect(protocol()).toBe("OpenAI Responses · 手动选择");
  await wrapper.get(".card-foot form").trigger("submit");
  await flushPromises();
  expect(api.post).toHaveBeenLastCalledWith("/ai-connections", expect.objectContaining({ provider_type: "openai_responses" }), expect.any(Object));
});

it("已有连接可以单独设置同时请求数，提交时保留其它选项，清空即移除", async () => {
  let rows = [{ id: "a", name: "Z.ai 免费", status: "active", provider_options: { top_p: 0.95 } }];
  api.get.mockImplementation(async () => rows);
  api.patch.mockImplementation(async (_url, body) => { rows = [{ ...rows[0], provider_options: body.provider_options }]; });
  const wrapper = mount(AccountView);
  await flushPromises();

  await wrapper.get('[data-test="tune-concurrency"]').trigger("click");
  await wrapper.get('[data-test="tune-form"] input').setValue("1");
  await wrapper.get('[data-test="tune-form"]').trigger("submit");
  await flushPromises();
  // PATCH 是整体替换：漏带 top_p 会把用户原有的采样设置清掉。
  expect(api.patch).toHaveBeenLastCalledWith("/ai-connections/a", { provider_options: { top_p: 0.95, max_concurrency: 1 } }, expect.any(Object));
  expect(wrapper.get('[data-test="conn-concurrency"]').text()).toBe("同时请求 1 个");

  await wrapper.get('[data-test="tune-concurrency"]').trigger("click");
  expect(wrapper.get('[data-test="tune-form"] input').element.value).toBe("1");
  await wrapper.get('[data-test="tune-form"] input').setValue("");
  await wrapper.get('[data-test="tune-form"]').trigger("submit");
  await flushPromises();
  expect(api.patch).toHaveBeenLastCalledWith("/ai-connections/a", { provider_options: { top_p: 0.95 } }, expect.any(Object));
  expect(wrapper.find('[data-test="conn-concurrency"]').exists()).toBe(false);
});

it("新建连接可以在高级设置里填写同时请求数", async () => {
  api.get.mockResolvedValue([]);
  api.post.mockResolvedValue({});
  vi.spyOn(useSessionStore(), "loadCapabilities").mockResolvedValue();
  const wrapper = mount(AccountView);
  await flushPromises();
  await wrapper.get('[data-test="draft-concurrency"]').setValue("1");
  await wrapper.get(".card-foot form").trigger("submit");
  await flushPromises();
  expect(api.post).toHaveBeenLastCalledWith("/ai-connections", expect.objectContaining({ provider_options: { max_concurrency: 1 } }), expect.any(Object));
});

it("Claude 地址显示思考强度与结构化输出，提交时带上设置并指明协议", async () => {
  api.get.mockResolvedValue([]);
  api.post.mockResolvedValue({});
  vi.spyOn(useSessionStore(), "loadCapabilities").mockResolvedValue();
  const wrapper = mount(AccountView);
  await flushPromises();
  expect(wrapper.find('[data-test="draft-effort"]').exists()).toBe(false);

  await wrapper.get('input[type="url"]').setValue("https://bedrock-runtime.us-east-1.amazonaws.com/anthropic");
  await wrapper.get('[data-test="draft-effort"]').setValue("low");
  await wrapper.get('[data-test="draft-structured-output"]').setValue("off");
  await wrapper.get(".card-foot form").trigger("submit");
  await flushPromises();

  const [, body] = api.post.mock.calls.find(([url]) => url === "/ai-connections");
  expect(body.provider_type).toBe("anthropic_messages");
  expect(body.provider_options).toEqual({ effort: "low", structured_output: "off" });

  // 换回 OpenAI 地址：Claude 设置隐藏，也不再提交。
  await wrapper.get('input[type="url"]').setValue("https://api.openai.com/v1");
  expect(wrapper.find('[data-test="draft-effort"]').exists()).toBe(false);
  await wrapper.get(".card-foot form").trigger("submit");
  await flushPromises();
  const last = api.post.mock.calls.filter(([url]) => url === "/ai-connections").at(-1)[1];
  expect(last.provider_type).toBe("auto");
  expect(last.provider_options).toEqual({});
});

it("手动选择 Claude 协议后显示设置，连接列表标出 Claude 与思考强度", async () => {
  api.get.mockResolvedValue([{
    id: "c", name: "Bedrock", status: "active", provider_type: "anthropic_messages",
    model_name: "anthropic.claude-opus-5-5", provider_options: { effort: "medium" },
  }]);
  const wrapper = mount(AccountView);
  await flushPromises();
  const row = wrapper.findAll("tbody tr").find(r => r.text().includes("Bedrock"));
  expect(row.text()).toContain("Claude");
  expect(row.get('[data-test="conn-effort"]').text()).toBe("思考强度 中");

  await wrapper.get('input[type="url"]').setValue("https://gw.example/v1");
  expect(wrapper.find('[data-test="draft-effort"]').exists()).toBe(false);
  await wrapper.get('select[data-test="protocol-select"]').setValue("anthropic_messages");
  expect(wrapper.get('[data-test="protocol-value"]').text()).toBe("Anthropic Messages（Claude） · 手动选择");
  expect(wrapper.find('[data-test="draft-effort"]').exists()).toBe(true);
});
