import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, expect, it, vi } from "vitest";
import { createMemoryHistory, createRouter } from "vue-router";

import App from "./App.vue";

afterEach(() => vi.useRealTimers());

it("首次导航完成前显示加载提示，等久了说明冷启动，完成后撤掉", async () => {
  vi.useFakeTimers();
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/", component: { template: "<main>工作台</main>" } },
  ] });
  router.beforeEach(() => gate.then(() => true));
  router.push("/");
  const wrapper = mount(App, { global: { plugins: [router] } });

  expect(wrapper.get('[data-test="app-boot"]').text()).toContain("正在连接服务器");
  expect(wrapper.text()).not.toContain("十几秒");
  await vi.advanceTimersByTimeAsync(4000);
  expect(wrapper.text()).toContain("十几秒");

  release();
  await flushPromises();
  expect(wrapper.find('[data-test="app-boot"]').exists()).toBe(false);
  expect(wrapper.text()).toContain("工作台");
});
