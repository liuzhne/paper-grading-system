import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { api, StaleContextError } from "@/api/client.js";

/**
 * 评分工作区（前端 v2 计划 §5-A、§3）。
 *
 * 中间栏正文有两条来源，身份强度不同：
 *
 * - ``core_snapshot`` 是运行绑定的冻结快照，能确凿回答「判分时看到的就是这段」。
 * - ``legacy_chunks`` 是**当前**解析结果，重新解析后可能已与判分时不同。界面
 *   必须标明这一点，否则复核者会以为自己在核对原始证据。
 *
 * 查不到快照时显示原因而不是空白正文——空白会被当成「这份材料没内容」。
 */
export const useGradingStore = defineStore("grading", () => {
  const batchId = ref(null);
  const papers = ref([]);
  const currentPaperId = ref(null);
  const currentRunId = ref(null);
  const items = ref([]);
  const documentView = ref(null);
  const neighbors = ref(null);
  const loading = ref(false);
  const error = ref(null);

  /** 当前选中的证据锚点，供中间栏滚动与高亮。 */
  const activeAnchorId = ref(null);
  const activeQuote = ref(null);

  const documentBlocks = computed(() => documentView.value?.blocks ?? []);
  const documentFrozen = computed(() => documentView.value?.frozen === true);
  const documentUnavailableReason = computed(
    () => documentView.value?.unavailable_reason ?? null,
  );

  /** legacy 来源要显式提示文本可能已变，Core 冻结快照不需要。 */
  const provenanceNotice = computed(() =>
    documentView.value?.text_provenance === "current_parse"
      ? "以下是该材料当前解析得到的正文，并非判分时的冻结快照；若材料被重新解析过，文本可能已不同。"
      : null,
  );

  const currentPaper = computed(
    () => papers.value.find((p) => p.id === currentPaperId.value) ?? null,
  );

  // 总分实时由评分项求和，不缓存旧值——改分后不重算会显示过期总分。
  const totalScore = computed(() =>
    items.value.reduce(
      (sum, item) => sum + Number(item.final_score ?? item.ai_score ?? 0),
      0,
    ),
  );
  const maxTotal = computed(() =>
    items.value.reduce((sum, item) => sum + Number(item.max_score ?? 0), 0),
  );

  function reset() {
    papers.value = [];
    currentPaperId.value = null;
    currentRunId.value = null;
    items.value = [];
    documentView.value = null;
    neighbors.value = null;
    activeAnchorId.value = null;
    activeQuote.value = null;
    error.value = null;
  }

  async function openBatch(id, paperId = null) {
    batchId.value = id;
    loading.value = true;
    error.value = null;
    try {
      papers.value = (await api.get(`/papers?batch_id=${id}`)) || [];
      const target = paperId || papers.value[0]?.id || null;
      if (target) await selectPaper(target);
    } catch (err) {
      if (err instanceof StaleContextError) return;
      reset();
      error.value = err?.message || "加载评分工作区失败";
    } finally {
      loading.value = false;
    }
  }

  async function selectPaper(paperId) {
    currentPaperId.value = paperId;
    activeAnchorId.value = null;
    activeQuote.value = null;
    try {
      const runs = (await api.get(`/scoring-runs?paper_id=${paperId}`)) || [];
      currentRunId.value = runs[0]?.id ?? null;
      if (!currentRunId.value) {
        // 未评分的材料不是错误状态，只是还没有结果可看。
        items.value = [];
        documentView.value = null;
      } else {
        items.value =
          (await api.get(
            `/scoring-runs/${currentRunId.value}/items?include_view=true`,
          )) || [];
        documentView.value = await api.get(
          `/scoring-runs/${currentRunId.value}/document-view`,
        );
      }
      neighbors.value = await api.get(
        `/papers/${paperId}/neighbors?batch_id=${batchId.value}`,
      );
    } catch (err) {
      if (!(err instanceof StaleContextError)) {
        error.value = err?.message || "加载评分记录失败";
      }
    }
  }

  /** 点证据 chip：设定锚点让中间栏定位。定位失败的证据不设高亮引文。 */
  function locate({ anchor_id, quote }) {
    activeAnchorId.value = anchor_id ?? null;
    activeQuote.value = quote ?? null;
  }

  async function loadAnchorPage(anchorId) {
    if (!currentRunId.value) return;
    documentView.value = await api.get(
      `/scoring-runs/${currentRunId.value}/document-view?anchor_id=${encodeURIComponent(anchorId)}`,
    );
  }

  return {
    batchId,
    papers,
    currentPaperId,
    currentPaper,
    currentRunId,
    items,
    documentView,
    documentBlocks,
    documentFrozen,
    documentUnavailableReason,
    provenanceNotice,
    neighbors,
    loading,
    error,
    activeAnchorId,
    activeQuote,
    totalScore,
    maxTotal,
    openBatch,
    selectPaper,
    locate,
    loadAnchorPage,
    reset,
  };
});
