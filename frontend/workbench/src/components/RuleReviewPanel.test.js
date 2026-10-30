import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import RuleReviewPanel from "@/components/RuleReviewPanel.vue";

const RULES = [
  { id: "source-1", rule_code: "T01.source", name: "原文规则", rule_text: "缺少依据扣 2 分", direction: "deduct", max_points: 2, status: "draft", creation_method: "compiler", origin: {} },
  { id: "ai-1", rule_code: "T01.ai", name: "AI 规则", rule_text: "没有依据扣 4 分", direction: "deduct", max_points: 4, status: "draft", creation_method: "manual", origin: { source: "ai_interpreted_user_text" } },
];

function mountPanel(props = {}) {
  return mount(RuleReviewPanel, {
    props: {
      criterion: { code: "T01", name: "论证" },
      rules: RULES,
      busy: false,
      editable: true,
      ...props,
    },
  });
}

describe("最终规则统一确认", () => {
  it("AI 建议尚未处理时暂停原文规则确认", () => {
    const wrapper = mountPanel({ deferConfirmation: true });

    expect(wrapper.find('[data-test="deferred-confirmation"]').text()).toContain("形成最终规则集合后");
    expect(wrapper.findAll("button.confirm").every((button) => button.attributes("disabled") !== undefined)).toBe(true);
    expect(wrapper.find(".review-foot .btn-primary").attributes("disabled")).toBeDefined();
  });

  it("最终集合形成后一次提交原文规则和 AI 规则", async () => {
    const wrapper = mountPanel();

    await wrapper.find(".review-foot .btn-primary").trigger("click");

    expect(wrapper.emitted("confirm-all")).toHaveLength(1);
    expect(wrapper.emitted("confirm-all")[0][0].map((rule) => rule.id)).toEqual(["source-1", "ai-1"]);
    expect(wrapper.find(".review-foot .btn-primary").text()).toContain("统一确认最终规则（2）");
    expect(wrapper.text()).toContain("AI 解读原文");
  });
});

describe("排除即时保存", () => {
  it("排除与恢复交给调用方落库；排除的细则不算待确认，显示“已排除 · 恢复”", async () => {
    const rules = [{ ...RULES[0] }, { ...RULES[1], status: "rejected" }];
    const wrapper = mountPanel({ rules });

    expect(wrapper.find(".summary").text()).toBe("2 条 · 已确认 0 · 待确认 1 · 已排除 1");
    expect(wrapper.find('[data-test="excluded"]').text()).toContain("已排除");
    expect(wrapper.find(".review-foot .btn-primary").text()).toContain("统一确认最终规则（1）");

    await wrapper.find('[data-test="restore"]').trigger("click");
    expect(wrapper.emitted("restore")[0][0].id).toBe("ai-1");
    // 本项只剩 1 条参与评分的细则：不能再排除。
    expect(wrapper.find('[data-test="exclude"]').attributes("disabled")).toBeDefined();
    expect(wrapper.find('[data-test="exclude"]').attributes("title")).toBe("每个评分项至少保留 1 条细则");
  });

  it("还有其它细则时可以排除", async () => {
    const wrapper = mountPanel();

    await wrapper.findAll('[data-test="exclude"]')[1].trigger("click");

    expect(wrapper.emitted("exclude")[0][0].id).toBe("ai-1");
  });
});
