import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useReviewStore } from "./review.js";

/**
 * 复核队列 store（前端 v2 计划 §5-B）。
 *
 * 批量采纳的三条硬约束在这里都要有对应行为：只提交用户预览过的有限集合、
 * 携带 revision 做前置条件、幂等键每次操作唯一。
 */
describe("review store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    globalThis.__PGS_CONFIG__ = undefined;
  });

  const QUEUE = {
    batch_id: "b1",
    result_revision: "r".repeat(64),
    ordinary_pending: 2,
    blocking_open: 1,
    next_cursor: null,
    entries: [
      {
        queue_type: "blocking",
        score_item_id: null,
        task_id: "t1",
        task_version: 1,
        student_id: "SE-2026-021",
        criterion_code: "C03",
        criterion_name: null,
        ai_score: null,
        max_score: null,
        confidence: null,
        review_reason: "Provider 调用失败",
        review_reasons: [
          { source: "core_issue", code: "provider_error", message: "Provider 调用失败", rule_code: null },
        ],
        review_revision: 1,
        acceptable: false,
      },
      {
        queue_type: "ordinary",
        score_item_id: "i1",
        task_id: null,
        student_id: "SE-2026-014",
        criterion_code: "C01",
        criterion_name: "研究方法",
        ai_score: 21,
        max_score: 25,
        confidence: 0.61,
        review_reason: "置信度偏低",
        review_reasons: [
          { source: "deterministic", code: "low_confidence", message: "置信度偏低", rule_code: null },
        ],
        review_revision: 2,
        acceptable: true,
      },
      {
        queue_type: "ordinary",
        score_item_id: "i2",
        task_id: null,
        student_id: "SE-2026-016",
        criterion_code: "C02",
        criterion_name: "实现与实验",
        ai_score: null,
        max_score: 25,
        confidence: null,
        review_reason: "该评分路径未提供置信度",
        review_reasons: [
          { source: "deterministic", code: "confidence_unavailable", message: "该评分路径未提供置信度", rule_code: null },
        ],
        review_revision: 1,
        acceptable: false,
      },
    ],
  };

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

  it("加载队列并分开统计两类工作", async () => {
    stub(() => jsonResponse(QUEUE));
    const store = useReviewStore();

    await store.load("b1");

    expect(store.entries).toHaveLength(3);
    expect(store.ordinaryPending).toBe(2);
    expect(store.blockingOpen).toBe(1);
  });

  it("阻塞任务排在普通确认之前", async () => {
    stub(() => jsonResponse(QUEUE));
    const store = useReviewStore();

    await store.load("b1");

    expect(store.entries[0].queue_type).toBe("blocking");
  });

  it("只有 acceptable 的条目进入可采纳集合", async () => {
    stub(() => jsonResponse(QUEUE));
    const store = useReviewStore();
    await store.load("b1");

    expect(store.acceptableEntries.map((e) => e.score_item_id)).toEqual(["i1"]);
  });

  it("采纳请求只提交本页可采纳项，并带上两级 revision", async () => {
    let captured = null;
    stub((url, init) => {
      if (String(url).endsWith("/accept")) {
        captured = JSON.parse(init.body);
        return jsonResponse({
          batch_id: "b1",
          accepted_count: 1,
          result_revision: QUEUE.result_revision,
          replayed: false,
        });
      }
      return jsonResponse(QUEUE);
    });
    const store = useReviewStore();
    await store.load("b1");

    await store.acceptVisible("采纳系统给分");

    expect(captured.result_revision).toBe(QUEUE.result_revision);
    expect(captured.items).toEqual([{ score_item_id: "i1", review_revision: 2 }]);
    expect(captured.idempotency_key).toBeTruthy();
  });

  it("没有可采纳项时不发请求", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      jsonResponse({ ...QUEUE, entries: [QUEUE.entries[0]] }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const store = useReviewStore();
    await store.load("b1");
    fetchMock.mockClear();

    await store.acceptVisible("理由");

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("每次采纳使用不同的幂等键，避免第二次被当成重放", async () => {
    const keys = [];
    stub((url, init) => {
      if (String(url).endsWith("/accept")) {
        keys.push(JSON.parse(init.body).idempotency_key);
        return jsonResponse({
          batch_id: "b1",
          accepted_count: 1,
          result_revision: QUEUE.result_revision,
          replayed: false,
        });
      }
      return jsonResponse(QUEUE);
    });
    const store = useReviewStore();
    await store.load("b1");

    await store.acceptVisible("理由");
    await store.acceptVisible("理由");

    expect(keys[0]).not.toBe(keys[1]);
  });

  it("409 冲突提示刷新，不静默吞掉", async () => {
    stub((url) => {
      if (String(url).endsWith("/accept")) {
        return jsonResponse({ detail: "批次结果已更新，请刷新后重新确认。" }, 409);
      }
      return jsonResponse(QUEUE);
    });
    const store = useReviewStore();
    await store.load("b1");

    await store.acceptVisible("理由");

    expect(store.error).toContain("刷新");
  });
});
