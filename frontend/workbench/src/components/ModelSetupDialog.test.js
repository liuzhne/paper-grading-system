import { mount } from "@vue/test-utils";
import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ModelSetupDialog from "@/components/ModelSetupDialog.vue";
import { useSessionStore } from "@/stores/session.js";

/**
 * 未配置可用模型时的阻断式弹窗（用户决定，2026-09-09）。
 *
 * 原先是「先跳到账户页，再显示一条横幅」。横幅容易被忽略，而且用户已经被送到
 * 一个自己没主动去的页面，不知道发生了什么。改为先弹窗说清楚，确认后再跳。
 *
 * **不可关闭**：没有可用模型时整套评分能力都用不了，给一个「关闭」等于放人进去
 * 撞一连串失败。唯一的出路是去配置。
 */
function mountWith(llm, { platformRole = null } = {}) {
  const session = useSessionStore();
  // `isPlatformAdmin` 是从能力表推导的 computed，不能直接赋值——用真实数据驱动，
  // 否则测的是一个现实中不会出现的状态。
  session.capabilities = { llm, abilities: {}, platform_role: platformRole };
  return mount(ModelSetupDialog, {
    global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
  });
}

describe("模型配置弹窗", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
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
    expect(wrapper.text()).toContain("尚未配置可用模型");
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
