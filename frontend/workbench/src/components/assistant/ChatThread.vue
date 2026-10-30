<script setup>
/**
 * 对话（评分助手 v2 设计稿）：老师的消息是深绿气泡；助手的消息带头像、名称与时间；“你发布了《…》”这类
 * 事件显示为分隔线。流程卡片与结果卡片显示成“在右侧查看”入口，对话本身的选择（选任务、重试、放弃上一步）仍在这里点。
 */
import { computed, nextTick, ref, watch } from "vue";

import AssistantCard from "@/components/AssistantCard.vue";
import { QUICK_ACTIONS } from "@/lib/assistant-flow.js";
import {
  STEPS, TAB_KINDS, artifactsFrom, clockText, entryStep, flowContext, viewStepKey,
} from "@/lib/assistant-workspace.js";
import { useAssistantStore } from "@/stores/assistant.js";

import EntryCard from "./EntryCard.vue";

defineProps({ loadError: { type: String, default: null } });

const store = useAssistantStore();
const scroller = ref(null);

/** 欢迎页的快捷按钮（设计稿 3 个，加上用自己的评分规则）。 */
const STARTS = ["start_grading", "import_rubric", "query_progress", "query_review"];
const starts = computed(() => QUICK_ACTIONS.filter((action) => STARTS.includes(action.intent)));

const plan = computed(() => flowContext(store.messages).plan);
const artifacts = computed(() => new Map(artifactsFrom(store.messages).map((item) => [item.id, item])));
const viewKey = computed(() => viewStepKey(plan.value, store.ws.step));
const shown = computed(() => store.ws.mode !== "closed");

function kindOf(card) {
  if (card.type === "plan") return "hidden";
  const artifact = artifacts.value.get(card.id);
  if (artifact) return "artifact";
  if (entryStep(card)) return "entry";
  return "inline";
}
function artifactOf(card) {
  const item = artifacts.value.get(card.id);
  const kind = TAB_KINDS[item.kind];
  return { tag: kind.tag, tone: kind.tone, title: item.title };
}
function entryActive(card) {
  if (!shown.value || store.ws.tab !== "task") return false;
  return STEPS[entryStep(card)]?.view === STEPS[viewKey.value]?.view;
}
const isEvent = (message) => message.intent === "event";

function scrollToEnd() {
  nextTick(() => {
    if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
  });
}
watch(() => [store.conversation?.id, store.messages.length, store.sending], scrollToEnd);
</script>

<template>
  <div ref="scroller" class="thread" data-test="thread">
    <div class="column">
      <p v-if="loadError" class="notice notice-danger">{{ loadError }}</p>

      <div v-if="!store.messages.length" class="welcome">
        <h2>有什么要处理的？</h2>
        <p>可以开始一次评分、查询任务进展，或问某一份为什么扣分。需要你决定的地方我才会问。</p>
        <div class="starts">
          <button v-for="action in starts" :key="action.intent" class="start" type="button" :disabled="store.sending"
                  @click="store.send(action.text, action.intent)">{{ action.label }}</button>
        </div>
      </div>

      <template v-for="message in store.messages" :key="message.id">
        <div v-if="isEvent(message)" class="event" role="note">
          <span class="line"></span><span>{{ message.text }} · {{ clockText(message.created_at) }}</span><span class="line"></span>
        </div>
        <article v-else-if="message.role === 'user'" class="message from-user">
          <time class="stamp" :datetime="message.created_at">{{ clockText(message.created_at) }}</time>
          <p class="bubble">{{ message.text }}</p>
        </article>
        <article v-else class="message from-assistant">
          <span class="avatar" aria-hidden="true">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
              <path d="M4 8.6V4h4.6" /><path d="M20 15.4V20h-4.6" /><path d="M8.2 12.4l3 3 5.2-5.8" />
            </svg>
          </span>
          <div class="body">
            <div class="who"><strong>有据智评</strong><time :datetime="message.created_at">{{ clockText(message.created_at) }}</time></div>
            <p v-if="message.text" class="text">{{ message.text }}</p>
            <template v-for="card in message.cards" :key="card.id">
              <EntryCard v-if="kindOf(card) === 'entry'" :card="card" :plan="plan" :active="entryActive(card)" />
              <EntryCard v-else-if="kindOf(card) === 'artifact'" :card="card" :artifact="artifactOf(card)"
                         :active="shown && store.ws.tab === card.id" />
              <AssistantCard v-else-if="kindOf(card) === 'inline'" :message="message" :card="card" />
            </template>
          </div>
        </article>
      </template>

      <div v-if="store.sending" class="typing">
        <span class="avatar" aria-hidden="true">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 8.6V4h4.6" /><path d="M20 15.4V20h-4.6" /><path d="M8.2 12.4l3 3 5.2-5.8" />
          </svg>
        </span>
        <span>正在整理…</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.thread { flex: 1; min-height: 0; overflow-y: auto; }
.column { max-width: 720px; margin: 0 auto; padding: 28px 24px 20px; display: flex; flex-direction: column; gap: 22px; }
.welcome { padding-top: 14vh; text-align: center; }
.welcome h2 { margin: 0; font-size: 20px; font-weight: 700; }
.welcome p { margin: 8px 0 0; font-size: 13px; color: var(--text-muted); }
.starts { display: flex; justify-content: center; flex-wrap: wrap; gap: 8px; margin-top: 20px; }
.start { height: 36px; padding: 0 14px; border: 1px solid var(--border-input); border-radius: 18px; background: var(--surface);
  color: var(--text); font: inherit; font-size: 13px; cursor: pointer; }
.start:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.event { display: flex; align-items: center; gap: 12px; font-size: 12px; color: var(--text-muted); }
.event .line { flex: 1; height: 1px; background: var(--border); }
.message.from-user { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; }
.stamp, .who time { font-size: 11.5px; color: var(--text-faint); }
.bubble { margin: 0; max-width: min(80%, 520px); padding: 10px 14px; border-radius: 14px 14px 4px 14px; background: var(--accent);
  color: #fff; font-size: 14px; line-height: 1.7; white-space: pre-wrap; text-wrap: pretty; }
.from-assistant { display: flex; gap: 12px; align-items: flex-start; }
.avatar { flex: none; width: 28px; height: 28px; border-radius: 8px; background: var(--accent); display: grid; place-items: center; margin-top: 1px; }
.body { flex: 1; min-width: 0; }
.who { display: flex; align-items: baseline; gap: 8px; margin-bottom: 4px; }
.who strong { font-size: 12.5px; font-weight: 600; color: var(--text-secondary); }
.text { margin: 0; font-size: 14px; line-height: 1.85; white-space: pre-wrap; text-wrap: pretty; }
.typing { display: flex; align-items: center; gap: 12px; font-size: 13px; color: var(--text-muted); }
</style>
