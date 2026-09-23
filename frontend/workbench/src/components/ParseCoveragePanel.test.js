import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ParseCoveragePanel from "@/components/ParseCoveragePanel.vue";

/**
 * 第 1 步列出来源冲突、未被识别的规则原文与结构辅助，让用户逐条或批量处理；
 * LLM 只在用户看过原因、范围与连接并确认后才调用。
 */
const STATE = {
  has_ledger: true,
  coverage: {
    blocking_count: 1,
    documents: [{ doc_id: "excel", doc_role: "rules", ratio: 0.8, denominator: 10, handled: 8 }],
    unclaimed: [
      { unit_id: "u1", doc_role: "rules", kind: "cell", text: "错别字每处扣1分", signals: ["score", "verb"], suspected: true, blocking: true },
      { unit_id: "u2", doc_role: "rules", kind: "cell", text: "XX大学评分表", signals: [], suspected: false, blocking: false },
    ],
  },
  triggers: [{ code: "E3", message: "存在未识别用途但有内容的列：备注", unit_ids: ["h1", "u2"] }],
  conflicts: [{ type: "word_only", anchor_unit_id: "w1", message: "Word 中的评分项“创新性”在 Excel 中不存在", resolved: false }],
  unit_classifications: { stale: false, results: [{ unit_id: "u1", label: "rule", suggested_criterion: "C02", confidence: "high", reason: "扣分条件" }] },
};
const CRITERIA = [{ code: "C01", name: "研究方法" }, { code: "C02", name: "文献综述" }];

function mountPanel(props = {}) {
  return mount(ParseCoveragePanel, {
    props: { state: STATE, criteria: CRITERIA, connections: [{ id: "c1", name: "我的连接", model_name: "m" }],
             connectionId: "c1", busy: false, editable: true, structureEstimate: null, ...props },
  });
}

describe("解析覆盖率面板", () => {
  it("第一步仅呈现来源冲突，不展示覆盖率、未认领规则或 AI 控制", async () => {
    const wrapper = mountPanel({ conflictsOnly: true });
    expect(wrapper.text()).toContain("Word 与 Excel 不一致");
    expect(wrapper.text()).toContain("创新性");
    expect(wrapper.text()).not.toContain("疑似规则");
    expect(wrapper.text()).not.toContain("覆盖率");
    expect(wrapper.text()).not.toContain("AI");
    expect(wrapper.find("[data-test=unit-u1]").exists()).toBe(false);
    expect(wrapper.find("[data-test=structure-box]").exists()).toBe(false);
    await wrapper.find("[data-test=keep-excel]").trigger("click");
    expect(wrapper.emitted("resolve")[0][0]).toMatchObject({ unitIds: ["w1"], reason: "以 Excel 为准" });
  });

  it("来源冲突已处理时第一步不再占据页面", () => {
    const wrapper = mountPanel({ conflictsOnly: true, state: { ...STATE, conflicts: [] } });
    expect(wrapper.find("[data-test=parse-coverage]").exists()).toBe(false);
  });

  it("解析核对在第 1 步完成，已无未认领内容时折叠可选 AI 辅助", () => {
    const wrapper = mountPanel();
    expect(wrapper.text()).toContain("发布前必须核对");
    expect(wrapper.text()).not.toContain("处理后才能进入第 2 步");
    const completed = mountPanel({ state: { ...STATE, coverage: { ...STATE.coverage, blocking_count: 0, unclaimed: [] }, conflicts: [] } });
    expect(completed.get("details summary").text()).toBe("可选：AI 结构辅助");
    expect(completed.get("details").attributes("open")).toBeUndefined();
    expect(completed.find("[data-test=structure-estimate]").exists()).toBe(true);
  });

  it("说明第一级门禁并列出阻断项与冲突", () => {
    const wrapper = mountPanel();
    expect(wrapper.text()).toContain("还有 1 条疑似规则、1 个冲突未处理");
    expect(wrapper.text()).toContain("80%");
    expect(wrapper.text()).toContain("创新性");
    expect(wrapper.text()).toContain("AI 建议：规则 → C02");
  });

  it("批量把选中内容标为不是规则", async () => {
    const wrapper = mountPanel();
    await wrapper.find("[data-test=unit-u1] input[type=checkbox]").setValue(true);
    await wrapper.find("[data-test=unit-u2] input[type=checkbox]").setValue(true);
    await wrapper.find("[data-test=batch-not-rule]").trigger("click");
    expect(wrapper.emitted("resolve")[0][0]).toEqual({ unitIds: ["u1", "u2"], action: "not_rule", reason: "用户确认不是评分规则" });
  });

  it("指派到评分项需要先选择评分项", async () => {
    const wrapper = mountPanel();
    const row = wrapper.find("[data-test=unit-u1]");
    expect(row.find("[data-test=assign]").attributes("disabled")).toBeDefined();
    await row.find("select").setValue("C02");
    await row.find("[data-test=assign]").trigger("click");
    expect(wrapper.emitted("resolve")[0][0]).toEqual({ unitIds: ["u1"], action: "assign", criterionCode: "C02", reason: "用户指派到评分项 C02" });
  });

  it("冲突可以以 Excel 为准，也可以指派到评分项", async () => {
    const wrapper = mountPanel();
    await wrapper.find("[data-test=conflict-w1] [data-test=keep-excel]").trigger("click");
    expect(wrapper.emitted("resolve")[0][0]).toEqual({ unitIds: ["w1"], action: "not_rule", reason: "以 Excel 为准" });
  });

  it("结构识别先估算，看到规模后才能确认调用", async () => {
    const wrapper = mountPanel();
    expect(wrapper.text()).toContain("存在未识别用途但有内容的列：备注");
    await wrapper.find("[data-test=structure-estimate]").trigger("click");
    expect(wrapper.emitted("suggest-structure")[0][0]).toEqual({ dryRun: true });
    await wrapper.setProps({ structureEstimate: { calls: 1, chars: 1200, compression: "none" } });
    expect(wrapper.text()).toContain("约 1200 字符");
    await wrapper.find("[data-test=structure-run]").trigger("click");
    expect(wrapper.emitted("suggest-structure")[1][0]).toEqual({ dryRun: false });
  });

  it("没有 AI 连接时不能调用模型，但仍可人工处理", () => {
    const wrapper = mountPanel({ connectionId: "" });
    expect(wrapper.find("[data-test=classify]").attributes("disabled")).toBeDefined();
    expect(wrapper.find("[data-test=batch-not-rule]").exists()).toBe(true);
  });

  it("用 AI 判断未处理内容时发送当前未认领单元", async () => {
    const wrapper = mountPanel();
    await wrapper.find("[data-test=classify]").trigger("click");
    expect(wrapper.emitted("classify")[0][0]).toEqual({ unitIds: ["u1", "u2"] });
  });
});
