<script setup>
/**
 * 评分助手（评分助手 v2 设计稿，docs/评分助手v2界面改造方案.md）：深色会话栏 · 对话 · 工作区。
 *
 * 工作区是原生面板：任务标签按计划步骤显示，问出来的结果另开标签；需要老师做的事在任务条与工作区底部操作条上。
 * 分隔条可拖（400px 到“总宽 − 400px”，记在本机，双击恢复 560）；窗口窄于 1000px 时工作区盖在对话上。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import AssistantSetupDialog from "@/components/AssistantSetupDialog.vue";
import ChatComposer from "@/components/assistant/ChatComposer.vue";
import ChatThread from "@/components/assistant/ChatThread.vue";
import ConversationRail from "@/components/assistant/ConversationRail.vue";
import WorkspacePanel from "@/components/assistant/WorkspacePanel.vue";
import { WS_DEFAULT, clampWorkspace, flowContext } from "@/lib/assistant-workspace.js";
import { useAssistantStore } from "@/stores/assistant.js";

const WIDTH_KEY = "pgs.assistant.wsWidth";
const OVERLAY_BELOW = 1000;
const RAIL_BELOW = 1240;
const FOCUS_CHAT = 380;

const store = useAssistantStore();
const route = useRoute();
const router = useRouter();

const loadError = ref(null);
const viewport = ref(window.innerWidth);
const railCollapsed = ref(window.innerWidth < RAIL_BELOW);
const main = ref(null);
const mainWidth = ref(1100);
const savedWidth = ref(readWidth());
const dragging = ref(false);
let observer = null;

function readWidth() {
  try {
    const value = Number(window.localStorage.getItem(WIDTH_KEY));
    return value >= 400 ? value : null;
  } catch {
    return null; // 隐私模式等读不到本机存储：用默认宽度
  }
}
function saveWidth(value) {
  try { window.localStorage.setItem(WIDTH_KEY, String(value)); } catch { /* 存不了就只在本次有效 */ }
}

const routeId = computed(() => (typeof route.params.conversationId === "string" ? route.params.conversationId : null));
const overlay = computed(() => viewport.value < OVERLAY_BELOW);
const shown = computed(() => store.ws.mode !== "closed");
const focus = computed(() => store.ws.mode === "focus" && !overlay.value);
const normalWidth = computed(() => clampWorkspace(savedWidth.value, mainWidth.value));
const wsWidth = computed(() => {
  if (!shown.value) return 0;
  if (overlay.value) return mainWidth.value;
  return focus.value ? mainWidth.value - FOCUS_CHAT : normalWidth.value;
});
const context = computed(() => flowContext(store.messages));
const stepBadge = computed(() => {
  const plan = context.value.plan;
  if (!plan) return "无结果";
  if (plan.status === "done") return "已完成";
  const index = (plan.steps || []).findIndex((step) => !["done", "skipped"].includes(step.status));
  return index >= 0 ? `${index + 1}/${plan.steps.length}` : "已结束";
});
const chatTitle = computed(() => store.conversation?.title || "新对话");
const modelLine = computed(() => store.settings?.effective?.label || "");

async function openRoute(id) {
  loadError.value = null;
  if (!id) {
    // 新对话还没有结果：工作区先收起，流程开始或第一个结果出来时再打开（设计稿 newChat）。
    store.clearConversation();
    store.ws.mode = "closed";
    return;
  }
  if (store.conversation?.id === id) return;
  try {
    await store.open(id);
  } catch {
    loadError.value = "会话不存在或无权访问。";
    store.clearConversation();
  }
}

function onResize() {
  viewport.value = window.innerWidth;
}
onMounted(async () => {
  window.addEventListener("resize", onResize);
  store.ws.narrow = overlay.value;
  if (overlay.value) store.ws.mode = "closed";
  if (main.value && window.ResizeObserver) {
    observer = new ResizeObserver(([entry]) => { mainWidth.value = Math.round(entry.contentRect.width); });
    observer.observe(main.value);
  }
  await Promise.all([store.loadSettings(), store.loadConversations()]);
  await openRoute(routeId.value);
});
onBeforeUnmount(() => {
  window.removeEventListener("resize", onResize);
  observer?.disconnect();
  store.stopWatchers();
});

watch(overlay, (value) => { store.ws.narrow = value; });
watch(routeId, (id) => openRoute(id));
// 第一句话时才建会话；建好后把地址换成这个会话，刷新能回来。
watch(() => store.conversation?.id, (id) => {
  if (id && id !== routeId.value) router.replace({ name: "assistant", params: { conversationId: id } });
});

