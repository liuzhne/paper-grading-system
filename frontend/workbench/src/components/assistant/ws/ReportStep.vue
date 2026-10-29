<script setup>
/**
 * 评分结果汇报：三格统计、分数分布、建议人工复核（带原因）、全部成绩。只用批次汇总里的现成分数（B8）。
 */
import { computed, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";

import { api, StaleContextError } from "@/api/client.js";
import { papersInUploadOrder, scoreOverview } from "@/lib/assistant-flow.js";
import { scoreBands } from "@/lib/assistant-workspace.js";
import { useAssistantStore } from "@/stores/assistant.js";

import WsLayout from "./WsLayout.vue";

const props = defineProps({
  batchId: { type: String, required: true },
  head: { type: Object, required: true },
});

const router = useRouter();
const store = useAssistantStore();
const summary = ref(null);
const loadError = ref(null);

async function load() {
  try {
    summary.value = await api.get(`/batches/${props.batchId}/summary`);
  } catch (err) {
    if (!(err instanceof StaleContextError)) loadError.value = "无法加载评分结果";
  }
}
onMounted(load);
watch(() => [props.batchId, store.jobs[props.batchId]?.status], load);

const papers = computed(() => papersInUploadOrder(summary.value?.papers || [])
  .map((paper, index) => ({ ...paper, ordinal: index + 1 })));
const overview = computed(() => scoreOverview(summary.value?.papers || []));
const stats = computed(() => [
  { label: "平均分", value: overview.value.average ?? "—" },
  { label: "最高分", value: overview.value.max ?? "—" },
  { label: "最低分", value: overview.value.min ?? "—" },
]);
const bands = computed(() => scoreBands(papers.value.map((paper) => paper.latest_final_score)));
const reviews = computed(() => papers.value.filter((paper) => paper.latest_need_manual_review)
  .sort((a, b) => (a.latest_final_score ?? 0) - (b.latest_final_score ?? 0)));
const href = (path) => router.resolve(path).href;
</script>

<template>
  <WsLayout v-bind="head">
    <div v-if="summary" class="body">
      <div class="stats">
        <div v-for="stat in stats" :key="stat.label" class="stat">
          <div class="stat-label">{{ stat.label }}</div>
          <div class="mono stat-value">{{ stat.value }}</div>
        </div>
      </div>

      <section>
        <div class="sec-head"><strong>分数分布</strong><span>{{ overview.scored }}/{{ overview.total }} 份已出分</span></div>
        <div class="bands">
          <div v-for="band in bands" :key="band.label" class="band">
            <span class="mono band-label">{{ band.label }}</span>
            <span class="band-track"><span :style="{ width: `${band.pct}%` }"></span></span>
            <span class="mono band-n">{{ band.n }}</span>
          </div>
        </div>
      </section>

      <section v-if="reviews.length">
        <div class="sec-head"><strong>建议人工复核</strong><span>{{ reviews.length }} 份</span></div>
        <div class="reviews">
          <div v-for="paper in reviews" :key="paper.paper_id" class="review">
            <div class="row-main">
              <div class="review-title">{{ paper.title || paper.file_name }}</div>
              <div class="row-sub">第 {{ paper.ordinal }} 份 · {{ paper.student_name || paper.file_name }}</div>
              <p v-for="(reason, index) in paper.latest_review_reasons || []" :key="index" class="reason">{{ reason }}</p>
            </div>
            <span class="mono review-score">{{ paper.latest_final_score ?? "—" }}</span>
          </div>
        </div>
      </section>

      <section>
        <div class="sec-head"><strong>全部成绩</strong></div>
        <div class="row-list">
          <div v-for="paper in papers" :key="paper.paper_id" class="row">
            <span class="row-idx">{{ String(paper.ordinal).padStart(2, "0") }}</span>
            <span class="row-main row-title">{{ paper.title || paper.file_name }}<span class="faint who">{{ paper.student_name || "" }}</span></span>
            <span class="mono total">{{ paper.latest_final_score ?? "—" }}</span>
          </div>
        </div>
      </section>
    </div>
    <p v-if="loadError" class="notice notice-danger pad">{{ loadError }}</p>

    <template #foot>
      <p class="foot-text">成绩已保存，可在结果复核中逐份查看、改分。</p>
      <a class="btn" :href="href('/exports')" target="_blank" rel="noopener">导出成绩表</a>
      <a class="btn btn-primary" :href="href(`/batches/${batchId}/grade`)" target="_blank" rel="noopener">前往结果复核</a>
    </template>
  </WsLayout>
</template>

<style scoped>
.body { padding: 14px 24px 0; display: flex; flex-direction: column; gap: 2px; }
.pad { margin: 14px 24px; }
.stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1px; background: var(--border-light);
  border: 1px solid var(--border-light); border-radius: var(--radius-md); overflow: hidden; }
.stat { background: var(--surface); padding: 14px 16px; }
.stat-label { font-size: 12px; color: var(--text-muted); }
.stat-value { margin-top: 4px; font-size: 26px; font-weight: 600; }
.bands { display: flex; flex-direction: column; gap: 9px; }
.band { display: grid; grid-template-columns: 62px minmax(0, 1fr) 24px; align-items: center; gap: 10px; font-size: 12.5px; }
.band-label { color: var(--neutral-ink); }
.band-track { display: block; height: 10px; border-radius: 3px; background: var(--border-row); overflow: hidden; }
.band-track > span { display: block; height: 100%; border-radius: 3px; background: var(--accent); }
.band-n { text-align: right; }
.reviews { display: flex; flex-direction: column; gap: 8px; }
.review { display: flex; gap: 12px; align-items: flex-start; padding: 12px 14px; border: 1px solid var(--warn-border);
  background: var(--warn-surface); border-radius: var(--radius-md); }
.review-title { font-size: 13.5px; font-weight: 600; }
.reason { margin: 6px 0 0; font-size: 12.5px; line-height: 1.6; color: var(--warn-ink); }
.review-score { flex: none; font-size: 18px; font-weight: 600; }
.who { margin-left: 8px; }
.total { flex: none; font-size: 14px; font-weight: 600; }
.row-list .row { padding: 9px 14px; font-size: 13px; }
</style>
