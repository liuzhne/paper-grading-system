import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useBatchesStore } from "./batches.js";

/**
 * 分数分布与归档动作（前端 v2 计划 §5-C、§5-D、§6）。
 *
 * 两条不能造假的：分桶策略还没定（§11），前端不能自己编一套档位；缺结果的
 * 材料不是 0 分，不能并进分布把平均分拉低。
 *
 * 归档与重开都带 `state_version`：这两个动作改的是批次能不能被写，凭一个
 * 过期的页面状态执行等于让并发的两个人互相覆盖。
 */
describe("分数分布", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    globalThis.__PGS_CONFIG__ = undefined;
  });

  function stub(handler) {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(handler));
  }

  function jsonResponse(body, status = 200) {
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    );
  }

  const DISTRIBUTION = {
    batch_id: "b1",
    result_revision: "r".repeat(64),
    max_score: 100,
    scores: [72.5, 88, 91],
    scored_count: 3,
    without_results: 2,
    average: 83.83,
    bucketing: null,
  };

  it("加载后保留服务端给的原始终分，不自行分桶", async () => {
    stub(() => jsonResponse(DISTRIBUTION));
    const store = useBatchesStore();

    const payload = await store.loadDistribution("b1");

    expect(payload.scores).toEqual([72.5, 88, 91]);
    expect(store.distributionFor("b1").bucketing).toBeNull();
  });

  it("缺结果单独可读，不混进分数序列", async () => {
    stub(() => jsonResponse(DISTRIBUTION));
    const store = useBatchesStore();

    await store.loadDistribution("b1");

    expect(store.distributionFor("b1").without_results).toBe(2);
    expect(store.distributionFor("b1").scores).not.toContain(0);
  });

  it("空批次不返回伪造的平均分", async () => {
    stub(() =>
      jsonResponse({ ...DISTRIBUTION, scores: [], scored_count: 0, average: null }),
    );
    const store = useBatchesStore();

    await store.loadDistribution("b1");

    expect(store.distributionFor("b1").average).toBeNull();
  });

  it("未加载过的批次返回 null 而不是空对象", () => {
    const store = useBatchesStore();

    expect(store.distributionFor("nope")).toBeNull();
  });
});

describe("归档与重开", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    globalThis.__PGS_CONFIG__ = undefined;
  });

  function jsonResponse(body, status = 200) {
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    );
  }

  it("提交时带上当前 state_version", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(() =>
        jsonResponse({ id: "b1", status: "archived", state_version: 4 }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const store = useBatchesStore();
    store.batches = [{ id: "b1", status: "reviewed", state_version: 3 }];

    await store.archive("b1");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain("/batches/b1/archive");
    expect(JSON.parse(init.body)).toEqual({ state_version: 3 });
  });

  it("成功后就地更新该批次的阶段与版本", async () => {
    vi.stubGlobal("fetch", () =>
      jsonResponse({ id: "b1", status: "archived", state_version: 4 }),
    );
    const store = useBatchesStore();
    store.batches = [{ id: "b1", status: "reviewed", state_version: 3 }];

    await store.archive("b1");

    expect(store.batches[0].status).toBe("archived");
    expect(store.batches[0].state_version).toBe(4);
  });

  it("重开采用服务端推导的阶段，不假设一定回 reviewed", async () => {
    vi.stubGlobal("fetch", () =>
      jsonResponse({ id: "b1", status: "draft", state_version: 5 }),
    );
    const store = useBatchesStore();
    store.batches = [{ id: "b1", status: "archived", state_version: 4 }];

    await store.reopen("b1");

    expect(store.batches[0].status).toBe("draft");
  });

  it("冲突时保留原阶段并报错，不把界面改成好像已成功", async () => {
    vi.stubGlobal("fetch", () =>
      jsonResponse({ detail: "批次状态已被其他操作更新" }, 409),
    );
    const store = useBatchesStore();
    store.batches = [{ id: "b1", status: "reviewed", state_version: 3 }];

    await expect(store.archive("b1")).rejects.toThrow();
    expect(store.batches[0].status).toBe("reviewed");
  });

  it("找不到批次时不发请求", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const store = useBatchesStore();

    await expect(store.archive("missing")).rejects.toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
