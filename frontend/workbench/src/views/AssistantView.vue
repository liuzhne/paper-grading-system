<script setup>
/**
 * 评分助手（对话评分助手方案）：左栏会话列表，中间对话，右侧工作区。
 *
 * 工作区就是现有页面，以同源 iframe 嵌入；页面在框架中运行时自己隐藏外壳侧栏。
 * 助手推进到哪一步，工作区就切到对应页面，用户在对话旁边实时看到每一步的数据。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import AssistantCard from "@/components/AssistantCard.vue";
import AssistantSetupDialog from "@/components/AssistantSetupDialog.vue";
import { MAX_FILES, QUICK_ACTIONS } from "@/lib/assistant-flow.js";
import { useAssistantStore } from "@/stores/assistant.js";

const store = useAssistantStore();
const route = useRoute();
const router = useRouter();

const draft = ref("");
const panel = ref("chat"); // 窄屏：chat | workspace
const workspaceOpen = ref(true);
const sidebarOpen = ref(false);
const thread = ref(null);
const attach = ref(null);
const loadError = ref(null);

const routeId = computed(() => (typeof route.params.conversationId === "string" ? route.params.conversationId : null));
const effective = computed(() => store.settings?.effective || null);
const workspaceSrc = computed(() => (store.workspacePath ? router.resolve(store.workspacePath).href : null));

async function openRoute(id) {
  loadError.value = null;
  if (!id) {
    store.clearConversation();
    return;
  }
  if (store.conversation?.id === id) return;
  try {
    await store.open(id);
    scrollToEnd();
  } catch {
    loadError.value = "会话不存在或无权访问。";
    store.clearConversation();
  }
}

onMounted(async () => {
  await Promise.all([store.loadSettings(), store.loadConversations()]);
  await openRoute(routeId.value);
});
onBeforeUnmount(() => store.stopWatchers());

watch(routeId, (id) => openRoute(id));
// 第一句话时才建会话；建好后把地址换成这个会话，刷新能回来。
watch(() => store.conversation?.id, (id) => {
  if (id && id !== routeId.value) router.replace({ name: "assistant", params: { conversationId: id } });
});
watch(() => store.messages.length, () => scrollToEnd());
watch(() => store.workspacePath, (path) => { if (path) workspaceOpen.value = true; });

function scrollToEnd() {
  nextTick(() => {
    if (thread.value) thread.value.scrollTop = thread.value.scrollHeight;
  });
}

async function submit() {
  const text = draft.value.trim();
  if (!text) return;
  draft.value = "";
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

function newConversation() {
  sidebarOpen.value = false;
  router.push({ name: "assistant" });
}

async function removeConversation(item) {
  if (!window.confirm(`删除会话“${item.title}”？删除后不能恢复。`)) return;
  await store.remove(item.id);
  if (routeId.value === item.id) router.push({ name: "assistant" });
}

function timeText(value) {
  // 后端返回不带时区的 UTC 时间；按 UTC 解析再显示成本地时间。
  const raw = String(value || "");
  const date = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(raw) ? raw : `${raw}Z`);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { hour12: false });
}
</script>

<template>
  <div class="assistant" :class="[`panel-${panel}`, { 'workspace-closed': !workspaceOpen }]">
    <!-- 左：会话列表 -->
    <aside class="history" :class="{ open: sidebarOpen }">
      <div class="brand">
        <span class="brand-name">有据智评</span>
        <span class="brand-sub">评分助手</span>
      </div>
      <button class="btn btn-primary new" type="button" @click="newConversation">＋ 新对话</button>
      <nav class="conversations" aria-label="历史会话">
        <div v-for="item in store.conversations" :key="item.id" class="conversation"
             :class="{ active: item.id === store.conversation?.id }">
          <RouterLink class="conversation-link" :to="{ name: 'assistant', params: { conversationId: item.id } }"
                      @click="sidebarOpen = false">{{ item.title }}</RouterLink>
          <button class="conversation-delete" type="button" :aria-label="`删除会话 ${item.title}`"
                  @click="removeConversation(item)">×</button>
        </div>
        <p v-if="!store.conversations.length" class="empty">还没有会话。</p>
      </nav>
      <div class="history-foot">
        <button class="link" type="button" @click="store.setupOpen = true">助手模型设置</button>
        <RouterLink class="link" :to="{ name: 'dashboard' }">← 返回工作台</RouterLink>
      </div>
    </aside>

    <!-- 中：对话 -->
    <section class="chat" aria-label="对话">
      <header class="chat-head">
        <button class="btn btn-sm only-narrow" type="button" @click="sidebarOpen = !sidebarOpen">会话</button>
        <div class="chat-title">
          <h1>{{ store.conversation?.title || "评分助手" }}</h1>
          <span v-if="effective" class="faint model">助手模型：{{ effective.label }}</span>
        </div>
        <button class="btn btn-sm only-narrow" type="button" @click="panel = panel === 'chat' ? 'workspace' : 'chat'">
          {{ panel === "chat" ? "工作区" : "对话" }}
        </button>
        <button class="btn btn-sm only-wide" type="button" @click="workspaceOpen = !workspaceOpen">
          {{ workspaceOpen ? "收起工作区" : "展开工作区" }}
        </button>
      </header>

      <div ref="thread" class="thread">
        <p v-if="loadError" class="notice notice-danger">{{ loadError }}</p>

        <div v-if="!store.messages.length" class="welcome">
          <h2>今天要评哪些论文？</h2>
          <p class="faint">上传待评分的文件就能开始，默认用最新发布的评分标准。评分过程会显示在右侧的页面上；只有需要你决定的地方我才会问。</p>
          <div class="quick">
            <button v-for="action in QUICK_ACTIONS" :key="action.intent" class="btn btn-sm" type="button"
                    :disabled="store.sending" @click="store.send(action.text)">{{ action.label }}</button>
          </div>
        </div>

        <article v-for="message in store.messages" :key="message.id" class="message" :class="`from-${message.role}`">
          <div class="bubble">
            <p v-if="message.text" class="text">{{ message.text }}</p>
            <AssistantCard v-for="card in message.cards" :key="card.id" :message="message" :card="card" />
          </div>
          <time class="faint stamp" :datetime="message.created_at">{{ timeText(message.created_at) }}</time>
        </article>
        <p v-if="store.sending" class="faint typing">正在处理…</p>
      </div>

      <footer class="composer">
        <p v-if="effective?.slow" class="notice notice-warn slim">{{ effective.notice }}</p>
        <p v-if="store.error" class="notice notice-danger slim" role="alert">{{ store.error }}</p>
        <div class="composer-row">
          <label class="attach" :title="`上传待评分文件（最多 ${MAX_FILES} 份）`">
            <input ref="attach" type="file" multiple accept=".docx,.pdf" :disabled="store.sending" @change="onAttach" />
            <span aria-hidden="true">📎</span><span class="sr-only">上传待评分文件</span>
          </label>
          <textarea v-model="draft" class="input" rows="1" maxlength="500"
                    placeholder="例如：开始评分 / 第 3 篇为什么扣分 / 哪些需要复核"
                    aria-label="输入消息" @keydown="onKeydown"></textarea>
          <button class="btn btn-primary" type="button" :disabled="store.sending || !draft.trim()" @click="submit">发送</button>
        </div>
      </footer>
    </section>

    <!-- 右：工作区（现有页面，实时） -->
    <section v-show="workspaceOpen" class="workspace" aria-label="工作区">
      <button class="btn btn-sm only-narrow back-to-chat" type="button" @click="panel = 'chat'">← 回到对话</button>
      <iframe v-if="workspaceSrc" :key="store.workspaceKey" class="frame" :src="workspaceSrc" title="工作区"></iframe>
      <div v-else class="workspace-empty">
        <p>评分的每一步会显示在这里：评分进度、评分结果、评分标准核对。</p>
      </div>
    </section>

    <AssistantSetupDialog />
  </div>
</template>

<style scoped>
.assistant {
  display: grid;
  grid-template-columns: 240px minmax(380px, 1fr) minmax(420px, 1.25fr);
  height: 100vh;
  background: var(--bg);
}
.assistant.workspace-closed { grid-template-columns: 240px 1fr; }

.history { display: flex; flex-direction: column; gap: 12px; padding: 18px 14px; background: var(--sidebar-bg); color: var(--sidebar-text); min-height: 0; }
.brand-name { display: block; font-weight: 600; }
.brand-sub { color: var(--sidebar-text-muted); font-size: 12px; }
.new { width: 100%; }
.conversations { flex: 1; overflow-y: auto; display: grid; align-content: start; gap: 2px; }
.conversation { display: flex; align-items: center; border-radius: var(--radius-sm); }
.conversation.active, .conversation:hover { background: var(--sidebar-item-active-bg); }
.conversation-link { flex: 1; min-width: 0; padding: 8px 10px; color: var(--sidebar-text); text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.conversation-delete { visibility: hidden; border: 0; background: none; color: var(--sidebar-text-muted); cursor: pointer; padding: 4px 8px; }
.conversation:hover .conversation-delete, .conversation.active .conversation-delete { visibility: visible; }
.empty { color: var(--sidebar-text-faint); font-size: 13px; }
.history-foot { display: grid; gap: 8px; }
.link { border: 0; background: none; color: var(--sidebar-text-muted); text-align: left; font: inherit; font-size: 13px; cursor: pointer; text-decoration: none; padding: 0; }
.link:hover { color: var(--sidebar-text); }

.chat { display: flex; flex-direction: column; min-width: 0; min-height: 0; border-right: 1px solid var(--border); background: var(--surface-muted); }
.chat-head { display: flex; align-items: center; gap: 10px; padding: 14px 18px; border-bottom: 1px solid var(--border); background: var(--surface); }
.chat-title { flex: 1; min-width: 0; }
.chat-title h1 { margin: 0; font-size: 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.model { font-size: 12px; }
.thread { flex: 1; overflow-y: auto; padding: 18px; display: flex; flex-direction: column; gap: 14px; }
.welcome { margin: auto 0; padding: 24px 4px; }
.welcome h2 { margin: 0 0 8px; font-size: 20px; }
.quick { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
.message { display: flex; flex-direction: column; max-width: 92%; }
.from-user { align-self: flex-end; align-items: flex-end; }
.bubble { padding: 10px 14px; border-radius: var(--radius-lg); background: var(--surface); border: 1px solid var(--border-light); }
.from-user .bubble { background: var(--accent); color: #fff; border-color: var(--accent); }
.text { margin: 0; white-space: pre-wrap; line-height: 1.65; }
.stamp { margin-top: 4px; font-size: 11px; }
.typing { font-size: 13px; }
.composer { padding: 12px 18px 16px; border-top: 1px solid var(--border); background: var(--surface); }
.composer-row { display: flex; gap: 8px; align-items: flex-end; }
.composer textarea { flex: 1; resize: none; min-height: 40px; max-height: 160px; }
.attach { display: inline-grid; place-items: center; width: 40px; height: 40px; border: 1px solid var(--border-input); border-radius: var(--radius); cursor: pointer; flex: none; }
.attach input { position: absolute; width: 1px; height: 1px; opacity: 0; }
.slim { margin: 0 0 8px; padding: 6px 10px; font-size: 13px; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }

.workspace { position: relative; min-width: 0; min-height: 0; background: var(--surface); }
.back-to-chat { position: absolute; top: 10px; right: 12px; z-index: 2; }
.frame { width: 100%; height: 100%; border: 0; display: block; }
.workspace-empty { display: grid; place-items: center; height: 100%; padding: 24px; color: var(--text-muted); text-align: center; }

.only-narrow { display: none; }

@media (max-width: 1100px) {
  .assistant, .assistant.workspace-closed { grid-template-columns: 1fr; }
  .history { position: fixed; inset: 0 auto 0 0; width: 260px; z-index: 30; transform: translateX(-100%); transition: transform 0.2s; }
  .history.open { transform: none; }
  .only-narrow { display: inline-flex; }
  .only-wide { display: none; }
  .panel-chat .workspace { display: none !important; }
  .panel-workspace .chat { display: none; }
  .panel-workspace .workspace { display: block !important; height: 100vh; }
}
</style>
