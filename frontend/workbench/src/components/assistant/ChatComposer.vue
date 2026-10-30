<script setup>
/**
 * 输入区（评分助手 v2 设计稿）：任务条（“待你处理 · 确认开始评分 · 12 份 [开始评分]”）、建议问题、输入框。
 * 任务条的按钮与工作区底部操作条是同一个动作；建议问题带 `quick_action` 发出，后端不经规则与模型。
 */
import { computed, nextTick, ref } from "vue";

import { MAX_FILES } from "@/lib/assistant-flow.js";
import { chipsFor, flowContext, taskBar, viewStepKey } from "@/lib/assistant-workspace.js";
import { useAssistantStore } from "@/stores/assistant.js";
import { useUploadStore } from "@/stores/upload.js";

const store = useAssistantStore();
const upload = useUploadStore();
const draft = ref("");
const box = ref(null);
const attach = ref(null);

const context = computed(() => flowContext(store.messages));
const effective = computed(() => store.settings?.effective || null);
const bar = computed(() => {
  const visibleStep = store.ws.mode !== "closed" && store.ws.tab === "task" ? viewStepKey(context.value.plan, store.ws.step) : null;
  const done = upload.queue.filter((entry) => ["done", "failed", "rejected"].includes(entry.status)).length;
  return taskBar(context.value.plan, store.pending, {
    visibleStep, confirm: context.value.cards.confirm_start,
    job: context.value.batchId ? store.jobs[context.value.batchId] : null,
    upload: upload.queue.length ? { done, total: upload.queue.length } : null,
  });
});
const chips = computed(() => (store.sending ? [] : chipsFor(context.value.plan)));
const disabled = computed(() => store.sending || store.uploading);

function act(action) {
  if (action.kind === "open") store.openStep(action.step);
  if (action.kind === "pick") attach.value?.click();
  if (action.kind === "confirm") store.answerConfirm("confirm");
}

function resize() {
  const el = box.value;
  if (!el) return;
  el.style.height = "34px";
  el.style.height = `${Math.min(140, Math.max(34, el.scrollHeight))}px`;
}
async function submit() {
  const text = draft.value.trim();
  if (!text || store.sending) return;
  draft.value = "";
  await nextTick();
  resize();
  await store.send(text);
}
function onKeydown(event) {
  // 输入法合成中的回车是选词，不是发送。
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    submit();
  }
}
function onAttach(event) {
  const files = Array.from(event.target.files || []);
  event.target.value = "";
  if (files.length) store.attachFiles(files).catch((err) => store.reportError(err));
}
</script>

<template>
  <footer class="composer">
    <div class="column">
      <p v-if="effective?.slow" class="notice notice-warn slim">{{ effective.notice }}</p>
      <p v-if="store.error" class="notice notice-danger slim" role="alert">{{ store.error }}</p>

      <div v-if="bar" class="taskbar" :class="`tone-${bar.tone}`" data-test="task-bar">
        <span class="dot" aria-hidden="true"></span>
        <div class="bar-main">
          <div class="bar-text"><strong>{{ bar.label }}</strong><span>{{ bar.text }}</span></div>
          <div v-if="bar.pct != null" class="bar"><span :style="{ width: `${bar.pct}%` }"></span></div>
        </div>
        <button v-if="bar.action" class="btn btn-primary btn-sm" type="button" :disabled="disabled" @click="act(bar.action)">{{ bar.action.label }}</button>
        <span v-else-if="bar.note" class="bar-note">{{ bar.note }}</span>
      </div>

      <div v-if="chips.length" class="chips">
        <button v-for="chip in chips" :key="chip.intent" class="chip-q" type="button" :disabled="disabled"
                @click="store.send(chip.text, chip.intent)">{{ chip.label }}</button>
      </div>

      <div class="input-row">
        <label class="attach" :title="`添加待评分文件（最多 ${MAX_FILES} 份）`">
          <input ref="attach" type="file" multiple accept=".docx,.pdf" :disabled="disabled" @change="onAttach" />
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M20.5 11.5l-8.4 8.4a5 5 0 01-7.1-7.1l8.4-8.4a3.3 3.3 0 014.7 4.7l-8.4 8.4a1.7 1.7 0 01-2.4-2.4l7.7-7.7" />
          </svg>
          <span class="sr-only">添加待评分文件</span>
        </label>
        <textarea ref="box" v-model="draft" rows="1" maxlength="500" placeholder="问问当前步骤，或说“开始评分”…"
                  aria-label="输入消息" @input="resize" @keydown="onKeydown"></textarea>
        <button class="send" type="button" :disabled="store.sending || !draft.trim()" @click="submit">发送</button>
      </div>
    </div>
  </footer>
</template>

<style scoped>
.composer { flex: none; padding: 0 24px 18px; }
.column { max-width: 720px; margin: 0 auto; display: flex; flex-direction: column; gap: 8px; }
.slim { margin: 0; padding: 6px 10px; font-size: 13px; }
.taskbar { display: flex; align-items: center; gap: 12px; min-height: 50px; padding: 9px 9px 9px 14px; background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--radius-md); }
.dot { flex: none; width: 7px; height: 7px; border-radius: 50%; background: var(--amber); }
.tone-run .dot { background: var(--accent); }
.bar-main { flex: 1; min-width: 0; }
.bar-text { font-size: 13px; line-height: 1.5; }
.bar-text strong { margin-right: 8px; font-weight: 600; color: var(--warn-ink); }
.tone-run .bar-text strong { color: var(--accent); }
.bar-main .bar { height: 4px; margin-top: 7px; }
.bar-note { flex: none; padding-right: 6px; font-size: 12px; color: var(--text-muted); }
.taskbar .btn { height: 32px; font-weight: 600; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip-q { height: 28px; padding: 0 11px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface);
  color: var(--text-secondary); font: inherit; font-size: 12.5px; cursor: pointer; }
.chip-q:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.input-row { display: flex; align-items: flex-end; gap: 6px; padding: 7px 7px 7px 8px; background: var(--surface);
  border: 1px solid var(--border-input); border-radius: var(--radius-lg); }
.input-row:focus-within { border-color: var(--accent); }
.attach { position: relative; flex: none; width: 34px; height: 34px; display: grid; place-items: center; border-radius: 8px; color: var(--text-muted); cursor: pointer; }
.attach:hover { background: var(--border-row); }
.attach input { position: absolute; width: 1px; height: 1px; opacity: 0; }
textarea { flex: 1; min-width: 0; height: 34px; min-height: 34px; max-height: 140px; padding: 6px 2px; border: 0; outline: none; resize: none;
  overflow-y: auto; background: transparent; font-size: 14px; line-height: 1.6; color: var(--text); }
textarea:focus { box-shadow: none; }
.send { flex: none; height: 34px; padding: 0 16px; border: 0; border-radius: 8px; background: var(--accent); color: #fff;
  font: inherit; font-size: 13px; font-weight: 600; cursor: pointer; }
.send:disabled { background: var(--accent-disabled); cursor: default; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
</style>
