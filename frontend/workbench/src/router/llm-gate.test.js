import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { requiresModelSetup } from "@/router/llm-gate.js";
import { useSessionStore } from "@/stores/session.js";

/**
 * 没有可用模型时把用户引导去配置 BYOK（用户决定，2026-09-09）。
 *
 * 不配 BYOK 就用不了系统——但**引导不等于把人锁死**：账户与连接页本身、登出、
 * 以及平台管理员去配置平台模型的运维页，都必须仍然可达，否则用户被挡在一个
 * 自己无法解开的门外。
 */
describe("模型可用性守卫", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  function withCapabilities(llm) {
    const session = useSessionStore();
    session.capabilities = { llm, abilities: {} };
    return session;
  }

  it("有平台模型时不拦截", () => {
    const session = withCapabilities({
      platform_model_available: true,
      has_own_connection: false,
      can_use_llm: true,
    });

    expect(requiresModelSetup(session, { name: "tasks" })).toBe(false);
  });

  it("自带连接时不拦截", () => {
    const session = withCapabilities({
      platform_model_available: false,
      has_own_connection: true,
      can_use_llm: true,
    });

    expect(requiresModelSetup(session, { name: "tasks" })).toBe(false);
  });

  it("两者都没有时拦截", () => {
    const session = withCapabilities({
      platform_model_available: false,
      has_own_connection: false,
      can_use_llm: false,
    });

    expect(requiresModelSetup(session, { name: "tasks" })).toBe(true);
  });

  it("账户与连接页本身永远放行", () => {
    const session = withCapabilities({
      platform_model_available: false,
      has_own_connection: false,
      can_use_llm: false,
    });

    // 拦到这里就是把人锁死在门外——他要配连接正是要去这一页。
    expect(requiresModelSetup(session, { name: "account" })).toBe(false);
  });

  it("平台管理员的运维页放行", () => {
    const session = withCapabilities({
      platform_model_available: false,
      has_own_connection: false,
      can_use_llm: false,
    });

    // 平台模型正是在这一页配置的。
    expect(requiresModelSetup(session, { name: "ops" })).toBe(false);
  });

  it("能力表还没加载时不拦截", () => {
    const session = useSessionStore();
    session.capabilities = null;

    // 首屏能力表未到就跳转，会把正常用户闪到配置页。
    expect(requiresModelSetup(session, { name: "tasks" })).toBe(false);
  });
});
