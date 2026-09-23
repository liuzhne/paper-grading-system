<script setup>
import { computed, ref, watch } from "vue";

import { stepOneGate, suggestionFor, triggerSummary } from "@/lib/parse-coverage.js";

/**
 * 第 1 步统一核对来源冲突、未认领内容与表格结构；第 2 步不挂载本面板。
 *
 * 把“原文里有、但没有被识别成评分项的内容”摆到用户面前：可以逐条指派到评分项、
 * 确认不是规则，也可以批量处理。LLM 只在用户看到原因、范围和所用连接并点击确认后
 * 才调用；不用 LLM 时仍需在发布前处理疑似规则。
 * 面板只发出事件，接口调用由页面完成。
 */
const props = defineProps({
  state: { type: Object, default: null },
  criteria: { type: Array, default: () => [] },
  connections: { type: Array, default: () => [] },
  connectionId: { type: String, default: "" },
  busy: { type: Boolean, default: false },
  editable: { type: Boolean, default: false },
  conflictsOnly: { type: Boolean, default: false },
  /** 结构识别的规模估算（dry run 结果）；有值才允许确认调用。 */
  structureEstimate: { type: Object, default: null },
});
const emit = defineEmits(["resolve", "classify", "suggest-structure", "update:connectionId"]);

const selected = ref(new Set());
const assignTo = ref({});
const LABELS = { rule: "规则", requirement: "要求", context: "说明", noise: "无关" };
const CONFIDENCE = { high: "高", medium: "中", low: "低" };

const gate = computed(() => stepOneGate(props.state));
const unclaimed = computed(() => props.state?.coverage?.unclaimed || []);
const conflicts = computed(() => (props.state?.conflicts || []).filter((item) => !item.resolved));
const documents = computed(() => props.state?.coverage?.documents || []);
const triggers = computed(() => triggerSummary(props.state?.triggers || []));
const locked = computed(() => props.busy || !props.editable);

watch(() => props.state, () => { selected.value = new Set(); });

function toggle(unitId, checked) {
  const next = new Set(selected.value);
  checked ? next.add(unitId) : next.delete(unitId);
  selected.value = next;
}

function markNotRule(unitIds, reason = "用户确认不是评分规则") {
  emit("resolve", { unitIds, action: "not_rule", reason });
}

function assign(unitId) {
  const code = assignTo.value[unitId];
  if (!code) return;
  emit("resolve", { unitIds: [unitId], action: "assign", criterionCode: code, reason: `用户指派到评分项 ${code}` });
}

function percent(ratio) {
  return ratio == null ? "—" : `${Math.round(ratio * 100)}%`;
}

function roleLabel(role) {
  return { rules: "规则文档", template: "模板文档", reference: "参考文档" }[role] || role;
}
</script>

