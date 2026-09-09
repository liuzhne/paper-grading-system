import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it } from "vitest";

import { requiresModelSetup } from "@/router/llm-gate.js";
import { useSessionStore } from "@/stores/session.js";

/**
 * 「有没有可用模型」的判定（用户决定，2026-09-09 改为弹窗呈现）。
 *
 * 判定本身仍在这里，但**不再触发路由跳转**：跳转会把用户送到一个他没主动去的
 * 页面，还得自己猜发生了什么。改由 `ModelSetupDialog` 阻断式弹窗说清楚，再由
 * 用户点进配置页。
 */
describe("模型可用性判定", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  function withCapabilities(llm) {
    const session = useSessionStore();
    session.capabilities = { llm, abilities: {} };
    return session;
  }

  it("有平台模型时不需要配置", () => {
    const session = withCapabilities({
      platform_model_available: true,
      has_own_connection: false,
      can_use_llm: true,
    });

    expect(requiresModelSetup(session)).toBe(false);
  });

  it("自带连接时不需要配置", () => {
    const session = withCapabilities({
      platform_model_available: false,
      has_own_connection: true,
      can_use_llm: true,
    });

    expect(requiresModelSetup(session)).toBe(false);
  });

  it("两者都没有时需要配置", () => {
    const session = withCapabilities({
      platform_model_available: false,
      has_own_connection: false,
      can_use_llm: false,
    });

    expect(requiresModelSetup(session)).toBe(true);
  });

  it("能力表还没加载时不下结论", () => {
    const session = useSessionStore();
    session.capabilities = null;

    // 首屏能力表未到就判定，会让正常用户先看到一次误报。
    expect(requiresModelSetup(session)).toBe(false);
  });
});
