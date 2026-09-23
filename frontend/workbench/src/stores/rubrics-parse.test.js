import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useRubricsStore } from "@/stores/rubrics.js";

/**
 * 解析台账相关接口（解析重构方案 §8–§9）：只上传 Word、按确认结构导入、
 * 处理未认领单元、LLM 建议（分类 / 结构 / 审查）与合入撤销。
 */
function jsonResponse(body, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

/** @returns {{calls: Array<{url: string, method: string, body: any}>}} */
function capture(body = {}) {
  const record = { calls: [] };
  vi.stubGlobal("fetch", (url, init) => {
    const raw = init?.body;
    record.calls.push({ url: String(url), method: init?.method || "GET",
      body: raw instanceof FormData ? raw : raw ? JSON.parse(raw) : undefined });
    return jsonResponse(body);
  });
  return record;
}

describe("导入：Word / Excel 至少一份", () => {
  beforeEach(() => { setActivePinia(createPinia()); vi.restoreAllMocks(); });

  it("只上传 Word 时不附带 rules_file", async () => {
    const record = capture({ rubric: { id: "r1" }, warnings: [], template_summary: {}, coverage: { blocking_count: 2 } });
    const store = useRubricsStore();
    await store.importFiles({ name: "x", version: "v1", templateFile: new File(["x"], "标准.docx") });
    const form = record.calls[0].body;
    expect(form.get("rules_file")).toBeNull();
    expect(form.get("template_file")).toBeInstanceOf(File);
    expect(store.lastImport.coverage).toEqual({ blocking_count: 2 });
  });

  it("两份都没有时不发请求", async () => {
    const record = capture();
    await expect(useRubricsStore().importFiles({ name: "x", version: "v1" })).rejects.toThrow("至少上传一份");
    expect(record.calls).toHaveLength(0);
  });

  it("按确认结构导入时带上 structure_override", async () => {
    const record = capture({ rubric: { id: "r1" } });
    const override = { sheet: "Sheet", header_row: 1, column_mapping: { name: 1, max_score: 2 } };
    await useRubricsStore().importFiles({ name: "x", version: "v1", rulesFile: new File(["x"], "a.xlsx"), structureOverride: override });
    expect(JSON.parse(record.calls[0].body.get("structure_override"))).toEqual(override);
  });

  it("导入前结构预检：先估算、确认后才调用模型", async () => {
    const record = capture({ estimate: { calls: 1 } });
    const store = useRubricsStore();
    await store.previewImportStructure({ rulesFile: new File(["x"], "a.xlsx"), connectionId: "c1", dryRun: true });
    const form = record.calls[0].body;
    expect(record.calls[0].url).toMatch(/\/rubrics\/import-files\/structure-suggestions$/);
    expect(form.get("dry_run")).toBe("true");
    expect(form.get("ai_connection_id")).toBe("c1");
  });
});

describe("未认领单元与 LLM 建议", () => {
  beforeEach(() => { setActivePinia(createPinia()); vi.restoreAllMocks(); });

  it("单元处理统一走批量接口", async () => {
    const record = capture({ units: [], coverage: {} });
    await useRubricsStore().resolveUnits("r1", { unitIds: ["u1", "u2"], action: "not_rule", reason: "批量忽略" });
    expect(record.calls[0].url).toMatch(/\/rubrics\/r1\/units\/resolve-batch$/);
    expect(record.calls[0].body).toEqual({ unit_ids: ["u1", "u2"], action: "not_rule", reason: "批量忽略" });
  });

  it("指派到评分项时必须带评分项编号", async () => {
    const record = capture();
    await expect(useRubricsStore().resolveUnits("r1", { unitIds: ["u1"], action: "assign", reason: "x" }))
      .rejects.toThrow("请选择评分项");
    expect(record.calls).toHaveLength(0);
  });

  it("LLM 调用必须先选定自己的 AI 连接（D-027），估算除外", async () => {
    const record = capture({});
    const store = useRubricsStore();
    await expect(store.classifyUnits("r1", { unitIds: ["u1"], connectionId: null })).rejects.toThrow("AI 连接");
    await expect(store.suggestStructure("r1", { connectionId: null })).rejects.toThrow("AI 连接");
    await expect(store.runRuleReview("r1", { connectionId: null, scope: "all" })).rejects.toThrow("AI 连接");
    expect(record.calls).toHaveLength(0);
    await store.suggestStructure("r1", { connectionId: null, dryRun: true });
    await store.runRuleReview("r1", { connectionId: null, scope: "priority", dryRun: true });
    expect(record.calls.map((c) => c.body)).toEqual([{ dry_run: true }, { scope: "priority", dry_run: true }]);
  });

  it("结构建议合入与撤销", async () => {
    const record = capture({ rubric: { id: "r1" } });
    const store = useRubricsStore();
    await store.mergeStructure("r1", { fingerprint: "f", confirm: ["modify:C02:max_score"], exclude: [], reason: "合入" });
    await store.undoStructure("r1", "撤销");
    expect(record.calls.map((c) => [c.url.replace(/^.*\/rubrics/, ""), c.body])).toEqual([
      ["/r1/suggestions/merge", { fingerprint: "f", confirm: ["modify:C02:max_score"], exclude: [], reason: "合入" }],
      ["/r1/suggestions/undo", { reason: "撤销" }],
    ]);
  });

  it("豁免审查问题需要原因", async () => {
    const record = capture({});
    const store = useRubricsStore();
    await expect(store.dismissFinding("r1", "F1", " ")).rejects.toThrow("原因");
    await store.dismissFinding("r1", "F1", "已知可接受");
    expect(record.calls[0].url).toMatch(/\/rubrics\/r1\/rule-review\/findings\/F1\/dismiss$/);
  });
});
