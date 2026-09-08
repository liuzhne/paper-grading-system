import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useBatchesStore } from "./batches.js";
import { STAGES, stageLabel, stageTone } from "./stages.js";

/**
 * 批次列表与阶段口径（前端 v2 计划 §2、§5-C、§5-D）。
 *
 * 阶段与执行状态是两件事：一次 job 失败不代表批次阶段就是失败。界面必须
 * 同时呈现两者，且计数一律来自服务端的结果选择集合，前端不自行推导。
 */
describe("stages", () => {
  it("覆盖设计稿声明的七种业务阶段", () => {
    expect(Object.keys(STAGES)).toEqual([
      "draft",
      "parsing",
      "scoring",
      "scored",
      "scored_with_errors",
      "reviewed",
      "archived",
    ]);
  });

  it("每个阶段都有中文标签与内部编码，供图例对照", () => {
    for (const code of Object.keys(STAGES)) {
      expect(stageLabel(code)).toBeTruthy();
      expect(stageLabel(code)).not.toBe(code);
    }
  });

  it("未知阶段原样回显而不是崩掉或伪装成已知阶段", () => {
    expect(stageLabel("something_new")).toBe("something_new");
    expect(stageTone("something_new")).toBe("neutral");
  });

  it("异常与阻断阶段有区分度更高的色调", () => {
    expect(stageTone("scored_with_errors")).toBe("warn");
    expect(stageTone("scored")).toBe("ok");
    expect(stageTone("archived")).toBe("neutral");
  });
});

describe("batches store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    globalThis.__PGS_CONFIG__ = undefined;
  });

  function stub(routes) {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url) => {
        const path = String(url).replace("/api", "");
        const hit = routes[path];
        if (!hit) return Promise.resolve(new Response("{}", { status: 404 }));
        return Promise.resolve(
          new Response(JSON.stringify(hit), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );
  }

  const BATCHES = [
    { id: "b1", name: "毕业论文批次 1", status: "scored_with_errors", state_version: 3 },
    { id: "b2", name: "课程报告", status: "scoring", state_version: 2 },
    { id: "b3", name: "去年批次", status: "archived", state_version: 9 },
  ];

  it("加载批次并按阶段统计出筛选计数", async () => {
    stub({ "/batches": BATCHES });
    const store = useBatchesStore();

    await store.load();

    expect(store.batches).toHaveLength(3);
    expect(store.stageCounts.scoring).toBe(1);
    expect(store.stageCounts.archived).toBe(1);
    expect(store.total).toBe(3);
  });

  it("按阶段筛选交给服务端，不在客户端筛第二遍", async () => {
    stub({
      "/batches": BATCHES,
      "/batches?status=scoring": [BATCHES[1]],
    });
    const store = useBatchesStore();
    await store.load();

    expect(store.visible).toHaveLength(3);
    await store.setStageFilter("scoring");
    expect(store.visible.map((b) => b.id)).toEqual(["b2"]);
    await store.setStageFilter(null);
    expect(store.visible).toHaveLength(3);
  });

  it("筛选期间阶段计数仍来自未过滤的那次加载", async () => {
    stub({
      "/batches": BATCHES,
      "/batches?status=scoring": [BATCHES[1]],
    });
    const store = useBatchesStore();
    await store.load();

    await store.setStageFilter("scoring");

    // 用过滤后的列表算图例，其它阶段会全变成 0——看上去像「这些阶段没有批次」，
    // 而不是「你正在筛」。
    expect(store.stageCounts.archived).toBe(1);
    expect(store.stageCounts.scored_with_errors).toBe(1);
  });

  it("进度来自服务端，前端不自行按阶段猜百分比", async () => {
    stub({
      "/batches": BATCHES,
      "/batches/b1/progress": {
        batch_id: "b1",
        stage: "scored_with_errors",
        state_version: 3,
        counts: { total: 24, scored: 22, reviewed: 18, failed: 2, pending: 0 },
        completion_ratio: 22 / 24,
        result_revision: "a".repeat(64),
        job: null,
        available_actions: ["start_scoring", "complete_review"],
      },
    });
    const store = useBatchesStore();
    await store.load();

    const progress = await store.loadProgress("b1");

    expect(progress.counts.failed).toBe(2);
    expect(store.progressFor("b1").completion_ratio).toBeCloseTo(22 / 24);
  });

  it("空批次不显示百分比", async () => {
    stub({
      "/batches": BATCHES,
      "/batches/b2/progress": {
        batch_id: "b2",
        stage: "draft",
        state_version: 1,
        counts: { total: 0, scored: 0, reviewed: 0, failed: 0, pending: 0 },
        completion_ratio: null,
        result_revision: "b".repeat(64),
        job: null,
        available_actions: [],
      },
    });
    const store = useBatchesStore();
    await store.load();

    await store.loadProgress("b2");

    expect(store.progressFor("b2").completion_ratio).toBeNull();
  });

  it("任务故障与阶段并存时两者都要能读到", async () => {
    stub({
      "/batches": BATCHES,
      "/batches/b2/progress": {
        batch_id: "b2",
        stage: "scoring",
        state_version: 2,
        counts: { total: 10, scored: 4, reviewed: 0, failed: 1, pending: 5 },
        completion_ratio: 0.4,
        result_revision: "c".repeat(64),
        job: {
          id: "j1",
          generation: 2,
          status: "failed",
          total_items: 10,
          succeeded_count: 4,
          failed_count: 1,
          pending_count: 5,
        },
        available_actions: ["finish_scoring", "cancel"],
      },
    });
    const store = useBatchesStore();
    await store.load();

    const progress = await store.loadProgress("b2");

    expect(progress.stage).toBe("scoring");
    expect(progress.job.status).toBe("failed");
  });

  it("加载失败时清空数据并记录错误，不留下半截列表", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "数据库暂不可用" }), { status: 503 }),
      ),
    );
    const store = useBatchesStore();

    await store.load();

    expect(store.batches).toEqual([]);
    expect(store.error).toBeTruthy();
  });
});
