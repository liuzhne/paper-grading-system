<script setup>
/**
 * 进展快照（`progress_snapshot` 卡片）：提问时计划走到哪一步、完成了哪些、之后还有哪些。
 * 快照不随后续变化；“刷新”只在本页按最新的计划重算，不落库（v2 界面改造方案 §6）。
 */
import { computed, ref } from "vue";

import { STEPS, clockText, currentStep, latestPlan } from "@/lib/assistant-workspace.js";
import { useAssistantStore } from "@/stores/assistant.js";

import WsLayout from "./WsLayout.vue";

const props = defineProps({
  card: { type: Object, required: true },
  head: { type: Object, required: true },
});

const store = useAssistantStore();
/** 刷新后的快照；null 时用卡片里存的那份。 */
const fresh = ref(null);

const snap = computed(() => fresh.value || {
  steps: props.card.steps || [], status: props.card.plan_status, job: props.card.job, asOf: props.card.as_of,
});

function refresh() {
  const plan = latestPlan(store.messages)?.card;
  if (!plan) return;
  const job = props.card.batch_id ? store.jobs[props.card.batch_id] : null;
  fresh.value = {
    steps: plan.steps || [], status: plan.status, asOf: new Date().toISOString(),
    job: job ? { status: job.status, total: job.total_items, failed: job.failed_count,
      done: (job.succeeded_count || 0) + (job.skipped_count || 0) } : snap.value.job,
  };
}

const steps = computed(() => snap.value.steps.map((step, index) => ({ ...step, num: String(index + 1).padStart(2, "0") })));
const current = computed(() => currentStep(steps.value));
const allDone = computed(() => snap.value.status === "done" || !current.value);
/** 后台自己走的步骤；其余的当前步骤都在等老师。 */
const BACKGROUND_STEPS = new Set(["upload", "scoring", "report"]);
const pill = computed(() => {
  if (allDone.value) return { text: "已完成", tone: "tag-accent" };
  if (current.value?.status === "failed") return { text: "没完成", tone: "tag-danger" };
  return current.value?.status === "blocked" || !BACKGROUND_STEPS.has(current.value?.key)
    ? { text: "等你处理", tone: "tag-warn" } : { text: "进行中", tone: "tag-accent" };
});
const job = computed(() => snap.value.job);
const pct = computed(() => (current.value?.key === "scoring" && job.value?.total ? Math.round((job.value.done / job.value.total) * 100) : null));
const detail = computed(() => {
  if (allDone.value) return `${steps.value.length} 个步骤已全部完成。`;
  if (current.value?.key === "scoring" && job.value?.total) return `已评完 ${job.value.done}/${job.value.total} 份。`;
  return current.value?.detail || STEPS[current.value?.key]?.desc || "";
});
const done = computed(() => steps.value.filter((step) => step.status === "done"));
const rest = computed(() => steps.value.filter((step) => step.status !== "done" && step.key !== current.value?.key));
const meta = computed(() => `截至 ${clockText(snap.value.asOf)} 的快照`);
</script>

<template>
  <WsLayout v-bind="head" :meta="meta">
    <template #head-actions>
      <button class="text-btn" type="button" @click="refresh">刷新</button>
    </template>
    <div class="body">
      <div class="now">
        <div class="now-head">
          <span class="mono num">{{ allDone ? "✓" : current?.num }}</span>
          <strong>{{ allDone ? "全部完成" : current?.label }}</strong>
          <span class="pill" :class="pill.tone">{{ pill.text }}</span>
        </div>
        <p class="detail">{{ detail }}</p>
        <div v-if="pct != null" class="bar"><span :style="{ width: `${pct}%` }"></span></div>
        <button v-if="!allDone && current" class="btn btn-sm btn-outline go" type="button" @click="store.openStep(current.key)">
          {{ pill.text === "等你处理" ? "去处理" : "查看这一步" }}
        </button>
      </div>
      <div class="cols">
        <div>
          <div class="col-head">已完成 · {{ done.length }}</div>
          <div v-for="step in done" :key="step.key" class="line"><span class="ok">✓</span><span>{{ step.label }}</span></div>
        </div>
        <div>
          <div class="col-head">之后 · {{ rest.length }}</div>
          <div v-for="step in rest" :key="step.key" class="line faint"><span class="mono">{{ step.num }}</span><span>{{ step.label }}</span></div>
          <div v-if="!rest.length" class="line faint">无</div>
        </div>
      </div>
    </div>
  </WsLayout>
</template>

<style scoped>
.body { padding: 14px 24px 0; display: flex; flex-direction: column; gap: 20px; }
.now { padding: 14px 16px; border: 1px solid var(--border-light); border-radius: var(--radius-md); }
.now-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.now-head strong { font-size: 15px; font-weight: 650; }
.now-head .pill { margin-left: auto; }
.num { font-size: 12px; color: var(--text-faint); }
.detail { margin: 8px 0 0; font-size: 13px; line-height: 1.7; color: var(--text-secondary); }
.now .bar { margin-top: 10px; }
.go { margin-top: 12px; font-weight: 600; }
.cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px 24px; }
.col-head { margin-bottom: 6px; font-size: 12.5px; font-weight: 650; color: var(--text-secondary); }
.line { display: flex; align-items: baseline; gap: 8px; padding: 4px 0; font-size: 13px; }
.line .mono { font-size: 11px; }
.ok { color: var(--accent); font-size: 12px; }
</style>
