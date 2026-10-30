<script setup>
/**
 * 评分进度：每份一行，评完显示分数，评分中、排队中、失败照实写。
 * 流程里由 store 轮询作业（`watchJob`）；查询出来的进度标签没有流程，作业没结束时这里自己低频刷新。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { api, StaleContextError } from "@/api/client.js";
import { ACTIVE_JOB_STATUSES, finishedCount, jobPercent, jobStatusLabel } from "@/lib/score-jobs.js";
import { useAssistantStore } from "@/stores/assistant.js";

import WsLayout from "./WsLayout.vue";

const props = defineProps({
  batchId: { type: String, required: true },
  head: { type: Object, required: true },
  /** 流程里由 store 轮询；结果标签自己刷新。 */
  selfPoll: { type: Boolean, default: false },
});

const POLL_MS = 5000;
const store = useAssistantStore();
const summary = ref(null);
const loadError = ref(null);
let timer = 0;

const job = computed(() => store.jobs[props.batchId] || null);
const active = computed(() => Boolean(job.value && ACTIVE_JOB_STATUSES.has(job.value.status)));

async function loadJob() {
  try {
    store.jobs[props.batchId] = await api.get(`/batches/${props.batchId}/score-jobs/latest`);
  } catch (err) {
    if (!(err instanceof StaleContextError)) loadError.value = "无法加载评分进度";
  }
}
async function loadSummary() {
  try {
    summary.value = await api.get(`/batches/${props.batchId}/summary`);
  } catch (err) {
    if (!(err instanceof StaleContextError)) loadError.value = "无法加载评分结果";
  }
}
function schedule() {
  window.clearTimeout(timer);
  if (!props.selfPoll) return;
  timer = window.setTimeout(async () => {
    if (document.visibilityState !== "hidden") await loadJob();
    if (active.value) schedule();
  }, POLL_MS);
}
onMounted(async () => {
  if (!job.value) await loadJob();
  await loadSummary();
  if (active.value) schedule();
});
onBeforeUnmount(() => window.clearTimeout(timer));
// 每多评完一份，重新取一次分数。
watch(() => finishedCount(job.value), () => loadSummary());

const rows = computed(() => {
  const items = new Map((job.value?.items || []).map((item) => [item.paper_id, item.status]));
  return (summary.value?.papers || []).slice().reverse().map((paper, index) => {
    const status = items.get(paper.paper_id);
    const score = paper.latest_final_score;
    const done = status ? ["succeeded", "skipped"].includes(status) : typeof score === "number";
    return {
      key: paper.paper_id, idx: String(index + 1).padStart(2, "0"),
      title: paper.title || paper.file_name, name: paper.student_name || paper.file_name,
      done, score: done && typeof score === "number" ? score : null,
      review: done && paper.latest_need_manual_review,
      running: status === "running",
      state: status === "running" ? "评分中…" : status === "failed" ? "失败" : status === "canceled" ? "已取消" : "排队中",
      tone: status === "running" ? "accent" : status === "failed" ? "danger" : "ghost",
    };
  });
});

const total = computed(() => job.value?.total_items || rows.value.length);
const doneCount = computed(() => (job.value ? finishedCount(job.value) : rows.value.filter((row) => row.done).length));
const eta = computed(() => {
  if (!job.value) return "";
  if (!active.value) return jobStatusLabel(job.value);
  // 粗估：每份约半分钟（设计稿口径），只作参考。
  const left = Math.max(0, total.value - doneCount.value);
  return `预计还需 ${Math.max(1, Math.ceil(left * 0.5))} 分钟`;
});
const footText = computed(() => {
  if (!job.value) return "";
  if (active.value) return `已完成 ${doneCount.value}/${total.value}`;
  const failed = job.value.failed_count || 0;
  return failed ? `评分结束，${failed} 份没有评完，原因见对话。` : "评分已完成。";
});
</script>

<template>
  <WsLayout v-bind="head">
    <div class="body">
      <div class="headline">
        <span class="mono big">{{ doneCount }}</span>
        <span class="of">/ {{ total }} 份已完成</span>
        <span class="eta">{{ eta }}</span>
      </div>
      <div class="bar big-bar"><span :style="{ width: `${job ? jobPercent(job) : 0}%` }"></span></div>
      <div v-if="rows.length" class="row-list">
        <div v-for="row in rows" :key="row.key" class="row" :class="{ 'is-active': row.running }">
          <span class="row-idx">{{ row.idx }}</span>
          <div class="row-main">
            <div class="row-title" :class="{ pending: !row.done && !row.running }">{{ row.title }}</div>
            <div class="row-sub">{{ row.name }}</div>
          </div>
          <span v-if="row.review" class="tag tag-warn">需复核</span>
          <span v-if="row.done" class="mono score">{{ row.score ?? "—" }}</span>
          <span v-else class="state" :class="`tone-${row.tone}`">{{ row.state }}</span>
        </div>
      </div>
      <p v-if="loadError" class="notice notice-danger">{{ loadError }}</p>
    </div>
    <template v-if="footText" #foot>
      <p class="foot-text">{{ footText }}</p>
    </template>
  </WsLayout>
</template>

<style scoped>
.body { padding: 14px 24px 0; }
.headline { display: flex; align-items: baseline; flex-wrap: wrap; gap: 10px; }
.big { font-size: 28px; font-weight: 600; }
.of { font-size: 14px; color: var(--text-muted); }
.eta { margin-left: auto; font-size: 12.5px; color: var(--text-muted); }
.big-bar { margin: 10px 0 16px; }
.row-title.pending { color: var(--text-muted); }
.score { flex: none; min-width: 28px; text-align: right; font-size: 15px; font-weight: 600; }
.state { flex: none; font-size: 12px; }
.tone-accent { color: var(--accent); }
.tone-danger { color: var(--danger); }
.tone-ghost { color: var(--text-ghost); }
</style>
