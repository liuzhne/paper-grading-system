<script setup>
/**
 * 评分标准页“核对”步的扣分细则：逐条核对内容与来源后确认或排除。每条用与评分助手工作区同一个 `RuleCard`
 * （v2 界面改造方案 §5），确认、排除、恢复都即时保存（排除落库改造方案）；这里另加规则编号、上限、确认记录等明细。
 */
import { computed } from "vue";

import RuleCard from "@/components/RuleCard.vue";

const props = defineProps({
  criterion: { type: Object, required: true },
  rules: { type: Array, default: () => [] },
  busy: Boolean,
  editable: Boolean,
  deferConfirmation: Boolean,
});
defineEmits(["confirm", "confirm-all", "exclude", "restore"]);
const pending = computed(() => props.rules.filter((r) => ["draft", "review"].includes(r.status)));
const approved = computed(() => props.rules.filter((r) => r.status === "approved").length);
const rejected = computed(() => props.rules.filter((r) => r.status === "rejected").length);
/** 每个评分项至少保留 1 条参与评分的细则（后端同样拒绝）。 */
const canExclude = computed(() => props.rules.length - rejected.value > 1);
</script>

<template>
  <section class="card" data-test="rule-review-panel">
    <div class="card-head">
      <div>
        <h2 class="card-title">{{ criterion.code }} {{ criterion.name }} · 扣分细则</h2>
        <p class="card-note">逐条核对内容与来源。确认立即保存，不会自动发布。</p>
      </div>
      <span class="pill" :class="pending.length ? 'tag-warn' : 'tag-accent'">
        {{ pending.length ? `${pending.length} 条待确认` : '本项已确认' }}
      </span>
    </div>
    <div class="card-pad rules">
      <p v-if="deferConfirmation" class="notice notice-warn" data-test="deferred-confirmation">
        请先应用或丢弃上方 AI 建议；形成最终规则集合后，再统一确认原文规则和 AI 规则。
      </p>
      <p class="summary">{{ rules.length }} 条 · 已确认 {{ approved }}{{ pending.length ? ` · 待确认 ${pending.length}` : "" }}{{ rejected ? ` · 已排除 ${rejected}` : "" }}</p>
      <RuleCard v-for="rule in rules" :key="rule.id" :rule="rule" :editable="editable" :busy="busy || deferConfirmation"
                :can-exclude="canExclude" @confirm="$emit('confirm', $event)" @exclude="$emit('exclude', $event)" @restore="$emit('restore', $event)">
        <template #details>
          <details class="more">
            <summary>规则编号与明细</summary>
            <p class="mono">{{ rule.rule_code }}</p>
            <p v-if="rule.mutex_group">互斥组：{{ rule.mutex_group }}，同组最多命中一档。</p>
            <p v-if="rule.cap_points != null">单条累计扣分上限 {{ Number(rule.cap_points) }} 分。</p>
            <p v-if="rule.origin?.group_cap_points != null">规则组上限 {{ Number(rule.origin.group_cap_points) }} 分；同组最多命中一档。</p>
            <p v-if="rule.reviewed_at">确认人 {{ rule.reviewed_by }} · {{ rule.reviewed_at }}</p>
            <p v-if="rule.origin?.generation_fingerprint">生成记录：{{ rule.origin.generation_metadata?.model_name }} · {{ rule.origin.generation_fingerprint }}</p>
          </details>
        </template>
      </RuleCard>
      <p v-if="!rules.length" class="empty">当前评分项没有可核对的规则，请补齐细则并保存重新校验。</p>
    </div>
    <div class="card-foot review-foot">
      <div><p>{{ deferConfirmation ? '当前规则集合尚未定稿，暂不记录确认。' : '未确认规则不会进入可执行评分版本。批量仅处理当前评分项；已排除的细则不参与评分，可随时恢复。' }}</p>
        <p v-if="!editable" class="faint">当前版本不可编辑或你没有确认权限。</p></div>
      <button class="btn btn-primary" :disabled="busy || !editable || deferConfirmation || !pending.length" @click="$emit('confirm-all', pending)">
        {{ busy ? '确认中…' : `统一确认最终规则（${pending.length}）` }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.rules { display: flex; flex-direction: column; gap: 8px; }
.summary { margin: 0 0 2px; font-size: 12.5px; color: var(--text-muted); }
.more { margin-top: 8px; font-size: 12px; color: var(--text-muted); }
.more summary { cursor: pointer; }
.more p { margin: 4px 0 0; overflow-wrap: anywhere; }
.empty { margin: 0; padding: 14px; border: 1px dashed var(--border); border-radius: var(--radius-md); font-size: 12.5px; color: var(--text-muted); }
.review-foot { display: flex; gap: 12px; align-items: center; justify-content: space-between; }
.review-foot p { margin: 4px 0; }
@media (max-width: 700px) {
  .review-foot { align-items: stretch; flex-direction: column; }
}
</style>
