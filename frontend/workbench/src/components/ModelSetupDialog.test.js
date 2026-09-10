import { mount } from "@vue/test-utils";
import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ModelSetupDialog from "@/components/ModelSetupDialog.vue";
import {
  clearBlockedAttempt,
  noteBlockedAttempt,
} from "@/router/llm-gate.js";
import { useSessionStore } from "@/stores/session.js";

/**
 * 未配置可用模型时的阻断式弹窗（用户决定，2026-09-09）。
 *
 * 原先是「先跳到账户页，再显示一条横幅」。横幅容易被忽略，而且用户已经被送到
 * 一个自己没主动去的页面，不知道发生了什么。改为先弹窗说清楚，确认后再跳。
 *
 * **可关闭**（2026-09-10）：遮罩铺满视口，用户到了配置页还挡着就填不了表。
 * 关闭只清掉当前这一条拦截记录——下一次被拦会重新出现，见文件末尾那组用例。
 */
function mountWith(llm, { platformRole = null, blocked = true } = {}) {
  const session = useSessionStore();
  // `isPlatformAdmin` 是从能力表推导的 computed，不能直接赋值——用真实数据驱动，
  // 否则测的是一个现实中不会出现的状态。
  session.capabilities = { llm, abilities: {}, platform_role: platformRole };
  // 弹窗的显示条件是「刚刚有一次导航被拦下」。这些用例都是在描述那个场景下的
  // 呈现，所以默认先记一条——不记的话它们测的是「用户主动来配置页」，那本来就
  // 不该弹。
  if (blocked) noteBlockedAttempt({ name: "tasks", fullPath: "/tasks" });
  return mount(ModelSetupDialog, {
    global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
  });
}

describe("模型配置弹窗", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    clearBlockedAttempt();
  });

  it("有可用模型时不出现", () => {
    const wrapper = mountWith({
      platform_model_available: true,
      has_own_connection: false,
      can_use_llm: true,
    });

    expect(wrapper.find(".dialog").exists()).toBe(false);
  });

  it("没有可用模型时出现并说明原因", () => {
    const wrapper = mountWith({
      platform_model_available: false,
      has_own_connection: false,
      can_use_llm: false,
    });

    expect(wrapper.find(".dialog").exists()).toBe(true);
    expect(wrapper.text()).toContain("请先配置 API Key");
  });

  it("能力表未加载时不出现", () => {
    const wrapper = mountWith(null);

    // 首屏能力表还没到就弹，正常用户会先看到一次误报。
    expect(wrapper.find(".dialog").exists()).toBe(false);
  });

  it("提供关闭按钮", async () => {
    /*
     * 初版设计成不可关闭，理由是「没有模型时整套能力都用不了」。实际用下来这条
     * 站不住：遮罩铺满视口，用户点进配置页之后**弹窗还在，表单点不到**——把人
     * 挡在了他正要去做的那件事前面。改为可关闭（用户决定，2026-09-10）。
     */
    const wrapper = mountWith({
      platform_model_available: false,
      has_own_connection: false,
      can_use_llm: false,
    });

    const close = wrapper.find("[data-test=close]");
    expect(close.exists()).toBe(true);

    await close.trigger("click");
    expect(wrapper.find(".dialog").exists()).toBe(false);
  });

  it("点操作链接后自动收起，不挡住目的地", async () => {
    const wrapper = mountWith({
      platform_model_available: false,
      has_own_connection: false,
      can_use_llm: false,
    });

    await wrapper.findAll("a")[0].trigger("click");

    // 用户点的就是「去配置」——到了目的地还挡着，等于没让他去成。
    expect(wrapper.find(".dialog").exists()).toBe(false);
  });

  it("收起后不再反复弹出", async () => {
    const wrapper = mountWith({
      platform_model_available: false,
      has_own_connection: false,
      can_use_llm: false,
    });
    await wrapper.find("[data-test=close]").trigger("click");

    // 每次路由变化都重新弹，比不弹更烦人。
    await wrapper.vm.$forceUpdate();
    expect(wrapper.find(".dialog").exists()).toBe(false);
  });

  it("普通用户只被指向账户与连接", () => {
    const wrapper = mountWith(
      { platform_model_available: false, has_own_connection: false, can_use_llm: false },
      { platformRole: "member" },
    );

    expect(wrapper.text()).toContain("账户与连接");
    expect(wrapper.text()).not.toContain("运维与质量");
  });

  it("平台管理员两条出路都给", () => {
    const wrapper = mountWith(
      { platform_model_available: false, has_own_connection: false, can_use_llm: false },
      { platformRole: "platform_admin" },
    );

    // 他既可以配平台默认模型，也可以绑自己的连接。
    expect(wrapper.text()).toContain("运维与质量");
    expect(wrapper.text()).toContain("账户与连接");
  });
});

/**
 * 弹窗按「这一次导航被拦了」显示，而不是按一个会话级开关（2026-09-10 缺陷修复）。
 *
 * 上一版的 `dismissed` 是组件级 ref，组件挂在 `AppShell` 上跨路由存活，关掉一次
 * 整个会话不再出现。拦截还在，但用户点任何入口都是**被静默弹回配置页**。
 */
describe("弹窗随每一次被拦的导航重新出现", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    clearBlockedAttempt();
  });

  const NO_MODEL = {
    platform_model_available: false,
    has_own_connection: false,
    can_use_llm: false,
  };

  function mountDialog() {
    const session = useSessionStore();
    session.capabilities = { llm: NO_MODEL, abilities: {}, platform_role: null };
    return mount(ModelSetupDialog, {
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
  }

  it("没有被拦下的导航时不弹——用户主动来配置页，不该被挡", async () => {
    const wrapper = mountDialog();
    await wrapper.vm.$nextTick();

    expect(wrapper.find(".dialog").exists()).toBe(false);
  });

  it("有被拦下的导航时弹出", async () => {
    const wrapper = mountDialog();
    noteBlockedAttempt({ name: "tasks", fullPath: "/tasks" });
    await wrapper.vm.$nextTick();

    expect(wrapper.find(".dialog").exists()).toBe(true);
  });

  it("关掉后再被拦一次，重新弹出", async () => {
    const wrapper = mountDialog();
    noteBlockedAttempt({ name: "tasks", fullPath: "/tasks" });
    await wrapper.vm.$nextTick();

    await wrapper.find("[data-test=close]").trigger("click");
    expect(wrapper.find(".dialog").exists()).toBe(false);

    // 用户没配置就走了，又点了另一个入口。
    noteBlockedAttempt({ name: "exports", fullPath: "/exports" });
    await wrapper.vm.$nextTick();

    expect(wrapper.find(".dialog").exists()).toBe(true);
  });

  it("配置成功后即使还留着拦截意图也不再弹", async () => {
    const session = useSessionStore();
    const wrapper = mountDialog();
    noteBlockedAttempt({ name: "tasks", fullPath: "/tasks" });
    await wrapper.vm.$nextTick();
    expect(wrapper.find(".dialog").exists()).toBe(true);

    session.capabilities = {
      llm: { ...NO_MODEL, has_own_connection: true, can_use_llm: true },
      abilities: {},
      platform_role: null,
    };
    await wrapper.vm.$nextTick();

    expect(wrapper.find(".dialog").exists()).toBe(false);
  });

  it("说出用户本来想去哪，而不是只说「没有模型」", async () => {
    const wrapper = mountDialog();
    noteBlockedAttempt({ name: "tasks", fullPath: "/tasks" });
    await wrapper.vm.$nextTick();

    expect(wrapper.text()).toContain("评分任务");
  });
});
