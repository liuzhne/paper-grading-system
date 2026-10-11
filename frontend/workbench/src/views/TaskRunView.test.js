import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, expect, it, vi } from "vitest";
import { createMemoryHistory, createRouter } from "vue-router";

import TaskRunView from "./TaskRunView.vue";
import { api, ApiError } from "@/api/client.js";

vi.mock("@/api/client.js", () => ({
  api: { get: vi.fn(), post: vi.fn() },
  ApiError: class extends Error {
    constructor(status, detail) { super(detail); this.status = status; this.detail = detail; }
  },
  StaleContextError: class extends Error {},
}));

beforeEach(() => { setActivePinia(createPinia()); vi.clearAllMocks(); });

async function mountRun(batch) {
  api.get.mockImplementation(async (path) => {
    if (path === "/batches/b1") return batch;
    if (path === "/batches/b1/summary") return { rubric_name: "标准", papers: [{ paper_id: "p1", file_name: "a.docx" }] };
    if (path === "/batches/b1/score-jobs/latest") throw new ApiError(404, "batch scoring job not found");
    return null;
  });
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/tasks/:batchId/run", name: "task-run", component: TaskRunView },
    { path: "/tasks", name: "tasks", component: { template: "<div />" } },
    { path: "/batches/:batchId/grade", name: "grade", component: { template: "<div />" } },
  ] });
  await router.push("/tasks/b1/run");
  await router.isReady();
  const wrapper = mount(TaskRunView, { global: { plugins: [router] } });
  await flushPromises();
  return wrapper;
}

it("没有后台评分记录不是错误：停在“评分中”的旧批次说明原因并可重新开始", async () => {
  const wrapper = await mountRun({ id: "b1", name: "软件工程评价 3", status: "scoring" });

  expect(wrapper.text()).not.toContain("batch scoring job not found");
  expect(wrapper.find('[role="alert"]').exists()).toBe(false);
  expect(wrapper.get('[data-test="no-job"]').text()).toContain("旧版本");

  api.post.mockResolvedValue({ id: "j1", status: "queued", items: [], total_items: 1, pending_count: 1,
    running_count: 0, succeeded_count: 0, skipped_count: 0, failed_count: 0, canceled_count: 0 });
  await wrapper.get('[data-test="no-job"] button').trigger("click");
  await flushPromises();
  expect(api.post).toHaveBeenCalledWith("/batches/b1/score-jobs", { rescore: false, max_workers: 2 });
  wrapper.unmount();
});

it("其它错误照常显示", async () => {
  api.get.mockImplementation(async (path) => {
    if (path === "/batches/b1/score-jobs/latest") throw new ApiError(500, "服务器错误");
    return {};
  });
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/tasks/:batchId/run", name: "task-run", component: TaskRunView },
    { path: "/tasks", name: "tasks", component: { template: "<div />" } },
  ] });
  await router.push("/tasks/b1/run");
  await router.isReady();
  const wrapper = mount(TaskRunView, { global: { plugins: [router] } });
  await flushPromises();
  expect(wrapper.get('[role="alert"]').text()).toContain("服务器错误");
  wrapper.unmount();
});
