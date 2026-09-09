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
