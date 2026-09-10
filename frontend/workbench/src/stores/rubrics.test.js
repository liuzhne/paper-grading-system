import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useRubricsStore } from "@/stores/rubrics.js";

/**
 * 评分标准的导入与列表（v3 §3.1、D-026）。
 *
 * 只能由导入产生——不保留空白新建。导入返回的 `warnings` 与 `template_summary`
 * **原样展示**：那是「你的 Excel 里哪几条没被识别」的唯一出口，吞掉它等于让用户
 * 以为全都导进去了。
 */
function jsonResponse(body, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

describe("评分标准导入", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it("导入成功后保留 warnings 与模板摘要，不吞掉", async () => {
    vi.stubGlobal("fetch", () =>
      jsonResponse({
        rubric: { id: "r1", name: "课程报告", version: "v1.0", status: "draft" },
        warnings: ["第 7 行缺少扣分说明，已跳过。"],
        template_summary: { matched: 5, unmatched: 2 },
      }),
    );
    const store = useRubricsStore();

    const result = await store.importFiles({
      name: "课程报告",
      version: "v1.0",
      rulesFile: new File(["x"], "rules.xlsx"),
    });

    expect(result.rubric.id).toBe("r1");
    expect(store.lastImport.warnings).toEqual(["第 7 行缺少扣分说明，已跳过。"]);
    expect(store.lastImport.templateSummary).toEqual({ matched: 5, unmatched: 2 });
  });

  it("默认可见范围是仅自己可见", async () => {
    let sent = null;
    vi.stubGlobal("fetch", (url, init) => {
      sent = init?.body;
      return jsonResponse({ rubric: { id: "r1" }, warnings: [], template_summary: {} });
    });
    const store = useRubricsStore();

    await store.importFiles({
      name: "私有标准",
      version: "v1.0",
      rulesFile: new File(["x"], "rules.xlsx"),
    });

    // D-026/V3-c：导入默认仅自己可见，扩大范围是发布时的显式动作。
    expect(sent.get("visibility")).toBe("private");
  });

  it("用 FormData 提交且不手工设置 Content-Type", async () => {
    let init = null;
    vi.stubGlobal("fetch", (url, options) => {
      init = options;
      return jsonResponse({ rubric: { id: "r1" }, warnings: [], template_summary: {} });
    });
    const store = useRubricsStore();

    await store.importFiles({
      name: "x",
      version: "v1.0",
      rulesFile: new File(["x"], "rules.xlsx"),
    });

    // multipart 的 boundary 必须由浏览器补，手工设置会让后端解析不出文件。
    expect(init.body).toBeInstanceOf(FormData);
    expect(init.headers?.["Content-Type"]).toBeUndefined();
  });

  it("导入失败时把服务端说明留给用户，不吞成一句通用错误", async () => {
    vi.stubGlobal("fetch", () =>
      jsonResponse({ detail: "rules_file must be an .xlsx or .xlsm file" }, 400),
    );
    const store = useRubricsStore();

    await expect(
      store.importFiles({
        name: "x",
        version: "v1.0",
        rulesFile: new File(["x"], "rules.txt"),
      }),
    ).rejects.toThrow(/xlsx/);
  });

  it("可选的模板文件不传时不出现在请求里", async () => {
    let sent = null;
    vi.stubGlobal("fetch", (url, init) => {
      sent = init?.body;
      return jsonResponse({ rubric: { id: "r1" }, warnings: [], template_summary: {} });
    });
    const store = useRubricsStore();

    await store.importFiles({
      name: "x",
      version: "v1.0",
      rulesFile: new File(["x"], "rules.xlsx"),
    });

    expect(sent.get("template_file")).toBeNull();
  });
});

describe("发布：编译产物 + 分享范围", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it("必须显式指定编译产物，不提供「用最新的」", async () => {
    const store = useRubricsStore();

    await expect(
      store.publish("r1", { compilationId: null, visibility: "private" }),
    ).rejects.toThrow(/编译产物/);
  });

  it("范围与编译产物在同一个请求里发出", async () => {
    let sent = null;
    vi.stubGlobal("fetch", (url, init) => {
      sent = JSON.parse(init.body);
      return jsonResponse({ id: "r1", status: "published" });
    });
    const store = useRubricsStore();

    await store.publish("r1", { compilationId: "c9", visibility: "organization" });

    // 分两个请求会留下「已发布但范围还是旧的」这个没有补救入口的中间态。
    expect(sent.compilation_id).toBe("c9");
    expect(sent.visibility).toBe("organization");
  });

  it("不选范围时不发送该字段，由服务端沿用当前值", async () => {
    let sent = null;
    vi.stubGlobal("fetch", (url, init) => {
      sent = JSON.parse(init.body);
      return jsonResponse({ id: "r1", status: "published" });
    });
    const store = useRubricsStore();

    await store.publish("r1", { compilationId: "c9", visibility: null });

    expect(sent.compilation_id).toBe("c9");
    expect("visibility" in sent).toBe(false);
  });

  it("发布失败时把服务端说明留给用户", async () => {
    vi.stubGlobal("fetch", () =>
      jsonResponse({ detail: "只有平台管理员可以把评分标准分享给所有组织。" }, 403),
    );
    const store = useRubricsStore();

    await expect(
      store.publish("r1", { compilationId: "c9", visibility: "system" }),
    ).rejects.toThrow(/平台管理员/);
  });
});

