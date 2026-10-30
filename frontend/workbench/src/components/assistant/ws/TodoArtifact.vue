<script setup>
/**
 * 待办清单（`todo` 卡片）：按最近的计划实时算，完成一项自动勾选；排在最前、现在就能做的那一项有按钮。
 */
import { computed } from "vue";
import { useRouter } from "vue-router";

import { latestPlan, todoItems } from "@/lib/assistant-workspace.js";
import { useAssistantStore } from "@/stores/assistant.js";

import WsLayout from "./WsLayout.vue";

defineProps({ head: { type: Object, required: true } });

const router = useRouter();
const store = useAssistantStore();
const plan = computed(() => latestPlan(store.messages)?.card || null);
const items = computed(() => todoItems(plan.value, store.pending));
const disabled = computed(() => store.sending || store.uploading);

function act(action) {
  if (action.kind === "open") store.openStep(action.step);
  if (action.kind === "confirm") store.answerConfirm("confirm");
  if (action.kind === "link") window.open(router.resolve(action.path).href, "_blank", "noopener");
}
function onPick(event) {
  const files = Array.from(event.target.files || []);
  event.target.value = "";
  if (files.length) store.attachFiles(files).catch((err) => store.reportError(err));
}
</script>

<template>
  <WsLayout v-bind="head" meta="实时更新">
    <div class="body">
      <div v-if="items.length" class="row-list">
        <div v-for="(item, index) in items" :key="item.key" class="row todo" :class="{ 'is-active': item.isNext }">
          <span class="mark" :class="{ done: item.done, next: item.isNext, failed: item.failed }">
            {{ item.done ? "✓" : item.failed ? "!" : index + 1 }}
          </span>
          <div class="row-main">
            <div class="todo-label" :class="{ done: item.done, next: item.isNext, later: !item.done && !item.isNext }">{{ item.label }}</div>
            <div v-if="item.detail" class="row-sub">{{ item.detail }}</div>
          </div>
          <label v-if="item.action?.kind === 'pick'" class="btn btn-primary btn-sm pick">
            <input type="file" multiple accept=".docx,.pdf" :disabled="disabled" @change="onPick" />{{ item.action.label }}
          </label>
          <button v-else-if="item.action" class="btn btn-primary btn-sm" type="button" :disabled="disabled" @click="act(item.action)">
            {{ item.action.label }}
          </button>
        </div>
      </div>
      <p v-else class="faint empty">现在没有进行中的评分。</p>
    </div>
  </WsLayout>
</template>

<style scoped>
.body { padding: 14px 24px 0; }
.todo { padding: 12px 14px; }
.mark { flex: none; width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center;
  font-family: var(--font-mono); font-size: 11px; border: 1.5px solid #cfcec7; color: var(--text-faint); background: var(--surface); }
.mark.next { border-color: var(--accent); color: var(--accent); }
.mark.done { border-color: var(--accent); background: var(--accent); color: #fff; }
.mark.failed { border-color: var(--danger); color: var(--danger); }
.todo-label { font-size: 13.5px; }
.todo-label.done { color: var(--text-muted); }
.todo-label.next { font-weight: 600; }
.todo-label.later { color: var(--text-faint); }
.pick { position: relative; overflow: hidden; color: #fff; }
.pick input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
.empty { font-size: 13px; }
</style>
