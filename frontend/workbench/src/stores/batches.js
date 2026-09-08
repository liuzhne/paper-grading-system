// @ts-check
import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { api, StaleContextError } from "@/api/client.js";
import { STAGE_ORDER } from "@/stores/stages.js";

/**
 * 从生成的 OpenAPI 类型引入（§8）。手抄一份形状出来，后端改了字段它不会动。
 * @typedef {import("@/api/types.js").Batch} Batch
 * @typedef {import("@/api/types.js").BatchProgress} BatchProgress
 * @typedef {import("@/api/types.js").ScoreDistribution} ScoreDistribution
 */

/**
 * 批次列表与进度。
 *
 * 计数一律来自服务端：进度、完成比例与阶段计数都由 `/batches/{id}/progress`
 * 的结果选择集合给出，前端**不自行按阶段猜百分比**——同一批次在工作台与
 * 评分任务页必须显示同一组数字（计划 §5-B）。
 */
export const useBatchesStore = defineStore("batches", () => {
  /** @type {import('vue').Ref<Batch[]>} */
  const batches = ref([]);
  const loading = ref(false);
  /** @type {import('vue').Ref<string|null>} */
  const error = ref(null);
  /** @type {import('vue').Ref<string|null>} */
  const stageFilter = ref(null);
  /** @type {import('vue').Ref<Record<string, BatchProgress>>} batch_id -> progress payload */
  const progress = ref({});
  /** @type {import('vue').Ref<Record<string, ScoreDistribution>>} batch_id -> score-distribution payload */
  const distribution = ref({});

  const total = computed(() => batches.value.length);

  /**
   * 阶段计数固定来自**未过滤**的那一次加载。
   *
   * 过滤改由服务端执行后，`batches` 里只剩被选中的阶段；用它算图例会让其它
   * 阶段全变成 0——看上去像「这些阶段没有批次」，而不是「你正在筛」。
   */
  /** @type {import('vue').Ref<Batch[]>} */
  const allStages = ref([]);

  const stageCounts = computed(() => {
    const counts = Object.fromEntries(STAGE_ORDER.map((code) => [code, 0]));
    for (const batch of allStages.value) {
      // 后端新增阶段时不丢计数。
      counts[batch.status] = (counts[batch.status] ?? 0) + 1;
    }
    return counts;
  });

  // 服务端已经筛过，这里不再筛第二遍——两处口径一旦分叉就会互相打架。
  const visible = computed(() => batches.value);

  /** @param {string|null} stage */
  async function setStageFilter(stage) {
    stageFilter.value = stage || null;
    await load();
  }

  async function load() {
    loading.value = true;
    error.value = null;
    try {
      // 过滤走服务端。客户端筛在批次多起来之后要为了看一个阶段下载全部，
      // 且分页一旦加上，会把「这一页里没有该阶段」显示成「没有该阶段的批次」。
      //
      // 两条路径都写成字面量：把整个查询串插进去（`/batches${query}`）会让基
      // 路径静态不可见，`test_frontend_api_contract` 那道门禁就查不到它。
      batches.value =
        (await (stageFilter.value
          ? api.get(`/batches?status=${stageFilter.value}`)
          : api.get("/batches"))) || [];
      if (!stageFilter.value) allStages.value = batches.value;
    } catch (err) {
      if (err instanceof StaleContextError) return;
      // 不留半截列表：失败时清空并报错，避免用户对着过期数据操作。
      batches.value = [];
      allStages.value = [];
      error.value =
        (err instanceof Error ? err.message : null) || "加载评分任务失败";
    } finally {
      loading.value = false;
    }
  }

  /** @param {string} batchId */
  async function loadProgress(batchId) {
    try {
      const payload = await api.get(`/batches/${batchId}/progress`);
      progress.value = { ...progress.value, [batchId]: payload };
      return payload;
    } catch (err) {
      if (err instanceof StaleContextError) return null;
      throw err;
    }
  }

  /** @param {string} batchId */
  function progressFor(batchId) {
    return progress.value[batchId] ?? null;
  }

  /** @param {string} batchId */
  async function loadDistribution(batchId) {
    try {
      const payload = await api.get(`/batches/${batchId}/score-distribution`);
      distribution.value = { ...distribution.value, [batchId]: payload };
      return payload;
    } catch (err) {
      if (err instanceof StaleContextError) return null;
      throw err;
    }
  }

  /** @param {string} batchId */
  function distributionFor(batchId) {
    return distribution.value[batchId] ?? null;
  }

  /**
   * 归档 / 重开。
   *
   * 带上本地已知的 `state_version` 做前置条件：这两个动作改的是批次能不能被
   * 写，凭一个过期的页面状态执行等于让并发的两个人互相覆盖。目标阶段由服务端
   * 推导——重开不一定回 reviewed，空批次会落回 draft。
   */
  /** @param {string} batchId */
  function stageActionBody(batchId) {
    const batch = batches.value.find((item) => item.id === batchId);
    if (!batch) {
      throw new Error("找不到该评分任务，请刷新后重试。");
    }
    return { state_version: batch.state_version };
  }

  /**
   * @param {string} batchId
   * @param {Partial<Batch>} updated
   */
  function applyStageResult(batchId, updated) {
    batches.value = batches.value.map((item) =>
      item.id === batchId ? { ...item, ...updated } : item,
    );
    return updated;
  }

  // 路径写死两条，不拼 `${action}`：拼出来的路径静态查不出来，
  // `test_frontend_api_contract` 这道门禁就漏过去了。
  /** @param {string} batchId */
  async function archive(batchId) {
    const body = stageActionBody(batchId);
    return applyStageResult(batchId, await api.post(`/batches/${batchId}/archive`, body));
  }

  /** @param {string} batchId */
  async function reopen(batchId) {
    const body = stageActionBody(batchId);
    return applyStageResult(batchId, await api.post(`/batches/${batchId}/reopen`, body));
  }

  function reset() {
    batches.value = [];
    allStages.value = [];
    progress.value = {};
    distribution.value = {};
    stageFilter.value = null;
    error.value = null;
  }

  return {
    batches,
    loading,
    error,
    stageFilter,
    total,
    stageCounts,
    visible,
    setStageFilter,
    load,
    loadProgress,
    progressFor,
    loadDistribution,
    distributionFor,
    archive,
    reopen,
    reset,
  };
});
