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

  it("导入前结构预检：估算不调用模型，确认后提交识别任务", async () => {
    const record = capture({ id: "t1", status: "queued", estimate: { calls: 1 } });
    const store = useRubricsStore();
    await store.estimateImportStructure({ rulesFile: new File(["x"], "a.xlsx") });
    expect(record.calls[0].url).toMatch(/\/rubrics\/import-files\/structure-suggestions\/estimate$/);
    expect(record.calls[0].body.get("ai_connection_id")).toBeNull();
    await expect(store.startImportStructure({ rulesFile: new File(["x"], "a.xlsx"), connectionId: null })).rejects.toThrow("AI 连接");
    await store.startImportStructure({ rulesFile: new File(["x"], "a.xlsx"), connectionId: "c1" });
    expect(record.calls[1].url).toMatch(/\/ai-tasks\/import-structure$/);
    expect(record.calls[1].body.get("ai_connection_id")).toBe("c1");
    expect(record.calls[1].body.get("rules_file")).toBeInstanceOf(File);
    expect(store.importStructureTask.id).toBe("t1");
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
    await expect(store.startStructureSuggestion("r1", { connectionId: null })).rejects.toThrow("AI 连接");
    await expect(store.startRuleReview("r1", { connectionId: null, scope: "all" })).rejects.toThrow("AI 连接");
    expect(record.calls).toHaveLength(0);
    await store.estimateStructure("r1");
    await store.estimateRuleReview("r1", { scope: "priority" });
    expect(record.calls.map((c) => [c.url.replace(/^.*\/rubrics/, ""), c.body])).toEqual([
      ["/r1/structure-suggestions/estimate", {}],
      ["/r1/rule-review/estimate", { scope: "priority" }],
    ]);
  });

  it("审查与结构识别提交为 AI 任务，可刷新、停止、重试与找回", async () => {
    const record = capture({ id: "t1", kind: "rule_review", status: "running" });
    const store = useRubricsStore();
    await store.startRuleReview("r1", { connectionId: "c1", scope: "all", regenerate: true });
    await store.startStructureSuggestion("r1", { connectionId: "c1" });
    expect(record.calls.map((c) => c.body)).toEqual([
      { kind: "rule_review", params: { scope: "all" }, ai_connection_id: "c1", regenerate: true },
      { kind: "structure_suggestion", params: { target: "draft" }, ai_connection_id: "c1", regenerate: false },
    ]);
    await store.refreshAiTask("review");
    await store.aiTaskAction("review", "cancel");
    await store.aiTaskAction("structure", "retry");
    expect(record.calls.slice(2).map((c) => [c.method, c.url.replace(/^.*\/api/, "")])).toEqual([
      ["GET", "/ai-tasks/t1"], ["POST", "/ai-tasks/t1/cancel"], ["POST", "/ai-tasks/t1/retry"],
    ]);
  });

  it("进入评分标准页时按种类找回进行中的审查与结构识别任务", async () => {
    vi.stubGlobal("fetch", () => jsonResponse([
      { id: "a", kind: "unit_classification", status: "running" },
      { id: "b", kind: "rule_review", status: "running" },
      { id: "c", kind: "structure_suggestion", status: "queued" },
    ]));
    const store = useRubricsStore();
    const found = await store.resumeReviewTasks("r1");
    expect([found.review.id, found.structure.id]).toEqual(["b", "c"]);
    store.reset();
    expect([store.ruleReviewTask, store.structureTask, store.importStructureTask]).toEqual([null, null, null]);
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
