import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it } from "vitest";
import { nextTick } from "vue";

import { useAssistantStore } from "@/stores/assistant.js";

import WorkspacePanel from "./WorkspacePanel.vue";

/** 各步骤视图会去取接口；这里只测外框（标签、步骤条、提示），视图用占位。 */
const stubs = Object.fromEntries(["SourceStep", "ReviewStep", "RubricStep", "ModelStep", "FilesStep", "GradingStep", "ReportStep",
  "ProgressArtifact", "PaperArtifact", "TodoArtifact"].map((name) => [name, { props: ["head"], template: `<div class="stub" data-view="${name}">{{ head?.title }}</div>` }]));

const plan = {
  id: "plan-1", type: "plan", status: "running", flow: "import",
  steps: [
    ["import", "导入评分标准", "done"], ["publish", "核对并发布评分标准", "done"], ["model", "评分模型", "done"],
    ["files", "选择待评分文件", "current"], ["upload", "上传与预检", "todo"], ["confirm", "确认开始评分", "todo"],
    ["scoring", "评分", "todo"], ["report", "汇报结果", "todo"],
  ].map(([key, label, status]) => ({ key, label, status, detail: null })),
};
const messages = [
  { id: "m1", role: "assistant", text: "这次评分分 8 步完成", cards: [plan], created_at: "2026-10-29T02:00:00" },
  { id: "m2", role: "user", text: "现在进展到哪了？", cards: [], created_at: "2026-10-29T02:01:00" },
  { id: "m3", role: "assistant", text: "现在在第 4 步", created_at: "2026-10-29T02:01:01",
    cards: [{ id: "snap-1", type: "progress_snapshot", as_of: "2026-10-29T02:01:01", steps: plan.steps, plan_status: "running" }] },
];

beforeEach(() => {
  setActivePinia(createPinia());
});

describe("工作区外框", () => {
  it("任务标签在前、结果标签在后；步骤条按计划画，点已完成的步骤会提示回到当前", async () => {
    const store = useAssistantStore();
    store.messages = messages;
    store.pending = { kind: "pick_files", card_id: "files-1" };
    const wrapper = mount(WorkspacePanel, { props: { width: 900 }, global: { stubs } });

    const tabs = wrapper.findAll('[role="tab"]');
    expect(tabs).toHaveLength(2);
    expect(tabs[0].text()).toBe("任务评分任务4/8");
    expect(tabs[1].text()).toContain("进展任务进展");
    const steps = wrapper.findAll(".step");
    expect(steps).toHaveLength(8);
    expect(steps.map((step) => step.classes().find((name) => name.startsWith("st-")))).toEqual(
      ["st-done", "st-done", "st-done", "st-current", "st-todo", "st-todo", "st-todo", "st-todo"]);
    expect(steps[4].attributes("disabled")).toBeDefined();
    expect(wrapper.get(".stub").attributes("data-view")).toBe("FilesStep");

    await steps[1].trigger("click");
    expect(store.ws.step).toBe("publish");
    expect(wrapper.get(".stub").attributes("data-view")).toBe("ReviewStep");
    expect(wrapper.get(".past").text()).toContain("回到当前：4 选择待评分文件");
    await wrapper.get(".past .text-btn").trigger("click");
    expect(store.ws.step).toBeNull();
    expect(wrapper.find(".past").exists()).toBe(false);
  });

  it("老师正忙时新结果只在顶部提示，点“查看”才切过去", async () => {
    const store = useAssistantStore();
    store.messages = messages;
    store.ws.toast = "snap-1";
    const wrapper = mount(WorkspacePanel, { props: { width: 560 }, global: { stubs } });
    expect(wrapper.get(".toast").text()).toContain("新结果：任务进展");
    await wrapper.get(".toast-open").trigger("click");
    await nextTick();
    expect(store.ws.tab).toBe("snap-1");
    expect(wrapper.get(".stub").attributes("data-view")).toBe("ProgressArtifact");
    expect(wrapper.find(".toast").exists()).toBe(false);
  });

  it("没有计划也没有结果时说明这里会显示什么；收起与全宽切换模式", async () => {
    const store = useAssistantStore();
    const wrapper = mount(WorkspacePanel, { props: { width: 560 }, global: { stubs } });
    expect(wrapper.text()).toContain("这次对话还没有结果");
    const [full, close] = wrapper.findAll(".tools .btn");
    await full.trigger("click");
    expect(store.ws.mode).toBe("focus");
    await close.trigger("click");
    expect(store.ws.mode).toBe("closed");
  });
});
