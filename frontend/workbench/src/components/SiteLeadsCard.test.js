import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import SiteLeadsCard from "./SiteLeadsCard.vue";
import { api } from "@/api/client.js";

vi.mock("@/api/client.js", () => ({
  api: { get: vi.fn(), patch: vi.fn(), del: vi.fn() },
  ApiError: class extends Error {},
  StaleContextError: class extends Error {},
}));

const LEADS = [
  {
    id: "l1",
    kind: "deploy_assessment",
    email: "dean@example.edu.cn",
    organization_name: "某大学 计算机学院",
    note: "三门课",
    source_path: "/about",
    status: "new",
    submit_count: 2,
    created_at: "2026-10-18T01:00:00",
    last_submitted_at: "2026-10-18T02:00:00",
    handled_at: null,
  },
  {
    id: "l2",
    kind: "trial",
    email: "teacher@example.edu.cn",
    organization_name: null,
    note: null,
    source_path: "/",
    status: "new",
    submit_count: 1,
    created_at: "2026-10-18T03:00:00",
    last_submitted_at: "2026-10-18T03:00:00",
    handled_at: null,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue({ items: LEADS, total: 2, new_count: 2 });
});

afterEach(() => vi.restoreAllMocks());

it("默认只看新申请，标题旁显示数量", async () => {
  const wrapper = mount(SiteLeadsCard);
  await flushPromises();

  expect(api.get).toHaveBeenCalledWith("/site/leads?limit=50&offset=0&status=new");
  expect(wrapper.get('[data-test="site-leads-new-count"]').text()).toBe("2 条新申请");
  const rows = wrapper.findAll("tbody tr");
  expect(rows).toHaveLength(2);
  expect(rows[0].text()).toContain("部署评估");
  expect(rows[0].text()).toContain("某大学 计算机学院");
  expect(rows[0].text()).toContain("共提交 2 次");
  expect(rows[0].get("a").attributes("href")).toContain("mailto:dean@example.edu.cn");
});

it("切到「全部」不带状态过滤", async () => {
  const wrapper = mount(SiteLeadsCard);
  await flushPromises();

  await wrapper.findAll(".filters button")[1].trigger("click");
  await flushPromises();

  expect(api.get).toHaveBeenLastCalledWith("/site/leads?limit=50&offset=0");
});

it("改状态后重新加载", async () => {
  api.patch.mockResolvedValue({ ...LEADS[0], status: "contacted" });
  const wrapper = mount(SiteLeadsCard);
  await flushPromises();

  await wrapper.get("#lead-status-l1").setValue("contacted");
  await flushPromises();

  expect(api.patch).toHaveBeenCalledWith("/site/leads/l1", { status: "contacted" });
  expect(api.get).toHaveBeenCalledTimes(2);
});

it("删除要二次确认；取消就什么都不做", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const wrapper = mount(SiteLeadsCard);
  await flushPromises();

  await wrapper.findAll("tbody tr")[1].get(".btn-danger").trigger("click");
  expect(confirm).toHaveBeenCalled();
  expect(api.del).not.toHaveBeenCalled();

  confirm.mockReturnValue(true);
  api.del.mockResolvedValue(null);
  await wrapper.findAll("tbody tr")[1].get(".btn-danger").trigger("click");
  await flushPromises();
  expect(api.del).toHaveBeenCalledWith("/site/leads/l2");
});

it("没有新申请时说清楚，而不是空表", async () => {
  api.get.mockResolvedValue({ items: [], total: 0, new_count: 0 });
  const wrapper = mount(SiteLeadsCard);
  await flushPromises();

  expect(wrapper.text()).toContain("没有待处理的新申请。");
  expect(wrapper.get('[data-test="site-leads-new-count"]').text()).toBe("没有新申请");
});

it("加载失败给出提示", async () => {
  api.get.mockRejectedValue(new Error("网络错误"));
  const wrapper = mount(SiteLeadsCard);
  await flushPromises();

  expect(wrapper.get('[role="alert"]').text()).toBe("网络错误");
});
