import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import RubricImportWorkspace from "@/components/RubricImportWorkspace.vue";


const session = {
  id: "s1",
  status: "draft",
  state_version: 1,
  name: "课程报告评分标准",
  version: "v1",
  description: "",
  total_score: 21,
  criteria: [
    { code: "C01", name: "选题与意义", max_score: 10, source_refs: [{ locator: "B2:C2" }], parse_status: "parsed" },
    { code: "C02", name: "文献综述", max_score: 11, source_refs: [{ locator: "B3:C3" }], parse_status: "rounded" },
  ],
  score_adjustments: [{ code: "C02", original: "10.5", rounded: 11, message: "C02 分值 10.5 已按四舍五入调整为 11。" }],
  conflicts: [],
};


describe("导入评分模板与评分项工作区", () => {
  it("点击取整提示能定位对应评分项，合计错误能定位满分", async () => {
    const wrapper = mount(RubricImportWorkspace, { props: { session: { ...session, total_score: 100 } }, attachTo: document.body });
    await wrapper.get('[data-test="rounding-C02"]').trigger("click");
    expect(document.activeElement.getAttribute("aria-label")).toBe("C02 满分");
    await wrapper.get('[data-test="fix-total"]').trigger("click");
    expect(document.activeElement.getAttribute("aria-label")).toBe("标准满分");
    wrapper.unmount();
  });
  it("初始名称在选择文件触发重绘之前就同步，避免输入被清空", async () => {
    const wrapper = mount(RubricImportWorkspace, { props: { session: { name: "", criteria: [] }, initial: true } });
    const input = wrapper.get('[aria-label="标准名称"]');
    input.element.value = "新标准";
    await input.trigger("input");
    expect(wrapper.emitted("update-session")?.[0]?.[0]).toEqual({ name: "新标准" });
  });
  it("已确认标准复用同一工作区，但不显示取消导入或允许修改稳定编号", () => {
    const wrapper = mount(RubricImportWorkspace, { props: { session, persisted: true } });
    expect(wrapper.text()).not.toContain("取消导入");
    expect(wrapper.get("input.code").attributes("disabled")).toBeDefined();
    expect(wrapper.get('[data-test="confirm-import-session"]').text()).toBe("保存评分项，下一步");
  });

  it("预览替换未确认、空名称、非整数分值均不能继续", () => {
    for (const props of [
      { session, reuploadPreview: { criteria_diff: [] } },
      { session: { ...session, name: " " } },
      { session: { ...session, total_score: 21.5, criteria: [{ code: "C01", name: "项", max_score: 21.5 }] } },
    ]) {
      const wrapper = mount(RubricImportWorkspace, { props });
      expect(wrapper.get('[data-test="confirm-import-session"]').attributes("disabled")).toBeDefined();
    }
  });

  it("Excel 拖放触发和文件选择相同的操作", async () => {
    const wrapper = mount(RubricImportWorkspace, { props: { session } });
    const file = new File(["test"], "rules.xlsx");
    await wrapper.get('[data-test="excel-dropzone"]').trigger("drop", { dataTransfer: { files: [file] } });
    expect(wrapper.emitted("pick-rules")[0][0].target.files[0]).toBe(file);
  });

  it("初始状态在有名称和文件后允许解析，空评分项不会阻塞首次上传", async () => {
    const wrapper = mount(RubricImportWorkspace, {
      props: { session: { ...session, criteria: [], score_adjustments: [] }, initial: true },
    });
    expect(wrapper.text()).toContain("上传文件后，解析出的评分项会显示在这里");
    expect(wrapper.get('[data-test="confirm-import-session"]').text()).toBe("解析文件");
    expect(wrapper.get('[data-test="confirm-import-session"]').attributes("disabled")).toBeDefined();
    await wrapper.setProps({ templateFile: new File(["test"], "standard.docx") });
    expect(wrapper.get('[data-test="confirm-import-session"]').attributes("disabled")).toBeUndefined();
    expect(wrapper.text()).not.toContain("解析成功");
  });

  it("只读标准可预览与查看下一步，但不能编辑、重传、增删或拖放", async () => {
    const wrapper = mount(RubricImportWorkspace, {
      props: { session: { ...session, files: { template: "standard.docx" } }, persisted: true, readonly: true },
    });
    expect(wrapper.get("input.name").attributes("disabled")).toBeDefined();
    expect(wrapper.find('input[type="file"]').exists()).toBe(false);
    expect(wrapper.text()).not.toContain("新增评分项");
    expect(wrapper.text()).not.toContain("删除");
    const next = wrapper.get('[data-test="confirm-import-session"]');
    expect(next.text()).toBe("查看评分规则");
    expect(next.attributes("disabled")).toBeUndefined();
    await wrapper.get('[data-test="excel-dropzone"]').trigger("drop", { dataTransfer: { files: [new File(["test"], "rules.xlsx")] } });
    expect(wrapper.emitted("pick-rules")).toBeUndefined();
    await wrapper.findAll("button").find((button) => button.text() === "预览原文").trigger("click");
    expect(wrapper.emitted("preview-source")[0][0]).toBe("word");
  });

  it("评分项编号、名称和整数分值缺失或重复时阻止继续", () => {
    for (const criteria of [
      [{ code: " ", name: "项", max_score: 21 }],
      [{ code: "C01", name: " ", max_score: 21 }],
      [{ code: "C01", name: "项", max_score: 0 }],
      [{ code: "C01", name: "项", max_score: 10 }, { code: " C01 ", name: "项二", max_score: 11 }],
    ]) {
      const wrapper = mount(RubricImportWorkspace, { props: { session: { ...session, criteria } } });
      expect(wrapper.get('[data-test="confirm-import-session"]').attributes("disabled")).toBeDefined();
    }
  });

  it("以可读定位呈现来源，未知来源不冒充人工新增，并接纳父页面分析和问题", () => {
    const wrapper = mount(RubricImportWorkspace, {
      props: { session: { ...session, criteria: [
        { code: "C01", name: "项", max_score: 10, source_refs: [{ locator: { sheet: "评分表", cell: "B2" } }] },
        { code: "C02", name: "项二", max_score: 11, source_refs: [] },
      ] } },
      slots: { analysis: "解析分析", issues: "待处理问题" },
    });
    expect(wrapper.get('[aria-label="C01 文档出处"]').text()).toBe("评分表 · B2");
    expect(wrapper.get('[aria-label="C02 文档出处"]').text()).toBe("来源待补充");
    expect(wrapper.text()).toContain("解析分析");
    expect(wrapper.text()).toContain("待处理问题");
    expect(wrapper.get(".more-info summary").text()).toBe("更多信息");
  });

  it("呈现截图约定的双文件、评分项、提示和主操作，不显示类型选择", () => {
    const wrapper = mount(RubricImportWorkspace, {
      props: { session, busy: false, rulesFile: null, templateFile: null },
    });

    expect(wrapper.text()).toContain("评分标准文档");
    expect(wrapper.text()).toContain("评分表");
    expect(wrapper.text()).toContain("解析出的评分项");
    expect(wrapper.find("input.name").element.value).toBe("选题与意义");
    expect(wrapper.text()).toContain("10.5 已按四舍五入调整为 11");
    expect(wrapper.get('[data-test="confirm-import-session"]').text()).toContain("确认评分项，下一步");
    expect(wrapper.text()).not.toContain("适用类型");
    expect(wrapper.text()).not.toContain("编程作业");
  });

  it("合并父级解析后按维度、子项和具体要求分层展示", () => {
    const wrapper = mount(RubricImportWorkspace, {
      props: { session: { ...session, criteria: [{
        code: "T02",
        name: "指导教师成绩项2",
        dimension: "分析与解决问题",
        description: "能够检索并分析相关研究现状。",
        max_score: 20,
      }] } },
    });

    expect(wrapper.get(".criterion-dimension").text()).toBe("分析与解决问题");
    expect(wrapper.get(".criterion-name-row").text()).toContain("子项");
    expect(wrapper.get("input.name").element.value).toBe("指导教师成绩项2");
    expect(wrapper.get(".criterion-description summary").text()).toBe("能够检索并分析相关研究现状。");
  });

  it("分值合计与满分不一致时禁用确认", () => {
    const wrapper = mount(RubricImportWorkspace, {
      props: { session: { ...session, total_score: 100 }, busy: false, rulesFile: null, templateFile: null },
    });

    expect(wrapper.text()).toContain("评分项合计 21 分，与满分 100 分不一致");
    expect(wrapper.get('[data-test="confirm-import-session"]').attributes("disabled")).toBeDefined();
  });

  it("展示原文预览和重新上传差异，并要求显式确认替换", async () => {
    const wrapper = mount(RubricImportWorkspace, {
      props: {
        session,
        sourcePreview: { document: "excel", items: [{ unit_id: "u1", locator: { sheet: "评分表" }, text: "原文内容" }] },
        reuploadPreview: {
          fingerprint: "fp1",
          criteria_diff: [{ code: "C01", name: "选题与意义", change_type: "modified", old_max_score: 10, new_max_score: 12 }],
        },
      },
    });

    expect(wrapper.text()).toContain("原文内容");
    expect(wrapper.text()).toContain("重新上传差异");
    expect(wrapper.text()).toContain("10 → 12");
    await wrapper.get('[data-test="confirm-reupload"]').trigger("click");
    expect(wrapper.emitted("confirm-reupload")).toHaveLength(1);
  });

  it("来源冲突提供明确的 Excel 和 Word 决策", async () => {
    const conflictSession = { ...session, conflicts: [{ anchor_unit_id: "u1", type: "score_mismatch", message: "分值冲突" }] };
    const wrapper = mount(RubricImportWorkspace, { props: { session: conflictSession } });

    await wrapper.get('[data-test="resolve-use-excel"]').trigger("click");
    await wrapper.get('[data-test="resolve-use-word"]').trigger("click");

    expect(wrapper.emitted("resolve-conflict")[0][0]).toMatchObject({ decision: "use_excel" });
    expect(wrapper.emitted("resolve-conflict")[1][0]).toMatchObject({ decision: "use_word" });
  });
});
