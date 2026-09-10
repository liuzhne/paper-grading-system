import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it } from "vitest";

import {
  blockedByMissingModel,
  clearBlockedAttempt,
  noteBlockedAttempt,
  pendingBlock,
  requiresModelSetup,
} from "@/router/llm-gate.js";
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

/**
 * 被拦下的那次导航要留痕（2026-09-10 缺陷修复）。
 *
 * 拦截判据一直是实时的 `can_use_llm`，这没错。错的是**呈现**：弹窗用一个组件级的
 * `dismissed` 标记，关掉一次就整个会话不再出现，于是用户点「评分任务」被静默弹回
 * 配置页，屏幕上没有任何解释。
 *
 * 改法是把「这一次导航被拦了」记下来：弹窗按它显示，关闭只清掉这一次。下次再点，
 * 守卫产生新的一条，弹窗重新出现。**没有任何一次性开关跨越两次导航。**
 */
describe("被拦下的导航意图", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    clearBlockedAttempt();
  });

  it("初始没有待处理的拦截", () => {
    expect(pendingBlock.value).toBeNull();
  });

  it("守卫拦下一次导航后留下意图，带着目标路由", () => {
    noteBlockedAttempt({ name: "tasks", fullPath: "/tasks" });

    expect(pendingBlock.value?.name).toBe("tasks");
    expect(pendingBlock.value?.fullPath).toBe("/tasks");
  });

  it("关闭只清掉当前这一次", () => {
    noteBlockedAttempt({ name: "tasks", fullPath: "/tasks" });
    clearBlockedAttempt();

    expect(pendingBlock.value).toBeNull();
  });

  it("关闭之后再被拦一次，意图重新出现——这正是上一版丢掉的行为", () => {
    noteBlockedAttempt({ name: "tasks", fullPath: "/tasks" });
    clearBlockedAttempt();
    noteBlockedAttempt({ name: "exports", fullPath: "/exports" });

    expect(pendingBlock.value?.name).toBe("exports");
  });

  it("连续拦两次记最后一次：用户最后想去哪，就说哪一次", () => {
    noteBlockedAttempt({ name: "tasks", fullPath: "/tasks" });
    noteBlockedAttempt({ name: "review", fullPath: "/review" });

    expect(pendingBlock.value?.name).toBe("review");
  });

  it("每次记录都是新对象——同一个目标连点两次也算两次拦截", () => {
    noteBlockedAttempt({ name: "tasks", fullPath: "/tasks" });
    const first = pendingBlock.value;
    noteBlockedAttempt({ name: "tasks", fullPath: "/tasks" });

    expect(pendingBlock.value).not.toBe(first);
  });
});