function startDrag(event) {
  event.preventDefault();
  const rect = main.value?.getBoundingClientRect();
  if (!rect) return;
  const move = (moveEvent) => { savedWidth.value = clampWorkspace(Math.round(rect.right - moveEvent.clientX), rect.width); };
  const up = () => {
    window.removeEventListener("mousemove", move);
    window.removeEventListener("mouseup", up);
    dragging.value = false;
    saveWidth(normalWidth.value);
  };
  window.addEventListener("mousemove", move);
  window.addEventListener("mouseup", up);
  dragging.value = true;
}
function resetWidth() {
  savedWidth.value = WS_DEFAULT;
  saveWidth(WS_DEFAULT);
}
function nudge(event) {
  // 键盘也能调宽度：左右方向键每次 20px。
  const delta = event.key === "ArrowLeft" ? 20 : event.key === "ArrowRight" ? -20 : 0;
  if (!delta) return;
  event.preventDefault();
  savedWidth.value = clampWorkspace(normalWidth.value + delta, mainWidth.value);
  saveWidth(savedWidth.value);
}

function newConversation() {
  router.push({ name: "assistant" });
}
async function removeConversation(item) {
  if (!window.confirm(`删除会话“${item.title}”？删除后不能恢复。`)) return;
  await store.remove(item.id);
  if (routeId.value === item.id) router.push({ name: "assistant" });
}
function expandRail() {
  railCollapsed.value = false;
  if (store.ws.mode === "focus") store.ws.mode = "open";
}
</script>

<template>
  <div class="assistant" :class="{ dragging }">
    <ConversationRail :collapsed="railCollapsed || focus" @collapse="railCollapsed = true" @expand="expandRail"
                      @new="newConversation" @remove="removeConversation" @navigate="overlay && (railCollapsed = true)" />

    <div ref="main" class="main">
      <section class="chat" :style="focus ? { flex: `0 0 ${FOCUS_CHAT}px` } : null" aria-label="对话">
        <header class="chat-head">
          <div class="chat-title">
            <h1>{{ chatTitle }}</h1>
            <div v-if="modelLine" class="mono model">{{ modelLine }}</div>
          </div>
          <button v-if="!shown" class="open-ws" type="button" @click="store.ws.mode = 'open'">
            打开工作区<span class="mono badge">{{ stepBadge }}</span>
          </button>
        </header>
        <ChatThread :load-error="loadError" />
        <ChatComposer />
      </section>

      <div v-if="shown && !overlay && !focus" class="divider" role="separator" aria-orientation="vertical" tabindex="0"
           :aria-valuenow="normalWidth" aria-label="拖动调整工作区宽度，双击恢复默认" title="拖动调整宽度，双击恢复默认"
           @mousedown="startDrag" @dblclick="resetWidth" @keydown="nudge">
        <span class="grip"></span>
      </div>

      <WorkspacePanel v-if="shown" class="ws" :style="overlay ? null : { flex: focus ? '1 1 0' : `0 0 ${normalWidth}px` }"
                      :width="wsWidth" :overlay="overlay" :focus="focus" />
    </div>

    <AssistantSetupDialog />
  </div>
</template>

<style scoped>
.assistant { display: flex; height: 100vh; overflow: hidden; background: var(--bg); color: var(--text); }
.assistant.dragging { user-select: none; cursor: col-resize; }
.main { position: relative; flex: 1; min-width: 0; display: flex; }
.chat { flex: 1 1 0; min-width: 0; display: flex; flex-direction: column; }
.chat-head { flex: none; height: 60px; display: flex; align-items: center; gap: 12px; padding: 0 24px; border-bottom: 1px solid var(--border); }
.chat-title { flex: 1; min-width: 0; }
.chat-title h1 { margin: 0; font-size: 15px; font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.model { margin-top: 2px; font-size: 11.5px; color: var(--text-faint); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.open-ws { flex: none; height: 34px; padding: 0 12px; display: flex; align-items: center; gap: 8px; border: 1px solid var(--border-input);
  border-radius: 8px; background: var(--surface); color: var(--text); font: inherit; font-size: 13px; cursor: pointer; }
.badge { padding: 1px 6px; border-radius: 4px; background: var(--warn-bg); color: var(--warn-ink); font-size: 11px; }
.divider { flex: none; width: 10px; margin: 0 -5px; position: relative; z-index: 6; display: grid; place-items: center; cursor: col-resize; }
.divider:hover, .divider:focus-visible { background: rgba(23, 92, 80, 0.08); }
.grip { width: 4px; height: 36px; border-radius: 2px; background: #cfcec7; }
.dragging .grip { background: var(--accent); }
.ws { min-width: 0; }
@media (max-width: 999px) {
  .chat-head { padding: 0 16px; }
}
</style>
