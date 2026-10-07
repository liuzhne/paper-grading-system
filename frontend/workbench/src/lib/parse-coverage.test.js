import { describe, expect, it } from "vitest";

import {
  diffGroups,
  importFilesError,
  mergeSelection,
  stepOneGate,
  suggestionFor,
  triggerSummary,
} from "@/lib/parse-coverage.js";

/**
 * 解析台账（解析重构方案 §8）：第一级门禁在第 1 步阻断进入第 2 步，
 * 发布时后端再做底线校验。LLM 建议的合入规则与后端 merge_plan 保持一致。
 */
const coverage = (overrides = {}) => ({
  has_ledger: true,
  coverage: { blocking_count: 1, unclaimed: [], documents: [] },
  conflicts: [{ anchor_unit_id: "a", resolved: false }, { anchor_unit_id: "b", resolved: true }],
  ...overrides,
});

describe("第一级门禁", () => {
  it("来源冲突阻止进入第 2 步，疑似规则在第 2 步处理", () => {
    expect(stepOneGate(coverage())).toEqual({ blocked: true, blocking: 1, conflicts: 1 });
    expect(stepOneGate(coverage({conflicts:[]}))).toEqual({blocked:false, blocking:1, conflicts:0});
  });

  it("全部处理后放行；没有台账的草稿不受影响", () => {
    expect(stepOneGate(coverage({ coverage: { blocking_count: 0 }, conflicts: [] })).blocked).toBe(false);
    expect(stepOneGate({ has_ledger: false }).blocked).toBe(false);
    expect(stepOneGate(null).blocked).toBe(false);
  });
});

describe("导入文件校验", () => {
  it("至少上传一份，且类型正确", () => {
    expect(importFilesError(null, null)).toBe("请至少上传一份评分标准文件（Word 或 Excel）。");
    expect(importFilesError({ name: "a.docx" }, null)).toMatch("评分表需为 .xlsx 或 .xlsm");
    expect(importFilesError(null, { name: "a.pdf" })).toMatch("评分标准文档需为 .docx");
    expect(importFilesError({ name: "a.XLSX" }, null)).toBeNull();
    expect(importFilesError(null, { name: "a.docx" })).toBeNull();
  });
});

describe("触发条件提示", () => {
  it("汇总原因与将发送的单元数", () => {
    const summary = triggerSummary([
      { code: "E3", message: "存在未识别用途的列", unit_ids: ["a", "b"] },
      { code: "E6", message: "覆盖率低", unit_ids: ["b", "c"] },
    ]);
    expect(summary).toEqual({ codes: ["E3", "E6"], messages: ["存在未识别用途的列", "覆盖率低"], unitCount: 3 });
    expect(triggerSummary([])).toEqual({ codes: [], messages: [], unitCount: 0 });
  });
});

const ITEMS = [
  { id: "new:C09:row7", kind: "new", row_number: 7 },
  { id: "fill:C01:description", kind: "fill", field: "description", row_number: 4, before: null },
  { id: "modify:C02:max_score", kind: "modify", field: "max_score", row_number: 5, before: 10 },
  { id: "conflict:C03:row8", kind: "conflict", row_number: 8 },
  { id: "removed:C04", kind: "removed" },
];

describe("结构建议差异", () => {
  it("按合入方式分组", () => {
    const groups = diffGroups(ITEMS);
    expect(groups.oneClick.map((i) => i.id)).toEqual(["new:C09:row7", "fill:C01:description"]);
    expect(groups.confirmable.map((i) => i.id)).toEqual(["modify:C02:max_score"]);
    expect(groups.conflicts.map((i) => i.id)).toEqual(["conflict:C03:row8"]);
    expect(groups.removed.map((i) => i.id)).toEqual(["removed:C04"]);
  });

  it("移除项始终阻断；冲突须排除；未确认的修改保留当前值", () => {
    const blocked = mergeSelection(ITEMS, new Set(), new Set());
    expect(blocked.blocked).toEqual(["conflict:C03:row8", "removed:C04"]);
    const withoutRemoval = ITEMS.filter((i) => i.kind !== "removed");
    const plan = mergeSelection(withoutRemoval, new Set(), new Set(["conflict:C03:row8"]));
    expect(plan.blocked).toEqual([]);
    expect(plan.keptCurrent).toEqual(["modify:C02:max_score"]);
    expect(plan.payload).toEqual({ confirm: [], exclude: ["conflict:C03:row8"] });
    const confirmed = mergeSelection(withoutRemoval, new Set(["modify:C02:max_score"]), new Set(["conflict:C03:row8"]));
    expect(confirmed.keptCurrent).toEqual([]);
    expect(confirmed.payload.confirm).toEqual(["modify:C02:max_score"]);
  });
});

describe("分类建议", () => {
  it("只在建议未过期时展示", () => {
    const state = { unit_classifications: { stale: false, results: [{ unit_id: "u1", label: "rule" }] } };
    expect(suggestionFor(state, "u1")).toEqual({ unit_id: "u1", label: "rule" });
    expect(suggestionFor(state, "u2")).toBeNull();
    expect(suggestionFor({ unit_classifications: { stale: true, results: [{ unit_id: "u1" }] } }, "u1")).toBeNull();
    expect(suggestionFor(null, "u1")).toBeNull();
  });
});
