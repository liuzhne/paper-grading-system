<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RouterLink } from "vue-router";

import { api, StaleContextError } from "@/api/client.js";
import { ACTIVE_JOB_STATUSES, jobPercent, jobStatusLabel } from "@/lib/score-jobs.js";

const jobs = ref([]);
const batches = ref([]);
const loading = ref(true);
const error = ref(null);
let timer = null;

const batchById = computed(() => Object.fromEntries(batches.value.map((item) => [item.id, item])));
const activeTotal = computed(() => jobs.value.filter((job) => ACTIVE_JOB_STATUSES.has(job.status)).length);

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN");
}

async function load({ quiet = false } = {}) {
  if (!quiet) loading.value = true;
  try {
    const [jobRows, batchRows] = await Promise.all([
      api.get("/batch-scoring-jobs"),
      api.get("/batches"),
    ]);
    jobs.value = jobRows || [];
    batches.value = batchRows || [];
    error.value = null;
  } catch (err) {
    if (!(err instanceof StaleContextError)) error.value = err?.message || "加载运行任务失败";
  } finally {
    loading.value = false;
  }
}

onMounted(async () => {
  await load();
  timer = window.setInterval(() => load({ quiet: true }), 5000);
});
onBeforeUnmount(() => window.clearInterval(timer));
</script>

<template>
  <div>
    <RouterLink class="faint back" :to="{ name: 'tasks' }">← 返回评分任务</RouterLink>
    <header class="page-head">
      <p class="page-eyebrow">评审 · 运行状态</p>
      <h1 class="page-title">正在评分 {{ activeTotal }}</h1>
      <p class="page-sub">页面会自动更新。关闭浏览器不会中断后台评分。</p>
    </header>

    <p v-if="error" class="notice notice-danger" role="alert">{{ error }}</p>
    <div class="card table-wrap">
      <table class="table">
        <thead><tr><th>任务</th><th>状态</th><th>进度</th><th>失败</th><th>最后更新</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="job in jobs" :key="job.id">
            <td>
              <strong>{{ batchById[job.grading_batch_id]?.name || "评分任务" }}</strong>
              <div class="faint mono sub">第 {{ job.generation }} 次执行</div>
            </td>
            <td><span class="chip" :class="job.heartbeat_state === 'stale' || ['failed','completed_with_errors'].includes(job.status) ? 'chip-warn' : 'chip-ok'">{{ jobStatusLabel(job) }}</span></td>
            <td class="mono">{{ jobPercent(job) }}% · {{ job.succeeded_count + job.skipped_count }}/{{ job.total_items }}</td>
            <td class="mono" :class="{ danger: job.failed_count }">{{ job.failed_count }}</td>
            <td class="mono faint">{{ formatTime(job.heartbeat_at || job.updated_at) }}</td>
            <td><RouterLink class="btn btn-sm" :to="{ name: 'task-run', params: { batchId: job.grading_batch_id } }">查看进度</RouterLink></td>
          </tr>
          <tr v-if="!jobs.length && !loading"><td colspan="6" class="table-empty">当前没有正在执行或需要处理的评分任务。</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.back { display: inline-block; margin-bottom: 18px; font-size: 13px; }
.sub { margin-top: 4px; font-size: 11.5px; }
.danger { color: var(--danger); }
</style>
