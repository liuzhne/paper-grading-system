import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";

import { useAssistantStore } from "@/stores/assistant.js";

import ChatThread from "./ChatThread.vue";

const stubs = { AssistantCard: true, EntryCard: true };

beforeEach(() => {
  setActivePinia(createPinia());
});

describe("对话", () => {
  it("老师的话发出时先显示；没发出去的标出来，点“重新发送”再发一次", async () => {
    const store = useAssistantStore();
    store.messages = [
      { id: "local-1", role: "user", text: "你好", cards: [], created_at: "2026-11-05T07:00:00Z", local: "sending" },
      { id: "local-2", role: "user", text: "有其他的评分标准吗？", cards: [], created_at: "2026-11-05T07:00:01Z", local: "failed" },
    ];
    const resend = vi.spyOn(store, "resend").mockResolvedValue(undefined);
    const wrapper = mount(ChatThread, { global: { stubs } });

    const bubbles = wrapper.findAll(".bubble").map((item) => item.text());
    expect(bubbles).toEqual(["你好", "有其他的评分标准吗？"]);
    const unsent = wrapper.findAll(".unsent");
    expect(unsent).toHaveLength(1);
    expect(unsent[0].text()).toContain("没有发出去");

    await unsent[0].find("button").trigger("click");
    expect(resend).toHaveBeenCalledWith("local-2");
  });

  it("回合进行时显示当前这一步，一步超过 5 秒加上已用秒数；没有进度时“正在整理…”", async () => {
    const store = useAssistantStore();
    store.messages = [{ id: "u1", role: "user", text: "有哪些评分标准？", cards: [], created_at: "2026-11-05T07:00:00Z" }];
    store.sending = true;
    const wrapper = mount(ChatThread, { global: { stubs } });
    expect(wrapper.find('[data-test="typing"]').text()).toBe("正在整理…");

    store.progress = { label: "正在理解你的问题…", since: Date.now() };
    await nextTick();
    expect(wrapper.find('[data-test="typing"]').text()).toBe("正在理解你的问题…");

    // 组件的“现在”每秒更新一次，取的是挂载时刻：起点往前多挪半秒，免得差几毫秒算成 6 秒。
    store.progress = { label: "正在查：可用的评分标准…", since: Date.now() - 7500 };
    await nextTick();
    expect(wrapper.find('[data-test="typing"]').text()).toBe("正在查：可用的评分标准… 7 秒");
    // 读屏只读步骤文字：秒数每秒都变，放在读屏区域外并且 aria-hidden（#79 复审）。
    expect(wrapper.find('[role="status"]').text()).toBe("正在查：可用的评分标准…");
    expect(wrapper.find('[aria-hidden="true"]:not(.avatar)').text()).toBe("7 秒");
    wrapper.unmount();
  });

  it("等待动效（设计稿“评分助手 v2”）：头像的取景框与对勾分层动，文字扫光；只在等待时出现，读屏能读到进度", async () => {
    const store = useAssistantStore();
    store.messages = [{ id: "u1", role: "user", text: "你好", cards: [], created_at: "2026-11-06T07:00:00Z" }];
    const wrapper = mount(ChatThread, { global: { stubs } });
    expect(wrapper.find(".typing").exists()).toBe(false);

    store.sending = true;
    await nextTick();
    const typing = wrapper.find(".typing");
    expect(typing.find('[role="status"]').text()).toBe("正在整理…");
    const avatar = typing.find(".avatar.yz-wait");
    expect(avatar.attributes("aria-hidden")).toBe("true");
    expect(avatar.findAll(".yz-frame path")).toHaveLength(2);
    expect(avatar.find("path.yz-check").attributes("d")).toBe("M8.2 12.4l3 3 5.2-5.8");
    expect(typing.find(".yz-wait-text").text()).toBe("正在整理…");
    // 对话里的助手头像是静止的，动效只给等待提示。
    expect(wrapper.findAll(".yz-wait")).toHaveLength(1);
    wrapper.unmount();
  });
});
