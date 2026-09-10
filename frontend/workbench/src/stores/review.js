import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { api, ApiError, StaleContextError } from "@/api/client.js";

/**
 * 复核队列（前端 v2 计划 §5-B）。
 *
 * 采纳按钮的文案是「采纳本页可采纳项」而不是设计稿的「全部采纳系统给分」：
 * 服务端不做无界全批扫描，请求必须携带用户**实际看到过**的那批条目。一次
 * 点击不该在服务端展开成对成百上千条记录的隐式写入。
 */
export const useReviewStore = defineStore("review", () => {
  const batchId = ref(null);
  const entries = ref([]);
  const resultRevision = ref(null);
  const ordinaryPending = ref(0);
  const blockingOpen = ref(0);
  const nextCursor = ref(null);
  const stats = ref(null);
  const timeline = ref([]);
  const loading = ref(false);
  const busy = ref(false);
  const error = ref(null);
  const notice = ref(null);

  /** 可批量采纳的条目：阻塞任务与无 AI 分的项都不在其中。 */
  const acceptableEntries = computed(() =>
    entries.value.filter((entry) => entry.acceptable),
  );

  function reset() {
    entries.value = [];
    resultRevision.value = null;
    ordinaryPending.value = 0;
    blockingOpen.value = 0;
    nextCursor.value = null;
    stats.value = null;
    timeline.value = [];
    error.value = null;
    notice.value = null;
  }

  async function load(id) {
    batchId.value = id;
    loading.value = true;
    error.value = null;
    try {
      const queue = await api.get(`/batches/${id}/review-queue`);
      entries.value = queue.entries;
      resultRevision.value = queue.result_revision;
      ordinaryPending.value = queue.ordinary_pending;
      blockingOpen.value = queue.blocking_open;
      nextCursor.value = queue.next_cursor;
    } catch (err) {
      if (err instanceof StaleContextError) return;
      reset();
      error.value = err?.message || "加载复核队列失败";
    } finally {
      loading.value = false;
    }
  }

  async function loadStats(id = batchId.value) {
    if (!id) return null;
    try {
      stats.value = await api.get(`/batches/${id}/review-stats`);
      return stats.value;
    } catch (err) {
      if (!(err instanceof StaleContextError)) stats.value = null;
      return null;
    }
  }

  async function loadTimeline(id = batchId.value) {
    if (!id) return;
    try {
      timeline.value = (await api.get(`/batches/${id}/review-timeline`)).entries;
    } catch (err) {
      if (!(err instanceof StaleContextError)) timeline.value = [];
    }
  }

  /** 幂等键每次操作重新生成：复用会让第二次采纳被当成重放而静默无效。 */
  function newIdempotencyKey() {
    const random =
      globalThis.crypto?.randomUUID?.() ??
      `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    return `accept-${random}`;
  }

  /**
   * 提交一组已经在界面上出现过的条目。批量与逐项走同一条路径——两者的服务端
   * 前置条件完全一致，分成两份实现只会让其中一份先忘记带 revision。
   */
  async function accept(items, reason) {
    if (!items.length) return null;

    busy.value = true;
    error.value = null;
    notice.value = null;
    try {
      const result = await api.post(
        `/batches/${batchId.value}/review-queue/accept`,
        {
          result_revision: resultRevision.value,
          idempotency_key: newIdempotencyKey(),
          reason,
          items,
        },
      );
      notice.value = result.replayed
        ? "该请求已执行过，未重复写入复核记录。"
        : `已采纳 ${result.accepted_count} 项。`;
      await load(batchId.value);
      return result;
    } catch (err) {
      if (err instanceof StaleContextError) return null;
      // 409 是并发前置条件失败，用户需要刷新后重看——不能静默吞掉。
      error.value =
        err instanceof ApiError && err.status === 409
          ? err.detail || "队列已变化，请刷新后重新确认。"
          : err?.message || "采纳失败";
      return null;
    } finally {
      busy.value = false;
    }
  }

  function payloadOf(entry) {
    return {
      score_item_id: entry.score_item_id,
      review_revision: entry.review_revision,
    };
  }

  async function acceptVisible(reason) {
    return accept(acceptableEntries.value.map(payloadOf), reason);
  }

  /**
   * 确认单独一项（设计稿复核表的行内「确认」）。
   *
   * 不可采纳的条目直接拒绝，**不发请求**：阻塞任务缺的是结论本身（provider
   * 失败、规则被阻断），没有 AI 分的项则根本没有分可采纳。给它们一个会 400
   * 的按钮，等于让用户去点一个注定失败的动作。
   */
  async function acceptOne(entry, reason) {
    if (!entry?.acceptable || !entry.score_item_id) return null;
    return accept([payloadOf(entry)], reason);
  }

  async function completeReview() {
    busy.value = true;
    error.value = null;
    try {
      await api.post(`/batches/${batchId.value}/complete-review`, {
        result_revision: resultRevision.value,
      });
      notice.value = "复核已完成。";
      await load(batchId.value);
      return true;
    } catch (err) {
      if (err instanceof StaleContextError) return false;
      error.value = err?.detail || err?.message || "完成复核失败";
      return false;
    } finally {
      busy.value = false;
    }
  }

  return {
    batchId,
    entries,
    resultRevision,
    ordinaryPending,
    blockingOpen,
    nextCursor,
    stats,
    timeline,
    loading,
    busy,
    error,
    notice,
    acceptableEntries,
    load,
    loadStats,
    loadTimeline,
    acceptVisible,
    acceptOne,
    completeReview,
    reset,
  };
});
