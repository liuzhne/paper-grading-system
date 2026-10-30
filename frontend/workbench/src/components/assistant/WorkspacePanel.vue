<script setup>
/**
 * 右侧工作区（评分助手 v2 设计稿）：标签（任务 + 本会话问出来的结果）、任务标签下的步骤条、新结果提示、
 * “正在查看已完成的步骤”提示，以及当前视图。标签与步骤都从消息算出（`lib/assistant-workspace.js`）。
 */
import { computed, ref } from "vue";

import {
  STEPS, TAB_KINDS, artifactsFrom, clockText, currentStep, flowContext, stepEyebrow, stepper, tabLayout, viewStepKey,
  viewingPast,
} from "@/lib/assistant-workspace.js";
import { useAssistantStore } from "@/stores/assistant.js";

import FilesStep from "./ws/FilesStep.vue";
import GradingStep from "./ws/GradingStep.vue";
import ModelStep from "./ws/ModelStep.vue";
import PaperArtifact from "./ws/PaperArtifact.vue";
import ProgressArtifact from "./ws/ProgressArtifact.vue";
import ReportStep from "./ws/ReportStep.vue";
import ReviewStep from "./ws/ReviewStep.vue";
import RubricStep from "./ws/RubricStep.vue";
import SourceStep from "./ws/SourceStep.vue";
import TodoArtifact from "./ws/TodoArtifact.vue";

const props = defineProps({
  /** 工作区实际宽度：决定放几个标签、步骤条显不显示名字、核对评分规则用单栏还是双栏。 */
  width: { type: Number, default: 560 },
  overlay: Boolean,
  focus: Boolean,
});

const store = useAssistantStore();
const moreOpen = ref(false);

const context = computed(() => flowContext(store.messages));
const plan = computed(() => context.value.plan);
const artifacts = computed(() => artifactsFrom(store.messages));
const ids = computed(() => [...(plan.value ? ["task"] : []), ...artifacts.value.map((item) => item.id)]);
const active = computed(() => {
  if (ids.value.includes(store.ws.tab)) return store.ws.tab;
  return ids.value[0] || null;
});
const layout = computed(() => tabLayout(ids.value, active.value, props.width));
const artifact = computed(() => artifacts.value.find((item) => item.id === active.value) || null);

const allDone = computed(() => plan.value?.status === "done");
const current = computed(() => currentStep(plan.value?.steps || []));
const viewKey = computed(() => viewStepKey(plan.value, store.ws.step));
const steps = computed(() => stepper(plan.value?.steps || [], viewKey.value, props.width >= 820));
const past = computed(() => active.value === "task" && viewingPast(plan.value, viewKey.value));

function tabOf(id) {
  if (id === "task") {
    const index = (plan.value?.steps || []).findIndex((step) => step.key === current.value?.key);
    return { id, kind: TAB_KINDS.task, title: "评分任务",
      sub: allDone.value ? "完成" : plan.value?.status === "running" && index >= 0 ? `${index + 1}/${plan.value.steps.length}` : "",
      subTone: allDone.value ? "faint" : "warn" };
  }
  const item = artifacts.value.find((entry) => entry.id === id);
  return { id, kind: TAB_KINDS[item.kind], title: item.title,
    sub: item.kind === "progress" ? clockText(item.card.as_of || item.time) : "", subTone: "faint" };
}
const visibleTabs = computed(() => layout.value.visible.map(tabOf));
const moreTabs = computed(() => layout.value.more.map(tabOf));

function openTab(id) {
  moreOpen.value = false;
  if (id === "task") store.openStep(store.ws.step);
  else store.openArtifact(id);
}
function openStep(item) {
  if (!item.reachable) return;
  // 点当前步骤就是“跟着当前步骤走”，流程往下推进时视图跟着走（设计稿 follow）。
  store.openStep(item.key === current.value?.key ? null : item.key);
}

const toastTitle = computed(() => {
  if (!store.ws.toast || store.ws.toast === active.value) return "";
  return artifacts.value.find((item) => item.id === store.ws.toast)?.title || "";
});

const stepHead = computed(() => {
  const key = viewKey.value;
  const step = (plan.value?.steps || []).find((item) => item.key === key);
  const eyebrow = stepEyebrow(plan.value, key, store.pending?.kind);
  const failed = step?.status === "failed" || step?.status === "blocked";
  return { eyebrow: eyebrow.text, eyebrowTone: eyebrow.tone, title: STEPS[key]?.title || step?.label || "",
    desc: failed && step?.detail ? step.detail : STEPS[key]?.desc || "" };
});
const view = computed(() => STEPS[viewKey.value]?.view || null);
const artifactHead = computed(() => {
  const item = artifact.value;
  if (!item) return {};
  return { tag: TAB_KINDS[item.kind], title: item.title,
    meta: item.kind === "todo" ? "" : `来自 ${clockText(item.time)} 的提问` };
});
const backLabel = computed(() => {
  const index = (plan.value?.steps || []).findIndex((step) => step.key === current.value?.key);
  return current.value ? `回到当前：${index + 1} ${current.value.label}` : "回到当前";
});
</script>

