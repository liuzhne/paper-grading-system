import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useGradingStore } from "./grading.js";

/**
 * 评分工作区（前端 v2 计划 §5-A、§3）。
 *
 * 中间栏正文有两条来源，身份强度不同，界面必须能区分：Core 冻结快照是硬
 * 锚点；legacy chunk 是**可变的当前解析结果**，可能已与判分时不同。
 */
describe("grading store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    globalThis.__PGS_CONFIG__ = undefined;
  });

  const PAPERS = [
    { id: "p1", student_id: "SE-2026-009", student_name: "周予彤", title: "链路追踪", status: "parsed" },
    { id: "p2", student_id: "SE-2026-011", student_name: "陈景行", title: "缺陷定位", status: "parsed" },
  ];

  const RUNS = [
    {
      id: "r1",
      paper_id: "p1",
      status: "scored",
      final_total_score: 86,
      // `GET /scoring-runs` 本来就带这两项（ScoringRunRead）。工作区一直没显示。
      coherence_findings: [
        { severity: "warn", kind: "figure_reference", message: "图 3 未在正文中引用。", deducted_by: null },
      ],
      format_findings: [
        { severity: "error", field: "line_spacing", message: "正文行距不是 1.5 倍。", deducted_by: "C04", deducted_points: 2 },
        { severity: "warn", field: "margin", message: "页边距小于模板规定。", deducted_by: null },
      ],
    },
  ];

  const ITEMS = [
    {
      id: "i1",
      criterion_code: "C01",
      criterion_name: "研究方法",
      ai_score: 21,
      final_score: 21,
      max_score: 25,
      confidence: 0.61,
      need_manual_review: true,
      review_reason: "置信度偏低",
      review_reasons: [{ source: "deterministic", code: "low_confidence", message: "置信度偏低", rule_code: null }],
      review_revision: 1,
      evidence_view: [
        {
          source_kind: "legacy_chunks",
          anchor_id: "c1",
          quote: "本文采用分层抽样",
          context_text: "本文采用分层抽样方法。",
          section_title: "3.1 研究方法",
          page_start: 12,
          location_status: "verified",
          unlocatable_reason: null,
        },
      ],
    },
  ];

  const VIEW = {
    run_id: "r1",
    source_kind: "legacy_chunks",
    frozen: false,
    text_provenance: "current_parse",
    document_snapshot_hash: null,
    blocks: [
      { block_id: "chunk:c1", chunk_id: "c1", evidence_unit_id: null, section_title: "3.1 研究方法", text: "本文采用分层抽样方法。", page_start: 12, page_end: 12 },
    ],
    next_cursor: null,
    anchor_found: false,
  };

  function stub(overrides = {}) {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url) => {
        const path = String(url).replace("/api", "").split("?")[0];
        const table = {
          "/papers": PAPERS,
          "/scoring-runs": RUNS,
          "/scoring-runs/r1/items": ITEMS,
          "/scoring-runs/r1/document-view": VIEW,
          "/papers/p1/neighbors": {
            batch_id: "b1", paper_id: "p1", position: 1, total: 2,
            previous_paper_id: null, next_paper_id: "p2",
          },
          ...overrides,
        };
        const body = table[path];
        if (body === undefined) return Promise.resolve(new Response("{}", { status: 404 }));
        return Promise.resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );
  }

  it("加载材料、当前 run、评分项与正文", async () => {
    stub();
    const store = useGradingStore();

    await store.openBatch("b1");

    expect(store.papers).toHaveLength(2);
    expect(store.currentPaperId).toBe("p1");
    expect(store.items).toHaveLength(1);
    expect(store.documentBlocks).toHaveLength(1);
  });

  it("请求评分项时带上 include_view，否则拿不到定位信息", async () => {
    const urls = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url) => {
        urls.push(String(url));
        const path = String(url).replace("/api", "").split("?")[0];
        const table = {
          "/papers": PAPERS, "/scoring-runs": RUNS,
          "/scoring-runs/r1/items": ITEMS, "/scoring-runs/r1/document-view": VIEW,
          "/papers/p1/neighbors": { position: 1, total: 2, previous_paper_id: null, next_paper_id: "p2" },
        };
        return Promise.resolve(new Response(JSON.stringify(table[path] ?? {}), {
          status: 200, headers: { "Content-Type": "application/json" },
        }));
      }),
    );
    const store = useGradingStore();

    await store.openBatch("b1");

    expect(urls.some((u) => u.includes("/items?include_view=true"))).toBe(true);
  });

  it("legacy 正文标注为当前解析结果，不冒充冻结快照", async () => {
    stub();
    const store = useGradingStore();
    await store.openBatch("b1");

    expect(store.documentFrozen).toBe(false);
    expect(store.provenanceNotice).toContain("当前解析");
  });

  it("Core 冻结快照给出快照标识且不提示可变", async () => {
    stub({
      "/scoring-runs/r1/document-view": {
        ...VIEW, source_kind: "core_snapshot", frozen: true,
        text_provenance: "frozen_snapshot", document_snapshot_hash: "b".repeat(64),
      },
    });
    const store = useGradingStore();
    await store.openBatch("b1");

    expect(store.documentFrozen).toBe(true);
    expect(store.provenanceNotice).toBe(null);
  });

  it("查不到快照时给出明确原因，不静默显示空白正文", async () => {
    stub({
      "/scoring-runs/r1/document-view": {
        run_id: "r1", source_kind: "unavailable", frozen: false,
        text_provenance: "unavailable", document_snapshot_hash: null,
        unavailable_reason: "该评分运行没有绑定冻结文档快照。",
        blocks: [], next_cursor: null, anchor_found: false,
      },
    });
    const store = useGradingStore();
    await store.openBatch("b1");

    expect(store.documentBlocks).toEqual([]);
    expect(store.documentUnavailableReason).toBeTruthy();
  });

  it("总分由评分项求和，不信任前端缓存的旧值", async () => {
    stub();
    const store = useGradingStore();
    await store.openBatch("b1");

    expect(store.totalScore).toBe(21);
    expect(store.maxTotal).toBe(25);
  });

  it("没有 run 的材料不报错，明确显示未评分", async () => {
    stub({ "/scoring-runs": [] });
    const store = useGradingStore();

    await store.openBatch("b1");

    expect(store.currentRunId).toBeNull();
    expect(store.items).toEqual([]);
    expect(store.documentBlocks).toEqual([]);
  });

  it("选中证据锚点后可供中间栏定位", async () => {
    stub();
    const store = useGradingStore();
    await store.openBatch("b1");

    store.locate({ anchor_id: "c1", quote: "本文采用分层抽样" });

    expect(store.activeAnchorId).toBe("c1");
    expect(store.activeQuote).toBe("本文采用分层抽样");
  });

  it("定位失败的证据不设置高亮引文", async () => {
    stub();
    const store = useGradingStore();
    await store.openBatch("b1");

    store.locate({ anchor_id: "c9", quote: null });

    expect(store.activeQuote).toBeNull();
  });
  it("带 paperId 打开时直接落在那一份，不是列表首项", async () => {
    stub();
    const store = useGradingStore();

    await store.openBatch("b1", "p2");

    expect(store.currentPaperId).toBe("p2");
  });

  it("指定的材料不在这个批次里时回落到首项，而不是空选中", async () => {
    stub();
    const store = useGradingStore();

    // 复核队列里的链接可能指向一份已经被移出批次的材料。选中一个列表里没有的
    // id，界面会显示成「一份都没选中」，而用户看不出发生了什么。
    await store.openBatch("b1", "p-not-here");

    expect(store.currentPaperId).toBe("p1");
  });

  it("留住当前 run 的篇章与格式发现——它们已经随 run 列表回来了", async () => {
    stub();
    const store = useGradingStore();

    await store.openBatch("b1");

    // 数据一直在浏览器里，只是从来没显示过：中栏「篇章结构 / 格式发现」两个页签
    // 读的就是这里，不必再发一次请求。
    expect(store.coherenceFindings).toHaveLength(1);
    expect(store.formatFindings).toHaveLength(2);
  });

  it("未评分的材料没有发现项，返回空数组而不是 undefined", async () => {
    // 这份材料没有 run：`/scoring-runs` 返回空数组。
    stub({ "/scoring-runs": [] });
    const store = useGradingStore();

    await store.openBatch("b1");

    expect(store.coherenceFindings).toEqual([]);
    expect(store.formatFindings).toEqual([]);
  });

  it("run 上没有这两个字段时也不炸——历史 run 可能是 null", async () => {
    stub({ "/scoring-runs": [{ id: "r1", paper_id: "p1", status: "scored" }] });
    const store = useGradingStore();

    await store.openBatch("b1");

    expect(store.coherenceFindings).toEqual([]);
    expect(store.formatFindings).toEqual([]);
  });
});

