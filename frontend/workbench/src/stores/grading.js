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
  /** 当前 run 的篇章一致性与格式发现（中栏页签，设计稿「篇章结构 / 格式发现」）。 */
  const coherenceFindings = ref([]);
  const formatFindings = ref([]);
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
    coherenceFindings.value = [];
    formatFindings.value = [];
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
      // 指定的材料必须真的在这个批次里。复核队列的「查看原文」链接可能指向一份
      // 已被移出批次的材料，直接选中它会让列表**一项都不高亮**——界面看起来正常，
      // 只是没有任何一行是选中的，用户无从判断自己在看什么。
      const requested = papers.value.some((p) => p.id === paperId) ? paperId : null;
      const target = requested || papers.value[0]?.id || null;
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
      const run = runs[0] ?? null;
      currentRunId.value = run?.id ?? null;
      // 篇章与格式发现随 run 列表一起回来（`ScoringRunRead`），不必再发请求。
      // 历史 run 这两列可能是 null，`|| []` 兜住。
      coherenceFindings.value = run?.coherence_findings || [];
      formatFindings.value = run?.format_findings || [];
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

  /**
   * 改单项分（V3-5）。
   *
   * **理由必填**：改分会写进复核记录，没有理由的记录事后无法判断当初为什么改。
   * 并发冲突（409）原样抛出——要让用户看到并重新决定，而不是把别人的改动盖掉。
   *
   * @param {string} itemId
   * @param {{score: number, reason: string}} input
   */
  async function overrideScore(itemId, input) {
    const reason = (input.reason || "").trim();
    if (!reason) {
      throw new Error("请填写改分理由。");
    }
    const updated = await api.patch(`/score-items/${itemId}`, {
      final_score: input.score,
      reason,
    });
    // 就地更新，避免整页重载把用户的滚动位置和当前锚点丢掉。
    const index = items.value.findIndex((item) => item.id === itemId);
    if (index >= 0) items.value[index] = { ...items.value[index], ...updated };
    return updated;
  }

  /**
   * 提交这一份的复核（V3-5）。
   *
   * @param {string} runId
   * @param {string} reason
   */
  async function submitRunReview(runId, reason) {
    const trimmed = (reason || "").trim();
    if (!trimmed) {
      throw new Error("请填写复核理由。");
    }
    return api.post(`/scoring-runs/${runId}/review`, { reason: trimmed });
  }

  return {
    overrideScore,
    submitRunReview,
    batchId,
    papers,
    currentPaperId,
    currentPaper,
    currentRunId,
    coherenceFindings,
    formatFindings,
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
