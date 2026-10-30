<script setup>
/**
 * 会话栏（评分助手 v2 设计稿）：深色侧栏，可收成 56px 窄轨。每条会话两行：标题 + “步骤 2/8 · 今天 10:21”。
 */
import { RouterLink } from "vue-router";

import { conversationMeta } from "@/lib/assistant-workspace.js";
import { useAssistantStore } from "@/stores/assistant.js";

defineProps({ collapsed: Boolean });
const emit = defineEmits(["collapse", "expand", "new", "remove", "navigate"]);
const store = useAssistantStore();
</script>

<template>
  <aside v-if="!collapsed" class="rail" aria-label="会话">
    <div class="brand">
      <span class="mark" aria-hidden="true">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 8.6V4h4.6" /><path d="M20 15.4V20h-4.6" /><path d="M8.2 12.4l3 3 5.2-5.8" />
        </svg>
      </span>
      <div class="brand-copy"><div class="brand-name">有据智评</div><div class="brand-sub">评分助手</div></div>
      <button class="icon" type="button" title="收起侧栏" aria-label="收起侧栏" @click="emit('collapse')">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 6l-6 6 6 6" /></svg>
      </button>
    </div>
    <button class="new" type="button" @click="emit('new')">＋ 新对话</button>
    <div class="label">最近</div>
    <nav class="list" aria-label="历史会话">
      <div v-for="item in store.conversations" :key="item.id" class="item" :class="{ active: item.id === store.conversation?.id }">
        <RouterLink class="item-link" :to="{ name: 'assistant', params: { conversationId: item.id } }" @click="emit('navigate')">
          <span class="item-title">{{ item.title }}</span>
          <span class="item-meta">{{ conversationMeta(item) }}</span>
        </RouterLink>
        <button class="item-delete" type="button" :aria-label="`删除会话 ${item.title}`" @click="emit('remove', item)">×</button>
      </div>
      <p v-if="!store.conversations.length" class="empty">还没有会话。</p>
    </nav>
    <div class="foot">
      <button class="foot-link" type="button" @click="store.setupOpen = true">助手模型设置</button>
      <RouterLink class="foot-link" :to="{ name: 'dashboard' }">返回工作台</RouterLink>
    </div>
  </aside>
  <aside v-else class="rail narrow" aria-label="会话">
    <button class="mark" type="button" title="展开侧栏" aria-label="展开侧栏" @click="emit('expand')">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M4 8.6V4h4.6" /><path d="M20 15.4V20h-4.6" /><path d="M8.2 12.4l3 3 5.2-5.8" />
      </svg>
    </button>
    <button class="plus" type="button" title="新对话" aria-label="新对话" @click="emit('new')">＋</button>
  </aside>
</template>

<style scoped>
.rail { width: 228px; flex: none; display: flex; flex-direction: column; min-height: 0; padding: 16px 12px 14px;
  background: var(--sidebar-bg); color: var(--sidebar-text); }
.rail.narrow { width: 56px; align-items: center; gap: 10px; padding: 16px 0; }
.brand { display: flex; align-items: center; gap: 10px; padding: 2px 4px 18px 6px; }
.mark { width: 30px; height: 30px; flex: none; display: grid; place-items: center; border: 0; border-radius: 8px; background: var(--accent); cursor: pointer; }
.brand-copy { flex: 1; min-width: 0; }
.brand-name { font-size: 14px; font-weight: 600; letter-spacing: 0.12em; color: #fff; }
.brand-sub { margin-top: 2px; font-size: 11px; color: var(--sidebar-text-faint); }
.icon { width: 26px; height: 26px; display: grid; place-items: center; border: 0; border-radius: 6px; background: transparent;
  color: var(--sidebar-text-faint); cursor: pointer; }
.icon:hover { background: rgba(255, 255, 255, 0.08); color: #fff; }
.new { height: 38px; border: 0; border-radius: 8px; background: var(--accent); color: #fff; font-size: 13.5px; font-weight: 600; cursor: pointer; }
.new:hover { filter: brightness(1.12); }
.plus { width: 32px; height: 32px; border: 0; border-radius: 8px; background: transparent; color: #c3c9c5; font-size: 17px; cursor: pointer; }
.plus:hover { background: rgba(255, 255, 255, 0.08); }
.label { padding: 20px 8px 8px; font-size: 10.5px; letter-spacing: 0.16em; color: var(--sidebar-label); }
.list { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; }
.item { position: relative; border-radius: 7px; }
.item:hover { background: var(--sidebar-item-hover-bg); }
.item.active { background: var(--sidebar-item-active-bg); }
.item-link { display: block; padding: 9px 26px 9px 10px; text-decoration: none; }
.item-title { display: block; font-size: 13.5px; color: #c3c9c5; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.item.active .item-title { color: #fff; }
.item-meta { display: block; margin-top: 3px; font-size: 11.5px; color: var(--sidebar-text-faint); }
.item-delete { position: absolute; top: 8px; right: 4px; visibility: hidden; padding: 2px 6px; border: 0; background: none;
  color: var(--sidebar-text-faint); cursor: pointer; }
.item:hover .item-delete, .item-delete:focus-visible { visibility: visible; }
.empty { padding: 0 10px; font-size: 13px; color: var(--sidebar-text-faint); }
.foot { margin-top: auto; display: flex; flex-direction: column; gap: 2px; padding-top: 12px; border-top: 1px solid var(--sidebar-divider); font-size: 13px; }
.foot-link { padding: 8px 10px; border: 0; border-radius: 7px; background: none; color: var(--sidebar-text-muted); text-align: left;
  font: inherit; font-size: 13px; cursor: pointer; text-decoration: none; }
.foot-link:hover { background: var(--sidebar-item-hover-bg); color: var(--sidebar-text); }
</style>
