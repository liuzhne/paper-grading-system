<script setup>
/**
 * 工作区里每个视图的骨架：可滚动的正文（顶部一行小字 + 标题 + 说明）+ 固定在底部的操作条。
 * 任务标签的步骤视图与结果标签共用（评分助手 v2 设计稿）。
 */
defineProps({
  /** 标题上方的一行：步骤视图是“步骤 2 / 8 · 待你处理”，结果标签不用。 */
  eyebrow: { type: String, default: "" },
  eyebrowTone: { type: String, default: "faint" },
  /** 结果标签的类型色块（进展、解释、结果、待办）与时间说明。 */
  tag: { type: Object, default: null },
  meta: { type: String, default: "" },
  title: { type: String, default: "" },
  desc: { type: String, default: "" },
  /** 对应的完整页面（新标签页打开：选好的待评分文件只在本页内存里，跳走就丢了）。 */
  link: { type: Object, default: null },
});
defineEmits(["refresh"]);
</script>

<template>
  <div class="ws-view">
    <div class="ws-scroll" data-ws-scroll>
      <header class="ws-head">
        <div class="ws-eyebrow">
          <span v-if="tag" class="kind" :class="`tone-${tag.tone}`">{{ tag.tag }}</span>
          <span v-if="eyebrow" class="mono eyebrow" :class="`tone-${eyebrowTone}`">{{ eyebrow }}</span>
          <span v-if="meta" class="meta">{{ meta }}</span>
          <slot name="head-actions" />
          <a v-if="link" class="open-page" :href="link.href" target="_blank" rel="noopener">{{ link.label }}</a>
        </div>
        <h2 v-if="title" class="ws-title">{{ title }}</h2>
        <p v-if="desc" class="ws-desc">{{ desc }}</p>
      </header>
      <slot />
    </div>
    <footer v-if="$slots.foot" class="ws-foot">
      <slot name="foot" />
    </footer>
  </div>
</template>

<style scoped>
.ws-view { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.ws-scroll { flex: 1; min-height: 0; overflow-y: auto; padding-bottom: 28px; }
.ws-head { padding: 20px 24px 6px; }
.ws-eyebrow { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-height: 18px; }
.eyebrow { font-size: 11.5px; }
.meta { font-size: 12px; color: var(--text-faint); }
.kind { font-size: 11.5px; padding: 1px 7px; border-radius: 4px; }
.open-page { margin-left: auto; font-size: 12.5px; }
.ws-title { margin: 6px 0 0; font-size: 20px; font-weight: 700; }
.ws-desc { margin: 6px 0 0; font-size: 12.5px; color: var(--text-muted); line-height: 1.7; text-wrap: pretty; }
.ws-foot {
  flex: none; display: flex; align-items: center; flex-wrap: wrap; gap: 10px 12px;
  padding: 12px 20px; border-top: 1px solid var(--border-light); background: var(--surface);
}
.tone-faint { color: var(--text-faint); }
.tone-accent { color: var(--accent); }
.tone-warn { color: var(--warn-ink); }
.tone-danger { color: var(--danger); }
.kind.tone-accent { background: var(--accent-soft); color: var(--accent); }
.kind.tone-neutral { background: var(--chip-bg); color: var(--text-secondary); }
.kind.tone-info { background: var(--info-bg); color: var(--info-ink); }
.kind.tone-warn { background: var(--warn-bg); color: var(--warn-ink); }
</style>