<template>
  <section v-if="state?.has_ledger && (!conflictsOnly || conflicts.length)" class="card card-pad parse-coverage" data-test="parse-coverage">
    <h2 class="card-title">{{ conflictsOnly ? '文档内容差异' : '原文识别情况' }}</h2>
    <p v-if="!conflictsOnly" class="card-note">
      <span v-for="doc in documents" :key="doc.doc_id" class="chip">{{ roleLabel(doc.doc_role) }} · 覆盖率 {{ percent(doc.ratio) }}</span>
    </p>
    <p v-if="conflictsOnly" class="card-note">请核对不同文件中的评分项与分值，确认后继续设置评分规则。</p>
    <p v-else-if="gate.blocked" class="notice notice-danger" role="alert">
      还有 {{ gate.blocking }} 条疑似规则、{{ gate.conflicts }} 个冲突未处理，发布前必须核对。
    </p>
    <p v-else class="notice" role="status">原文中的疑似规则已全部处理。</p>

    <details v-if="!conflictsOnly && (unclaimed.length || triggers.codes.length)" class="ai-assistance" :open="unclaimed.length > 0">
      <summary>可选：AI 结构辅助</summary>
      <label class="field">
      <span class="field-label">用哪个 AI 连接（仅在你确认后调用）</span>
      <select class="select" :value="connectionId" :disabled="locked" @change="emit('update:connectionId', $event.target.value)">
        <option value="">不使用 AI</option>
        <option v-for="item in connections" :key="item.id" :value="item.id">{{ item.name }} · {{ item.model_name }}</option>
      </select>
    </label>

    <div v-if="triggers.codes.length" class="llm-box" data-test="structure-box">
      <h3>表格结构可能没有识别完整</h3>
      <ul><li v-for="(message, index) in triggers.messages" :key="index">{{ message }}</li></ul>
      <p class="faint">AI 只识别表头、列用途与行类型，不改写原文；结果以差异呈现，由你确认后才合入。</p>
      <button v-if="!structureEstimate" class="btn" data-test="structure-estimate" :disabled="locked" @click="emit('suggest-structure', { dryRun: true })">用 AI 识别表格结构…</button>
      <template v-else>
        <p class="notice">将发送约 {{ structureEstimate.chars }} 字符（{{ triggers.unitCount }} 个相关单元），调用 {{ structureEstimate.calls }} 次模型。</p>
        <button class="btn btn-primary" data-test="structure-run" :disabled="locked || !connectionId" @click="emit('suggest-structure', { dryRun: false })">确认调用 AI 识别结构</button>
      </template>
    </div>
    </details>

    <div v-if="conflicts.length" class="conflicts">
      <h3>Word 与 Excel 不一致（{{ conflicts.length }}）</h3>
      <div v-for="item in conflicts" :key="item.anchor_unit_id" class="issue-row" :data-test="`conflict-${item.anchor_unit_id}`">
        <span>{{ item.message }}</span>
        <span class="actions">
          <button class="btn btn-sm" data-test="keep-excel" :disabled="locked" @click="markNotRule([item.anchor_unit_id], '以 Excel 为准')">以 Excel 为准</button>
          <select v-model="assignTo[item.anchor_unit_id]" class="select" :disabled="locked" aria-label="指派到评分项">
            <option value="">指派到评分项…</option>
            <option v-for="c in criteria" :key="c.code" :value="c.code">{{ c.code }} · {{ c.name }}</option>
          </select>
          <button class="btn btn-sm" :disabled="locked || !assignTo[item.anchor_unit_id]" @click="assign(item.anchor_unit_id)">指派</button>
        </span>
      </div>
    </div>

    <div v-if="!conflictsOnly && unclaimed.length" class="unclaimed">
      <div class="issue-row">
        <h3>未被识别的原文（{{ unclaimed.length }}）</h3>
        <span class="actions">
          <button class="btn btn-sm" data-test="classify" :disabled="locked || !connectionId" @click="emit('classify', { unitIds: unclaimed.map((u) => u.unit_id) })">用 AI 判断这些内容（{{ unclaimed.length }} 条）</button>
          <button class="btn btn-sm" data-test="batch-not-rule" :disabled="locked || !selected.size" @click="markNotRule([...selected])">将选中的 {{ selected.size }} 条标为不是规则</button>
        </span>
      </div>
      <p v-if="state.unit_classifications?.stale" class="notice notice-warn">评分项已变化，之前的 AI 判断已过期，请重新判断。</p>
      <div v-for="unit in unclaimed" :key="unit.unit_id" class="unit-row" :data-test="`unit-${unit.unit_id}`">
        <label class="unit-text">
          <input type="checkbox" :checked="selected.has(unit.unit_id)" :disabled="locked" @change="toggle(unit.unit_id, $event.target.checked)" />
          <span>{{ unit.text }}</span>
        </label>
        <span class="faint mono">{{ unit.unit_id }}</span>
        <span v-if="unit.blocking" class="chip chip-danger">疑似规则 · 发布前确认</span>
        <span v-else-if="unit.suspected" class="chip chip-warn">疑似要求</span>
        <p v-if="suggestionFor(state, unit.unit_id)" class="faint suggestion">
          AI 建议：{{ LABELS[suggestionFor(state, unit.unit_id).label] }}<template v-if="suggestionFor(state, unit.unit_id).suggested_criterion"> → {{ suggestionFor(state, unit.unit_id).suggested_criterion }}</template>
          （{{ CONFIDENCE[suggestionFor(state, unit.unit_id).confidence] }}）· {{ suggestionFor(state, unit.unit_id).reason }}
        </p>
        <span class="actions">
          <select v-model="assignTo[unit.unit_id]" class="select" :disabled="locked" aria-label="指派到评分项">
            <option value="">指派到评分项…</option>
            <option v-for="c in criteria" :key="c.code" :value="c.code">{{ c.code }} · {{ c.name }}</option>
          </select>
          <button class="btn btn-sm" data-test="assign" :disabled="locked || !assignTo[unit.unit_id]" @click="assign(unit.unit_id)">指派</button>
          <button class="btn btn-sm" :disabled="locked" @click="markNotRule([unit.unit_id])">不是规则</button>
        </span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.issue-row, .actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.issue-row { justify-content: space-between; margin-top: 10px; }
.unit-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 6px 12px; align-items: center; padding: 10px 0; border-top: 1px solid #eeefeb; }
.unit-row .suggestion, .unit-row .actions { grid-column: 1 / -1; margin: 0; }
.unit-text { display: flex; gap: 8px; align-items: flex-start; }
.llm-box, .conflicts, .unclaimed { margin-top: 18px; }
.ai-assistance { margin-top: 18px; padding: 14px 16px; border: 1px solid #eeefeb; border-radius: 8px; background: #fafaf8; }
.ai-assistance summary { cursor: pointer; font-size: 13px; color: #697466; }
.ai-assistance .field { margin-top: 12px; }
.chip + .chip { margin-left: 6px; }
@media (max-width: 760px) { .unit-row { grid-template-columns: minmax(0, 1fr); } }
</style>