<template>
  <section class="workspace" :class="{ overlay, focus }" aria-label="工作区">
    <div class="ws-top">
      <div class="tabs-row">
        <div class="tabs" role="tablist">
          <button v-for="tab in visibleTabs" :key="tab.id" class="tab" :class="{ on: tab.id === active }" type="button"
                  role="tab" :aria-selected="tab.id === active" :title="tab.title" @click="openTab(tab.id)">
            <span class="kind" :class="`tone-${tab.kind.tone}`">{{ tab.kind.tag }}</span>
            <span class="tab-title">{{ tab.title }}</span>
            <span v-if="tab.sub && (tab.id === active || tab.id === 'task')" class="mono tab-sub" :class="`tone-${tab.subTone}`">{{ tab.sub }}</span>
          </button>
        </div>
        <div v-if="moreTabs.length" class="more">
          <button class="btn btn-sm more-btn" :class="{ open: moreOpen }" type="button" :aria-expanded="moreOpen" @click="moreOpen = !moreOpen">
            更多 · {{ moreTabs.length }}
          </button>
          <div v-if="moreOpen" class="more-menu" role="menu">
            <button v-for="tab in moreTabs" :key="tab.id" class="more-item" type="button" role="menuitem" @click="openTab(tab.id)">
              <span class="kind" :class="`tone-${tab.kind.tone}`">{{ tab.kind.tag }}</span>
              <span class="tab-title">{{ tab.title }}</span>
              <span v-if="tab.sub" class="mono tab-sub tone-faint">{{ tab.sub }}</span>
            </button>
          </div>
        </div>
        <div class="tools">
          <button v-if="!overlay" class="btn btn-sm" type="button" @click="store.ws.mode = focus ? 'open' : 'focus'">{{ focus ? "还原" : "全宽" }}</button>
          <button class="btn btn-sm" type="button" @click="store.ws.mode = 'closed'">{{ overlay ? "返回对话" : "收起" }}</button>
        </div>
      </div>
      <div v-if="active === 'task' && plan" class="stepper" aria-label="评分步骤">
        <button v-for="item in steps" :key="item.key" class="step" :class="[`st-${item.status}`, { on: item.isView, wide: item.wide }]"
                type="button" :disabled="!item.reachable" :title="item.full" :aria-current="item.isView ? 'step' : undefined"
                @click="openStep(item)">
          <span class="step-bar"></span>
          <span class="step-text"><span class="mono">{{ item.num }}</span><span v-if="item.showLabel" class="step-label">{{ item.short }}</span></span>
        </button>
      </div>
    </div>

    <div v-if="toastTitle" class="toast" role="status">
      <span class="toast-text">新结果：{{ toastTitle }}</span>
      <button type="button" class="toast-open" @click="store.openArtifact(store.ws.toast)">查看</button>
      <button type="button" class="toast-later" @click="store.ws.toast = null">稍后</button>
    </div>
    <div v-if="past" class="past">
      <span>正在查看已完成的步骤</span>
      <button type="button" class="text-btn" @click="store.openStep(null)">{{ backLabel }}</button>
    </div>

    <div class="ws-body">
      <template v-if="active === 'task' && plan">
        <SourceStep v-if="view === 'source'" :key="viewKey" :context="context" :head="stepHead" />
        <ReviewStep v-else-if="view === 'review'" :key="viewKey" :context="context" :head="stepHead" :wide="width >= 760" />
        <RubricStep v-else-if="view === 'rubric'" :key="viewKey" :context="context" :head="stepHead" />
        <ModelStep v-else-if="view === 'model'" :key="viewKey" :context="context" :head="stepHead" />
        <FilesStep v-else-if="view === 'files'" :key="'files'" :context="context" :head="stepHead" />
        <GradingStep v-else-if="view === 'grading' && context.batchId" :key="viewKey" :batch-id="context.batchId" :head="stepHead" />
        <ReportStep v-else-if="view === 'report' && context.batchId" :key="viewKey" :batch-id="context.batchId" :head="stepHead" />
        <div v-else class="empty"><p class="faint">这一步还没有内容。</p></div>
      </template>
      <template v-else-if="artifact">
        <ProgressArtifact v-if="artifact.card.type === 'progress_snapshot'" :key="artifact.id" :card="artifact.card" :head="artifactHead" />
        <PaperArtifact v-else-if="artifact.card.type === 'paper_result'" :key="artifact.id" :card="artifact.card" :head="artifactHead" />
        <TodoArtifact v-else-if="artifact.card.type === 'todo'" :key="artifact.id" :head="artifactHead" />
        <GradingStep v-else-if="artifact.card.type === 'job_progress'" :key="artifact.id" :batch-id="artifact.card.batch_id"
                     :head="artifactHead" self-poll />
        <ReportStep v-else :key="artifact.id" :batch-id="artifact.card.batch_id" :head="artifactHead" />
      </template>
      <div v-else class="empty">
        <strong>这次对话还没有结果</strong>
        <p>开始评分、查询进展或问某一份的扣分后，结果会显示在这里。</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.workspace { display: flex; flex-direction: column; min-width: 0; min-height: 0; height: 100%; background: var(--surface); border-left: 1px solid var(--border); }
