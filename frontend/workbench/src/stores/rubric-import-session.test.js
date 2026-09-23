import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useRubricsStore } from "@/stores/rubrics.js";


function response(body, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  }));
}


describe("评分标准临时导入会话", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it("解析只创建会话，不把结果当作 Rubric", async () => {
    const calls = [];
    vi.stubGlobal("fetch", (url, init) => {
      calls.push({ url: String(url), body: init.body });
      return response({
        id: "s1", status: "draft", state_version: 1, rubric_id: null,
        criteria: [{ code: "C01", name: "内容", max_score: 10 }],
        total_score: 10, warnings: [], score_adjustments: [],
      }, 201);
    });
    const store = useRubricsStore();

    const result = await store.createImportSession({
      name: "课程报告评分标准",
      version: "v1",
      rulesFile: new File(["x"], "评分表.xlsx"),
    });

    expect(calls[0].url).toMatch(/\/rubrics\/import-sessions$/);
    expect(calls[0].body).toBeInstanceOf(FormData);
    expect(store.activeImportSession.id).toBe("s1");
    expect(store.lastImport.rubricId).toBeNull();
    expect(result.rubric_id).toBeNull();
  });

  it("确认时携带状态版本和幂等键", async () => {
    const calls = [];
    vi.stubGlobal("fetch", (url, init) => {
      calls.push({ url: String(url), body: init.body && JSON.parse(init.body) });
      return response({ status: "confirmed", import_session_id: "s1", state_version: 2,
        rubric: { id: "r1", name: "课程报告评分标准" } });
    });
    const store = useRubricsStore();
    store.activeImportSession = { id: "s1", state_version: 1 };

    const result = await store.confirmImportSession("confirm-s1");

    expect(calls[0]).toEqual(expect.objectContaining({
      url: expect.stringMatching(/\/rubrics\/import-sessions\/s1\/confirm$/),
      body: { expected_state_version: 1, idempotency_key: "confirm-s1" },
    }));
    expect(result.rubric.id).toBe("r1");
    expect(store.activeImportSession).toBeNull();
    expect(store.lastImport.rubricId).toBe("r1");
  });

  it("重新上传先预览差异，确认后才替换活动会话", async () => {
    const calls = [];
    vi.stubGlobal("fetch", (url, init) => {
      calls.push({ url: String(url), body: init.body });
      if (String(url).endsWith("/reupload-preview")) {
        return response({ fingerprint: "fp1", criteria_diff: [{ code: "C01", change_type: "modified" }] });
      }
      return response({ id: "s1", status: "draft", state_version: 2,
        criteria: [{ code: "C01", name: "新名称", max_score: 20 }] });
    });
    const store = useRubricsStore();
    store.activeImportSession = { id: "s1", state_version: 1,
      criteria: [{ code: "C01", name: "旧名称", max_score: 10 }] };
    const file = new File(["replacement"], "新评分表.xlsx");

    const preview = await store.previewImportReupload({ rulesFile: file });
    expect(preview.fingerprint).toBe("fp1");
    expect(store.activeImportSession.criteria[0].name).toBe("旧名称");
    await store.confirmImportReupload({ fingerprint: preview.fingerprint, rulesFile: file });

    expect(calls[0].url).toMatch(/\/reupload-preview$/);
    expect(calls[1].url).toMatch(/\/reupload-confirm$/);
    expect(store.activeImportSession.state_version).toBe(2);
    expect(store.activeImportSession.criteria[0].name).toBe("新名称");
  });

  it("冲突裁决与取消均携带活动会话版本", async () => {
    const calls = [];
    vi.stubGlobal("fetch", (url, init) => {
      calls.push({ url: String(url), body: init.body && JSON.parse(init.body) });
      if (String(url).endsWith("/cancel")) {
        return response({ id: "s1", status: "cancelled", state_version: 3 });
      }
      return response({ id: "s1", status: "draft", state_version: 2, conflicts: [] });
    });
    const store = useRubricsStore();
    store.activeImportSession = { id: "s1", state_version: 1 };

    await store.resolveImportConflict("docx:tbl[0]/r2/c1", "use_excel", "以 Excel 为准");
    await store.cancelImportSession();

    expect(calls[0].url).toContain("conflicts/docx%3Atbl%5B0%5D%2Fr2%2Fc1/resolve");
    expect(calls[0].body.expected_state_version).toBe(1);
    expect(calls[1].body.expected_state_version).toBe(2);
    expect(store.activeImportSession).toBeNull();
  });

  it("正式草稿重新上传同样先预览再生成后继编译", async () => {
    const calls = [];
    vi.stubGlobal("fetch", (url, init) => {
      calls.push({ url: String(url), body: init.body });
      if (String(url).endsWith("/reupload-preview")) {
        return response({ fingerprint: "formal-fp", modified_count: 1, criteria_diff: [] });
      }
      return response({ id: "r1", name: "更新后的评分标准", criteria: [] });
    });
    const store = useRubricsStore();
    const file = new File(["replacement"], "replacement.xlsx");

    const preview = await store.previewRubricReupload("r1", { rulesFile: file });
    const result = await store.confirmRubricReupload("r1", {
      fingerprint: preview.fingerprint,
      rulesFile: file,
    });

    expect(calls[0].url).toMatch(/\/rubrics\/r1\/reupload-preview$/);
    expect(calls[1].url).toMatch(/\/rubrics\/r1\/reupload-confirm$/);
    expect(result.name).toBe("更新后的评分标准");
  });
});
