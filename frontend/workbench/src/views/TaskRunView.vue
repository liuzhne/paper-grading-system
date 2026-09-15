<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { api, ApiError, StaleContextError } from "@/api/client.js";
import { ACTIVE_JOB_STATUSES, itemStatusLabel, jobPercent, jobStatusLabel } from "@/lib/score-jobs.js";

const route = useRoute();
const batchId = String(route.params.batchId);
const batch = ref(null);
const summary = ref(null);
const job = ref(null);
const loading = ref(true);
const busy = ref(false);
const error = ref(null);
const feedback = ref(null);
let timer = null;

const paperById = computed(() => Object.fromEntries((summary.value?.papers || []).map((paper) => [paper.paper_id, paper])));
const canCancel = computed(() => ACTIVE_JOB_STATUSES.has(job.value?.status));
const canRetry = computed(() => ["completed_with_errors", "failed", "canceled"].includes(job.value?.status) && job.value?.items?.some((item) => ["failed", "canceled", "running"].includes(item.status)));
const complete = computed(() => job.value?.status === "completed");

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN");
}

function tone(status) {
  if (["failed", "canceled"].includes(status)) return "chip-danger";
  if (["pending", "cancel_requested", "completed_with_errors"].includes(status)) return "chip-warn";
  return "chip-ok";
}

async function refresh({ quiet = false } = {}) {
  if (!quiet) loading.value = true;
  try {
    const [batchData, summaryData, jobData] = await Promise.all([
      api.get(`/batches/${batchId}`),
      api.get(`/batches/${batchId}/summary`),
      api.get(`/batches/${batchId}/score-jobs/latest`),
    ]);
    batch.value = batchData;
    summary.value = summaryData;
    job.value = jobData;
    error.value = null;
  } catch (err) {
    if (!(err instanceof StaleContextError)) error.value = err instanceof ApiError ? err.detail || err.message : err?.message;
  } finally {
    loading.value = false;
  }
}

async function act(action) {
  busy.value = true;
  feedback.value = null;
  try {
    job.value = action === "cancel"
      ? await api.post(`/batch-scoring-jobs/${job.value.id}/cancel`, {})
      : await api.post(`/batch-scoring-jobs/${job.value.id}/retry`, {});
    feedback.value = action === "cancel" ? "已请求取消，正在完成已开始的材料。" : "失败项已重新排队。";
    await refresh({ quiet: true });
  } catch (err) {
    error.value = err instanceof ApiError ? err.detail || err.message : err?.message;
  } finally {
    busy.value = false;
  }
}

onMounted(async () => {
  await refresh();
  timer = window.setInterval(() => refresh({ quiet: true }), 3000);
});
onBeforeUnmount(() => window.clearInterval(timer));
</script>

<template>
  <div>
    <RouterLink class="faint back" :to="{ name: 'tasks' }">← 返回评分任务</RouterLink>
    <header class="page-head page-head-row">
      <div>
        <p class="page-eyebrow">评审 · 后台评分</p>
        <h1 class="page-title">{{ batch?.name || "评分进度" }}</h1>
        <p class="page-sub">{{ summary?.rubric_name || "—" }} · 页面每 3 秒自动更新</p>
      </div>
      <div v-if="job" class="btn-row">
        <button v-if="canCancel" class="btn btn-danger" type="button" :disabled="busy" @click="act('cancel')">取消剩余任务</button>
        <button v-if="canRetry" class="btn btn-primary" type="button" :disabled="busy" @click="act('retry')">重试失败项</button>
        <RouterLink v-if="complete" class="btn btn-primary" :to="{ name: 'grade', params: { batchId } }">查看评分结果</RouterLink>
      </div>
    </header>

    <p v-if="error" class="notice notice-danger" role="alert">{{ error }}</p>
    <p v-if="feedback" class="notice" role="status">{{ feedback }}</p>
    <p v-if="job?.heartbeat_state === 'stale'" class="notice notice-warn" role="status">执行中断，等待后台自动恢复。已有结果会保留，不会重复覆盖。</p>

    <template v-if="job">
      <section class="card card-pad">
        <div class="run-head">
          <div><span class="chip" :class="tone(job.status)">{{ jobStatusLabel(job) }}</span><span class="mono percent">{{ jobPercent(job) }}%</span></div>
          <div class="faint meta">开始 {{ formatTime(job.started_at || job.created_at) }} · 最后心跳 {{ formatTime(job.heartbeat_at) }}</div>
        </div>
        <div class="bar run-bar"><span class="fill" :style="{ width: `${jobPercent(job)}%` }"></span></div>
        <div class="stats">
          <div><span class="num">{{ job.total_items }}</span><span>总数</span></div>
          <div><span class="num">{{ job.pending_count }}</span><span>等待</span></div>
          <div><span class="num">{{ job.running_count }}</span><span>评分中</span></div>
          <div><span class="num">{{ job.succeeded_count + job.skipped_count }}</span><span>完成</span></div>
          <div><span class="num danger">{{ job.failed_count }}</span><span>失败</span></div>
          <div><span class="num">{{ job.canceled_count }}</span><span>取消</span></div>
        </div>
      </section>

      <section class="card table-wrap">
        <div class="card-head"><div><h2 class="card-title">材料进度</h2><p class="card-note">每份材料独立保存，单份失败不会清空其他结果。</p></div></div>
        <table class="table">
          <thead><tr><th>材料</th><th>状态</th><th>尝试次数</th><th>完成时间</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="item in job.items" :key="item.id">
              <td>{{ paperById[item.paper_id]?.file_name || "材料" }}</td>
              <td><span class="chip" :class="tone(item.status)">{{ itemStatusLabel(item.status) }}</span></td>
              <td class="mono">{{ item.attempt_count }}</td>
              <td class="mono faint">{{ formatTime(item.finished_at) }}</td>
              <td class="error-cell">{{ item.error_message || "—" }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>
    <p v-else-if="!loading && !error" class="notice">尚未创建后台评分任务。</p>
  </div>
</template>

<style scoped>
.back { display: inline-block; margin-bottom: 18px; font-size: 13px; }
.run-head { display: flex; justify-content: space-between; align-items: center; gap: 14px; flex-wrap: wrap; }
.percent { margin-left: 12px; font-size: 20px; }
.meta { font-size: 12px; }
.run-bar { margin: 18px 0 22px; width: 100%; }
.stats { display: grid; grid-template-columns: repeat(6, minmax(82px, 1fr)); gap: 12px; }
.stats > div { padding: 12px; border-radius: var(--radius); background: var(--surface-muted); }
.stats .num { display: block; font-size: 22px; margin-bottom: 5px; }
.stats span:last-child { color: var(--text-muted); font-size: 12px; }
.danger, .error-cell { color: var(--danger); }
.error-cell { max-width: 360px; font-size: 12px; line-height: 1.6; }
@media (max-width: 760px) { .stats { grid-template-columns: repeat(3, 1fr); } }
</style>