.workspace.overlay { position: absolute; inset: 0; z-index: 5; border-left: 0; box-shadow: -12px 0 40px rgba(28, 33, 30, 0.18); }
.ws-top { flex: none; border-bottom: 1px solid var(--border-light); }
.tabs-row { display: flex; align-items: flex-end; gap: 10px; padding: 6px 16px 0 10px; }
.tabs { flex: 0 1 auto; min-width: 0; display: flex; gap: 2px; overflow: hidden; }
.tab { flex: 0 1 auto; min-width: 96px; max-width: 240px; height: 40px; padding: 0 10px; border: 0; border-bottom: 2px solid transparent;
  background: transparent; display: flex; align-items: center; gap: 7px; cursor: pointer; color: var(--neutral-ink); font: inherit; }
.tab.on { flex-shrink: 0; border-bottom-color: var(--text); color: var(--text); font-weight: 650; }
.kind { flex: none; font-size: 11px; padding: 1px 6px; border-radius: 4px; font-weight: 400; }
.tab-title { min-width: 0; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tab-sub { flex: none; font-size: 11px; font-weight: 400; }
.tone-accent.kind { background: var(--accent-soft); color: var(--accent); }
.tone-neutral.kind { background: var(--chip-bg); color: var(--text-secondary); }
.tone-info.kind { background: var(--info-bg); color: var(--info-ink); }
.tone-warn.kind { background: var(--warn-bg); color: var(--warn-ink); }
.tab-sub.tone-warn { color: var(--warn-ink); }
.tone-faint { color: var(--text-faint); }
.more { flex: none; position: relative; padding-bottom: 7px; }
.more-btn { height: 28px; }
.more-btn.open { background: var(--border-row); }
.more-menu { position: absolute; top: 34px; right: 0; z-index: 20; width: 280px; padding: 6px; display: flex; flex-direction: column; gap: 2px;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md); box-shadow: 0 12px 32px rgba(28, 33, 30, 0.14); }
.more-item { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border: 0; border-radius: 7px; background: transparent;
  text-align: left; cursor: pointer; font: inherit; color: var(--text); }
.more-item:hover { background: var(--bg); }
.more-item .tab-title { flex: 1; }
.tools { flex: none; margin-left: auto; display: flex; gap: 6px; padding-bottom: 7px; }
.tools .btn { height: 28px; }
.stepper { display: flex; gap: 4px; padding: 10px 20px 0; border-top: 1px solid var(--border-row); }
.step { flex: 1 1 0; min-width: 0; display: flex; flex-direction: column; gap: 7px; padding: 0 0 9px; border: 0;
  border-bottom: 2px solid transparent; background: transparent; cursor: pointer; text-align: left; font: inherit; color: var(--neutral-ink); }
.step.wide { flex: 3 1 0; }
.step:disabled { cursor: default; color: var(--text-ghost); }
.step.on { border-bottom-color: var(--text); color: var(--text); font-weight: 650; }
.step-bar { display: block; width: 100%; height: 3px; border-radius: 2px; background: var(--border); }
.st-done .step-bar { background: var(--accent); }
.st-current .step-bar, .st-blocked .step-bar { background: var(--amber); }
.st-failed .step-bar { background: var(--danger); }
.step-text { display: flex; gap: 5px; align-items: baseline; width: 100%; font-size: 12px; white-space: nowrap; overflow: hidden; }
.step-text .mono { font-size: 11px; }
.step-label { overflow: hidden; text-overflow: ellipsis; }
.toast { flex: none; display: flex; align-items: center; gap: 14px; padding: 9px 20px; background: var(--sidebar-bg); color: #fff; font-size: 12.5px; }
.toast-text { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.toast button { flex: none; padding: 0; border: 0; background: transparent; cursor: pointer; font: inherit; }
.toast-open { color: #9fd3c2; font-weight: 600; }
.toast-later { color: var(--sidebar-text-faint); }
.past { flex: none; display: flex; align-items: center; gap: 10px; padding: 8px 20px; background: var(--surface-muted);
  border-bottom: 1px solid var(--border-light); font-size: 12.5px; color: var(--neutral-ink); }
.past span { flex: 1; min-width: 0; }
.past .text-btn { font-weight: 600; }
.ws-body { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.empty { padding: 90px 32px; text-align: center; }
.empty strong { font-size: 15px; font-weight: 650; }
.empty p { margin: 8px 0 0; font-size: 12.5px; line-height: 1.8; color: var(--text-muted); }
</style>