describe("工作区写能力（V3-5）", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  function jsonOk(body) {
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  }

  it("改分必须带理由，空理由当场拦下", async () => {
    const store = useGradingStore();

    // 改分会写进复核记录；没有理由的记录事后无法判断当初为什么改。
    await expect(store.overrideScore("i1", { score: 18, reason: "  " })).rejects.toThrow(
      /理由/,
    );
  });

  it("改分把分数与理由一起发出", async () => {
    let sent = null;
    vi.stubGlobal("fetch", (url, init) => {
      sent = JSON.parse(init.body);
      return jsonOk({ id: "i1", final_score: 18, review_revision: 2 });
    });
    const store = useGradingStore();

    await store.overrideScore("i1", { score: 18, reason: "论证不足，扣 2 分" });

    expect(sent.final_score).toBe(18);
    expect(sent.reason).toBe("论证不足，扣 2 分");
  });

  it("提交单份复核同样要理由", async () => {
    const store = useGradingStore();

    await expect(store.submitRunReview("r1", "")).rejects.toThrow(/理由/);
  });

  it("提交单份复核后返回服务端结果", async () => {
    vi.stubGlobal("fetch", () => jsonOk({ id: "r1", status: "reviewed" }));
    const store = useGradingStore();

    const result = await store.submitRunReview("r1", "已逐项核对");

    expect(result.status).toBe("reviewed");
  });

  it("并发冲突原样抛出，不静默覆盖", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve(
        new Response(JSON.stringify({ detail: "该给分已被其他人修改，请刷新" }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    const store = useGradingStore();

    // 冲突要让用户看到并重新决定，而不是把别人的改动盖掉。
    await expect(
      store.overrideScore("i1", { score: 18, reason: "改一下" }),
    ).rejects.toThrow(/刷新/);
  });
});
