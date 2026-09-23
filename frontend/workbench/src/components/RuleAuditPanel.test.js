import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import RuleAuditPanel from "@/components/RuleAuditPanel.vue";

/** 规则审查（第 3 步）：先估算再运行；只报告不修改；可豁免但须写原因；可跳过但发布时留痕。 */
const REVIEW = {
  reviewed: true, stale: false,
  prechecks: [{ code: "TOTAL_MISMATCH", message: "评分项满分合计 65 与总分 70 不一致" }],
  findings: [
    { id: "F1", criterion_code: "C01", type: "granularity", severity: "high", quote: "各扣2分", problem: "只拆出一条", example: "", status: "open" },
    { id: "F2", criterion_code: "C02", type: "match_too_broad", severity: "low", quote: "数据", problem: "过宽", example: "数据格式也命中", status: "dismissed" },
  ],
};

const mountPanel = (props = {}) => mount(RuleAuditPanel, {
  props: { review: { reviewed: false }, estimate: null, busy: false, editable: true, connectionId: "c1", ...props },
});

describe("规则审查面板", () => {
  it("未审查时说明可以跳过但会留痕", () => {
    expect(mountPanel().text()).toContain("发布时会记录“未经审查即发布”");
  });

  it("先估算，看到规模后再运行", async () => {
    const wrapper = mountPanel();
    await wrapper.find("[data-test=review-estimate]").trigger("click");
    expect(wrapper.emitted("estimate")[0][0]).toEqual({ scope: "priority" });
    await wrapper.setProps({ estimate: { criteria_codes: ["C01"], estimate: { calls: 1, chars: 300 }, prechecks: [] } });
    await wrapper.find("[data-test=review-run]").trigger("click");
    expect(wrapper.emitted("run")[0][0]).toEqual({ scope: "priority" });
  });

  it("展示前置检查与问题，已豁免的标明状态", () => {
    const wrapper = mountPanel({ review: REVIEW });
    expect(wrapper.text()).toContain("评分项满分合计 65 与总分 70 不一致");
    expect(wrapper.text()).toContain("各扣2分");
    expect(wrapper.find("[data-test=finding-F2]").text()).toContain("已豁免");
    expect(wrapper.find("[data-test=finding-F2] [data-test=dismiss]").exists()).toBe(false);
  });

  it("豁免需要填写原因", async () => {
    const wrapper = mountPanel({ review: REVIEW });
    const row = wrapper.find("[data-test=finding-F1]");
    expect(row.find("[data-test=dismiss]").attributes("disabled")).toBeDefined();
    await row.find("input").setValue("已知且可接受");
    await row.find("[data-test=dismiss]").trigger("click");
    expect(wrapper.emitted("dismiss")[0][0]).toEqual({ id: "F1", reason: "已知且可接受" });
  });

  it("审查过期时提示重新运行", () => {
    expect(mountPanel({ review: { ...REVIEW, stale: true } }).text()).toContain("规则已修改，审查结果已过期");
  });
});
