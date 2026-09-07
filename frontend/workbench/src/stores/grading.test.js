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

  const RUNS = [{ id: "r1", paper_id: "p1", status: "scored", final_total_score: 86 }];

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
});
