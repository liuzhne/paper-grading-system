import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import AiRuleDraftPanel from "@/components/AiRuleDraftPanel.vue";

/**
 * AI 起草建议的确认面板（设计稿「T03 · 扣分规则」区块，V3-2）。
 *
 * 面板本身不落库：起草端点是 non-persistent 的，确认之后由调用方走 `recompile`。
 * 因此这里只负责「让用户看清每一条、能逐条排除、并把最终集合交出去」。
 */
const DRAFT_ITEMS = [
  {
    criterion_code: "T02",
    status: "pending_confirmation",
    draft: {
      criterion_code: "T02",
      rule_groups: [
        {
          group_code: "G1",
          issue: "文献综述覆盖不足",
          mutex_group: "T02-coverage",
          cap_points: 6,
          rules: [
            {
              severity: "minor",
              trigger: "引用文献少于 15 篇",
              points: 2,
              reason: "覆盖面偏窄",
              source: "ai_inferred",
              source_refs: ["/criteria/T02/deduction_rules/0"],
            },
            {
              severity: "severe",
              trigger: "缺少近三年文献",
              points: 5,
              reason: "时效性不足",
              source: "user_text",
              source_refs: ["/criteria/T02/deduction_rules/1"],
            },
          ],
        },
      ],
      generation_metadata: {
        provider: "openai_compatible",
        model_name: "glm-4",
        fingerprint: "f".repeat(64),
      },
    },
  },
];

function mountPanel(items = DRAFT_ITEMS, props = {}) {
  return mount(AiRuleDraftPanel, {
    props: { items, busy: false, ...props },
  });
}

describe("AI 起草确认面板", () => {
  it("没有起草结果时整块不渲染", () => {
    const wrapper = mountPanel([]);

    expect(wrapper.find("[data-test=draft-panel]").exists()).toBe(false);
  });

  it("每条规则一行，列出问题类型、严重程度、扣分、触发条件与来源", () => {
    const wrapper = mountPanel();

    const rows = wrapper.findAll("tbody tr");
    expect(rows).toHaveLength(2);
    const text = rows[0].text();
    expect(text).toContain("文献综述覆盖不足");
    expect(text).toContain("轻微");
    expect(text).toContain("引用文献少于 15 篇");
    expect(text).toContain("AI 起草");
  });

  it("标明这些规则未确认前不进入可执行版本", () => {
    const wrapper = mountPanel();

    expect(wrapper.text()).toContain("未确认");
  });

  it("公开模型与指纹——确认记录要追得回是哪一次生成", () => {
    const wrapper = mountPanel();

    expect(wrapper.text()).toContain("glm-4");
  });

  it("排除一行后它显示为已排除，且不再计入待应用条数", () => {
    const wrapper = mountPanel();

    expect(wrapper.find("[data-test=apply]").text()).toContain("2");

    return wrapper
      .findAll("[data-test=exclude]")[0]
      .trigger("click")
      .then(() => {
        expect(wrapper.find("[data-test=apply]").text()).toContain("1");
        expect(wrapper.findAll("tbody tr")[0].text()).toContain("已排除");
      });
  });

  it("排除可以撤销——点错一下不该逼用户重新生成", async () => {
    const wrapper = mountPanel();

    await wrapper.findAll("[data-test=exclude]")[0].trigger("click");
    await wrapper.findAll("[data-test=exclude]")[0].trigger("click");

    expect(wrapper.find("[data-test=apply]").text()).toContain("2");
  });

  it("应用时把未排除的行键交出去，由调用方负责落库", async () => {
    const wrapper = mountPanel();

    await wrapper.findAll("[data-test=exclude]")[0].trigger("click");
    await wrapper.find("[data-test=apply]").trigger("click");

    const emitted = wrapper.emitted("apply");
    expect(emitted).toHaveLength(1);
    // 交出的是「被排除的行」，合并逻辑在 ai-draft.js 里，面板不重复实现一遍。
    expect([...emitted[0][0]]).toEqual(["T02::G1::0"]);
  });

  it("全部排除后不能应用——那会产出一个没有任何规则的 deductive 评分项", async () => {
    const wrapper = mountPanel();

    for (const button of wrapper.findAll("[data-test=exclude]")) {
      await button.trigger("click");
    }

    expect(wrapper.find("[data-test=apply]").attributes("disabled")).toBeDefined();
  });

  it("提交进行中时禁用应用按钮，避免重复生成执行草稿", () => {
    const wrapper = mountPanel(DRAFT_ITEMS, { busy: true });

    expect(wrapper.find("[data-test=apply]").attributes("disabled")).toBeDefined();
  });

  it("已经结构化、无需起草的评分项不占一行", () => {
    const wrapper = mountPanel([
      { criterion_code: "T01", status: "already_structured", draft: null },
      ...DRAFT_ITEMS,
    ]);

    expect(wrapper.findAll("tbody tr")).toHaveLength(2);
  });
});
