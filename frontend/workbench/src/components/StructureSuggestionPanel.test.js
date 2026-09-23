import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import StructureSuggestionPanel from "@/components/StructureSuggestionPanel.vue";

/** 结构建议差异：新增/补全一键合入，修改逐条确认，冲突须排除，移除一律阻断。 */
const BASE = {
  status: "pending", stale: false, fingerprint: "f",
  items: [
    { id: "new:C09:row7", kind: "new", code: "C09", row_number: 7, after: { name: "创新点", max_score: 10 } },
    { id: "fill:C01:description", kind: "fill", code: "C01", field: "description", before: null, after: "选题新颖" },
    { id: "modify:C02:max_score", kind: "modify", code: "C02", field: "max_score", before: 10, after: 15 },
    { id: "conflict:C03:row8", kind: "conflict", code: "C03", row_number: 8, reason: "code_collision", after: { name: "写作" } },
  ],
};

const mountPanel = (suggestion = BASE) => mount(StructureSuggestionPanel, { props: { suggestion, busy: false, editable: true } });

describe("结构建议面板", () => {
  it("有未排除的冲突时不能合入；排除后按选择提交", async () => {
    const wrapper = mountPanel();
    const merge = wrapper.find("[data-test=merge]");
    expect(merge.attributes("disabled")).toBeDefined();
    await wrapper.find("[data-test=item-conflict\\:C03\\:row8] input").setValue(true);
    await wrapper.find("[data-test=item-modify\\:C02\\:max_score] input").setValue(true);
    expect(wrapper.find("[data-test=merge]").attributes("disabled")).toBeUndefined();
    await wrapper.find("[data-test=merge]").trigger("click");
    expect(wrapper.emitted("merge")[0][0]).toEqual({ confirm: ["modify:C02:max_score"], exclude: ["conflict:C03:row8"] });
  });

  it("未确认的修改会提示保留当前值", async () => {
    const wrapper = mountPanel({ ...BASE, items: BASE.items.filter((i) => i.kind !== "conflict") });
    expect(wrapper.text()).toContain("1 处修改未确认，将保留当前值");
  });

  it("移除已有评分项的结构无法合入", () => {
    const wrapper = mountPanel({ ...BASE, items: [...BASE.items.slice(0, 2), { id: "removed:C05", kind: "removed", code: "C05", before: { name: "旧项" } }] });
    expect(wrapper.find("[data-test=merge]").attributes("disabled")).toBeDefined();
    expect(wrapper.text()).toContain("会移除已有评分项");
  });

  it("过期的建议不能合入", () => {
    const wrapper = mountPanel({ ...BASE, stale: true });
    expect(wrapper.text()).toContain("已过期");
    expect(wrapper.find("[data-test=merge]").attributes("disabled")).toBeDefined();
  });

  it("已合入的建议可以撤销", async () => {
    const wrapper = mountPanel({ ...BASE, status: "merged" });
    await wrapper.find("[data-test=undo]").trigger("click");
    expect(wrapper.emitted("undo")).toHaveLength(1);
  });
});
