<script setup>
/**
 * 评分模型：与“新建评分任务”页同一规则——启用的个人连接优先，没有时用平台模型。在这里只看，不在这里改。
 */
import { computed } from "vue";
import { useRouter } from "vue-router";

import WsLayout from "./WsLayout.vue";

const props = defineProps({
  context: { type: Object, required: true },
  head: { type: Object, required: true },
});

const router = useRouter();
const step = computed(() => (props.context.plan?.steps || []).find((item) => item.key === "model") || null);
const ready = computed(() => step.value?.status === "done");
const accountHref = computed(() => router.resolve("/account").href);
</script>

<template>
  <WsLayout v-bind="head">
    <div class="body">
      <div class="model" :class="{ missing: !ready }">
        <span class="badge mono">AI</span>
        <div class="row-main">
          <div class="name">{{ ready ? step.detail : "还没有可用的评分模型" }}</div>
          <p class="note">
            {{ ready ? "启用的个人模型连接优先，没有时用平台模型。评分时只使用已发布的评分标准和待评分文件的原文。"
              : (step?.detail || "请先在账户与连接里配置自己的模型连接，配好后再说“开始评分”。") }}
          </p>
        </div>
        <a class="change" :href="accountHref" target="_blank" rel="noopener">{{ ready ? "更换" : "去配置" }}</a>
      </div>
    </div>
  </WsLayout>
</template>

<style scoped>
.body { padding: 14px 24px 0; }
.model { display: flex; gap: 14px; align-items: flex-start; padding: 14px 16px; border: 1px solid var(--border-light); border-radius: var(--radius-md); }
.model.missing { border-color: var(--warn-border); background: var(--warn-surface); }
.badge { flex: none; width: 36px; height: 36px; border-radius: 8px; display: grid; place-items: center;
  background: var(--chip-bg); color: var(--text-secondary); font-size: 11px; font-weight: 600; }
.name { font-size: 14px; font-weight: 600; }
.note { margin: 5px 0 0; font-size: 12.5px; line-height: 1.7; color: var(--neutral-ink); }
.change { flex: none; font-size: 12.5px; }
</style>
