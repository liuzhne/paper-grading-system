import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it } from "vitest";

import { blockedByMissingModel, requiresModelSetup } from "@/router/llm-gate.js";
import { useSessionStore } from "@/stores/session.js";

/**
 * 「有没有可用模型」的判定与拦截（D-027、D-028、D-032）。
 *
 * **判定、呈现、拦截是三件事**：
 * - `requiresModelSetup` 只回答需不需要配置；
 * - `ModelSetupDialog` 负责说清楚，可关闭（否则它会盖住配置表单）；
 * - `blockedByMissingModel` 负责拦住功能页——**关掉弹窗不等于可以用系统**。
 *
 * 上一版把拦截也交给了弹窗，于是关掉弹窗后全站畅通，而那些页面上的动作在后端
 * 一律失败。用户看到的是「能进去，但什么都做不成」。
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

  const NO_MODEL = {
    platform_model_available: false,
    has_own_connection: false,
    can_use_llm: false,
  };

  it("有平台模型时不需要配置", () => {
    const session = withCapabilities({ ...NO_MODEL, platform_model_available: true, can_use_llm: true });

    expect(requiresModelSetup(session)).toBe(false);
  });

  it("自带连接时不需要配置", () => {
    const session = withCapabilities({ ...NO_MODEL, has_own_connection: true, can_use_llm: true });

    expect(requiresModelSetup(session)).toBe(false);
  });

  it("两者都没有时需要配置", () => {
    expect(requiresModelSetup(withCapabilities(NO_MODEL))).toBe(true);
  });

  it("能力表还没加载时不下结论", () => {
    const session = useSessionStore();
    session.capabilities = null;

    // 首屏能力表未到就判定，会让正常用户先看到一次误报。
    expect(requiresModelSetup(session)).toBe(false);
  });
});

describe("功能页拦截", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  function noModel() {
    const session = useSessionStore();
    session.capabilities = {
      llm: {
        platform_model_available: false,
        has_own_connection: false,
        can_use_llm: false,
      },
      abilities: {},
    };
    return session;
  }

  it("没有模型时功能页被拦下", () => {
    expect(blockedByMissingModel(noModel(), { name: "tasks" })).toBe(true);
    expect(blockedByMissingModel(noModel(), { name: "dashboard" })).toBe(true);
    expect(blockedByMissingModel(noModel(), { name: "review" })).toBe(true);
  });

  it("配置页永远放行", () => {
    // 拦住它们等于把用户挡在一个自己无法解开的门外。
    expect(blockedByMissingModel(noModel(), { name: "account" })).toBe(false);
    expect(blockedByMissingModel(noModel(), { name: "ops" })).toBe(false);
  });

  it("有可用模型时全部放行", () => {
    const session = useSessionStore();
    session.capabilities = {
      llm: { platform_model_available: true, has_own_connection: false, can_use_llm: true },
      abilities: {},
    };

    expect(blockedByMissingModel(session, { name: "tasks" })).toBe(false);
  });

  it("能力表未加载时不拦", () => {
    const session = useSessionStore();
    session.capabilities = null;

    expect(blockedByMissingModel(session, { name: "tasks" })).toBe(false);
  });

  it("拦截与弹窗是否被关掉无关", () => {
    /*
     * 这条是这次修复的核心：用户关掉弹窗只是不想再看那段说明，不代表他已经配好
     * 模型。拦截必须只看能力表。
     */
    const session = noModel();

    expect(blockedByMissingModel(session, { name: "tasks" })).toBe(true);
  });
});