describe("AI 起草缺失扣分细则", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it("必须带上用户自己的连接，不允许留空回落", async () => {
    const store = useRubricsStore();

    // 不带连接调用会走平台默认；平台是 mock 时得到的是编出来的扣分规则，
    // 却以「AI 起草 · 待确认」呈现，确认后进入正式标准（D-027）。
    await expect(
      store.draftRules("r1", { criteria: [{ code: "T01" }], connectionId: null }),
    ).rejects.toThrow(/连接/);
  });

  it("带连接时把 id 发给服务端", async () => {
    let sent = null;
    vi.stubGlobal("fetch", (url, init) => {
      sent = JSON.parse(init.body);
      return jsonResponse({ items: [] });
    });
    const store = useRubricsStore();

    await store.draftRules("r1", {
      criteria: [{ code: "T01" }],
      connectionId: "conn-1",
    });

    expect(sent.ai_connection_id).toBe("conn-1");
  });

  it("起草结果标记为待确认，不直接进入可执行版本", async () => {
    vi.stubGlobal("fetch", () =>
      jsonResponse({
        items: [
          { criterion_code: "T01", rules: [{ issue: "缺少方法说明", deduct: 3 }] },
        ],
      }),
    );
    const store = useRubricsStore();

    const result = await store.draftRules("r1", {
      criteria: [{ code: "T01" }],
      connectionId: "conn-1",
    });

    // 起草只是建议：确认之前不能改变任何已发布内容。
    expect(result.items[0].criterion_code).toBe("T01");
    expect(store.lastDraft.items).toHaveLength(1);
  });
});

describe("确认后的 AI 规则落库（V3-2 闭环）", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  const DRAFT = {
    criterion_code: "T02",
    rule_groups: [
      {
        group_code: "G1",
        issue: "覆盖不足",
        mutex_group: "T02-c",
        cap_points: 6,
        rules: [
          { severity: "minor", trigger: "少于 15 篇", points: 2, reason: "偏窄", source: "ai_inferred", source_refs: ["/x"] },
        ],
      },
    ],
    generation_metadata: { fingerprint: "f".repeat(64) },
  };

  const RUBRIC = {
    id: "r1",
    name: "课程报告",
    version: "v1.0",
    total_score: 100,
    criteria: [
      { code: "T01", name: "选题", max_score: 85, scoring_mode: "llm_direct", deduction_rules_structured: [] },
      { code: "T02", name: "文献综述", max_score: 15, scoring_mode: "review_only", deduction_rules_structured: [] },
    ],
  };

  it("走 recompile 落库：起草端点本身不写任何东西", async () => {
    let captured = null;
    let calledPath = null;
    vi.stubGlobal("fetch", (url, init) => {
      const path = String(url);
      if (init?.method === "POST") {
        calledPath = path;
        captured = JSON.parse(init.body);
        return jsonResponse(RUBRIC);
      }
      return jsonResponse(RUBRIC);
    });
    const store = useRubricsStore();

    await store.applyDraftRules("r1", {
      criteria: RUBRIC.criteria,
      items: [{ criterion_code: "T02", draft: DRAFT }],
      excluded: new Set(),
      supersedesCompilationId: "c1",
      version: "v1.0",
    });

    expect(calledPath).toContain("/rubrics/r1/recompile");
    expect(captured.supersedes_compilation_id).toBe("c1");
    expect(captured.criteria[1].scoring_mode).toBe("deductive");
    expect(captured.criteria[1].deduction_rules_structured).toHaveLength(1);
  });

  it("没有编译产物 id 时不发请求——recompile 缺它必然 409", async () => {
    const fetchMock = vi.fn(() => jsonResponse(RUBRIC));
    vi.stubGlobal("fetch", fetchMock);
    const store = useRubricsStore();

    await expect(
      store.applyDraftRules("r1", {
        criteria: RUBRIC.criteria,
        items: [{ criterion_code: "T02", draft: DRAFT }],
        excluded: new Set(),
        supersedesCompilationId: null,
        version: "v1.0",
      }),
    ).rejects.toThrow(/执行草稿/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("一条都没确认时不发请求——那只会生成一份内容相同的新草稿", async () => {
    const fetchMock = vi.fn(() => jsonResponse(RUBRIC));
    vi.stubGlobal("fetch", fetchMock);
    const store = useRubricsStore();

    await expect(
      store.applyDraftRules("r1", {
        criteria: RUBRIC.criteria,
        items: [{ criterion_code: "T02", draft: DRAFT }],
        excluded: new Set(["T02::G1::0"]),
        supersedesCompilationId: "c1",
        version: "v1.0",
      }),
    ).rejects.toThrow(/没有可应用/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("应用成功后清空起草结果，避免同一批建议被重复应用", async () => {
    vi.stubGlobal("fetch", () => jsonResponse(RUBRIC));
    const store = useRubricsStore();
    store.lastDraft = { items: [{ criterion_code: "T02", draft: DRAFT }] };

    await store.applyDraftRules("r1", {
      criteria: RUBRIC.criteria,
      items: [{ criterion_code: "T02", draft: DRAFT }],
      excluded: new Set(),
      supersedesCompilationId: "c1",
      version: "v1.0",
    });

    expect(store.lastDraft.items).toEqual([]);
  });

  it("带上 reason：recompile 会把它写进变更记录", async () => {
    let captured = null;
    vi.stubGlobal("fetch", (url, init) => {
      if (init?.method === "POST") captured = JSON.parse(init.body);
      return jsonResponse(RUBRIC);
    });
    const store = useRubricsStore();

    await store.applyDraftRules("r1", {
      criteria: RUBRIC.criteria,
      items: [{ criterion_code: "T02", draft: DRAFT }],
      excluded: new Set(),
      supersedesCompilationId: "c1",
      version: "v1.0",
    });

    expect(captured.reason).toBeTruthy();
  });
});
