<script setup>
/**
 * Core 评分项的逐条规则判定（docs/评分证据与理由改造方案.md G2、G3、G5）。
 *
 * 每条规则一行：判定结果、规则名、分值影响；模型写的一句理由（开关打开后才有）；
 * 证据按钮——有核对过的原文引文时直接显示引文（论文原文，语言与论文一致），
 * 只有段落上下文时显示章节并标“上下文”，不冒充引文。
 */

/**
 * @typedef {{ quote?: string | null, section_title?: string | null, location_status: string,
 *   unlocatable_reason?: string | null, anchor_id?: string | null }} EvidenceView
 * @typedef {{ rule_code: string, rule_name: string, outcome: string, label: string,
 *   effect?: string | null, rationale?: string | null, evidence: EvidenceView[],
 *   basis_evidence: EvidenceView[], basis_scope?: string | null,
 *   basis_shared_with?: string | null }} RuleRow
 */

defineProps({
  /** @type {import("vue").PropType<{ rules: RuleRow[] }>} */
  view: { type: Object, required: true },
});

const emit = defineEmits(["locate"]);

const TONE = {
  deducted: "chip-danger",
  invalid: "chip-danger",
  insufficient: "chip-warn",
  flagged: "chip-warn",
  triggered: "chip-warn",
  not_triggered: "chip-ok",
};

const STATUS_TEXT = {
  verified: "原文引文",
  quote_not_found: "引文与当前文本不一致",
  unit_context: "上下文",
  anchor_missing: "冻结正文中找不到",
  no_anchor: "无锚点",
  check_fact: "检查事实",
};

/** @param {EvidenceView} view */
function locatable(view) {
  return ["verified", "quote_not_found", "unit_context"].includes(view.location_status);
}

/** @param {EvidenceView} view */
function evidenceTitle(view) {
  return view.unlocatable_reason || [view.section_title, STATUS_TEXT[view.location_status]].filter(Boolean).join(" · ");
}

/** @param {RuleRow} row */
function evidenceOf(row) {
  return [
    ...row.evidence.map((view) => ({ view, basis: false })),
    ...(row.basis_evidence || []).map((view) => ({ view, basis: true })),
  ];
}
</script>

<template>
  <ul class="rules" data-test="rule-judgements">
    <li v-for="row in view.rules" :key="row.rule_code" class="rule" :data-outcome="row.outcome">
      <div class="rule-head">
        <span class="chip rule-chip" :class="TONE[row.outcome]">{{ row.label }}</span>
        <span class="rule-name">{{ row.rule_name }}</span>
        <span v-if="row.effect" class="mono rule-effect">{{ row.effect }}</span>
      </div>
      <p v-if="row.rationale" class="rule-rationale" data-test="rule-rationale">
        <span v-if="row.basis_scope === 'group'" class="faint">同组判定：</span>{{ row.rationale }}
      </p>
      <p v-else-if="row.basis_shared_with" class="rule-rationale faint" data-test="rule-basis-shared">
        依据见同组「{{ row.basis_shared_with }}」
      </p>
      <div v-if="evidenceOf(row).length" class="rule-evidence">
        <button
          v-for="(entry, index) in evidenceOf(row)"
          :key="index"
          class="ev"
          :class="{ dim: !locatable(entry.view), quote: entry.view.quote }"
          type="button"
          :disabled="!locatable(entry.view)"
          :title="evidenceTitle(entry.view)"
          :data-test="entry.view.quote ? 'rule-quote' : 'rule-context'"
          @click="emit('locate', entry.view)"
        >
          <template v-if="entry.view.quote">
            <span v-if="entry.basis" class="faint">依据 </span>“{{ entry.view.quote }}”
          </template>
          <template v-else>
            {{ [entry.view.section_title, STATUS_TEXT[entry.view.location_status]].filter(Boolean).join(" · ") }}
          </template>
        </button>
      </div>
    </li>
  </ul>
</template>

<style scoped>
.rules {
  list-style: none;
  margin: 0 0 8px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.rule-head {
  display: flex;
  align-items: baseline;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 12.5px;
  line-height: 1.5;
}

.rule-chip {
  padding: 1px 7px;
  font-size: 11.5px;
}

.rule-name {
  flex: 1 1 auto;
  min-width: 0;
  overflow-wrap: anywhere;
}

.rule-effect {
  font-size: 12px;
  color: var(--danger-ink);
}

.rule-rationale {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.rule-evidence {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 4px;
}

.ev {
  font-size: 12px;
  text-align: left;
  color: var(--text-secondary);
  border: 1px solid var(--border-input);
  border-radius: 5px;
  padding: 2px 8px;
  background: var(--surface);
  cursor: pointer;
  align-self: flex-start;
  max-width: 100%;
}

.ev.quote {
  color: var(--text);
  line-height: 1.6;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.ev:hover:not(:disabled) {
  border-color: var(--border-input-hover);
}

.ev.dim {
  color: var(--text-faint);
  cursor: not-allowed;
}
</style>
