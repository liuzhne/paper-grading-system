import { mount, flushPromises } from "@vue/test-utils";
import { beforeEach, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import AccountView from "./AccountView.vue";
import { api } from "@/api/client.js";
import { useSessionStore } from "@/stores/session.js";

vi.mock("@/lib/anchor-highlight.js", () => ({ useAnchorHighlight: () => ({ highlighted: false }) }));
vi.mock("@/api/client.js", () => ({
  api: { get: vi.fn(), post: vi.fn() },
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
