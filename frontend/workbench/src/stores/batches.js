import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { api, StaleContextError } from "@/api/client.js";
import { STAGE_ORDER } from "@/stores/stages.js";

/**
 * 批次列表与进度。
 *
 * 计数一律来自服务端：进度、完成比例与阶段计数都由 `/batches/{id}/progress`
 * 的结果选择集合给出，前端**不自行按阶段猜百分比**——同一批次在工作台与
 * 评分任务页必须显示同一组数字（计划 §5-B）。
 */
export const useBatchesStore = defineStore("batches", () => {
  const batches = ref([]);
  const loading = ref(false);
  const error = ref(null);
  const stageFilter = ref(null);
  /** batch_id -> progress payload */
  const progress = ref({});

  const total = computed(() => batches.value.length);

  const stageCounts = computed(() => {
    const counts = Object.fromEntries(STAGE_ORDER.map((code) => [code, 0]));
    for (const batch of batches.value) {
      // 后端新增阶段时不丢计数。
      counts[batch.status] = (counts[batch.status] ?? 0) + 1;
    }
    return counts;
  });

  const visible = computed(() =>
    stageFilter.value
      ? batches.value.filter((batch) => batch.status === stageFilter.value)
      : batches.value,
  );

  function setStageFilter(stage) {
    stageFilter.value = stage || null;
  }

  async function load() {
    loading.value = true;
    error.value = null;
    try {
      batches.value = (await api.get("/batches")) || [];
    } catch (err) {
      if (err instanceof StaleContextError) return;
      // 不留半截列表：失败时清空并报错，避免用户对着过期数据操作。
      batches.value = [];
      error.value = err?.message || "加载评分任务失败";
    } finally {
      loading.value = false;
    }
  }

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

  function progressFor(batchId) {
    return progress.value[batchId] ?? null;
  }

  function reset() {
    batches.value = [];
    progress.value = {};
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
    reset,
  };
});
