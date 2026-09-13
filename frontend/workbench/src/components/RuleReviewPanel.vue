<script setup>
import { computed } from "vue";
const props = defineProps({
  criterion: { type: Object, required: true },
  rules: { type: Array, default: () => [] },
  excluded: { type: Set, default: () => new Set() },
  busy: Boolean,
  editable: Boolean,
});
defineEmits(["confirm", "confirm-all", "exclude"]);
const pending = computed(() => props.rules.filter((r) =>
  ["draft", "review"].includes(r.status) && !props.excluded.has(r.id)));
const statuses = { draft: "待确认", review: "待确认", approved: "已确认", rejected: "已驳回" };
const sources = { compiler: "原文解析", manual: "用户录入", legacy_upgrade: "历史原文", llm: "AI 起草" };
const directions = { deduct: "扣分", band: "分档", bonus: "加分", none: "人工复核" };
const severities = { minor: "轻微", moderate: "中等", severe: "严重" };
function displayPoints(rule) {
  if (rule.max_points == null) return "—";
  return `${rule.direction === "deduct" ? "−" : rule.direction === "bonus" ? "+" : ""}${Number(rule.max_points)}`;
}
</script>

<template>
  <section class="card" data-test="rule-review-panel">
    <div class="card-head">
      <div>
        <h2 class="card-title">{{ criterion.code }} {{ criterion.name }} · 评分规则</h2>
        <p class="card-note">逐条核对内容与来源。确认立即保存，不会自动发布。</p>
      </div>
      <span class="chip" :class="pending.length ? 'chip-warn' : 'chip-ok'">
        {{ pending.length ? `${pending.length} 条待确认` : '核对结果' }}
      </span>
    </div>
    <div class="table-wrap">
      <table class="table">
        <thead><tr><th>问题类型 / 条款</th><th>严重程度 / 方式</th><th>分值</th><th>触发条件与判据</th><th>来源</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="rule in rules" :key="rule.id" :class="{ excluded: excluded.has(rule.id) }" :data-rule-code="rule.rule_code">
            <td><strong>{{ rule.name }}</strong><details><summary class="faint">规则编号</summary><p class="faint mono">{{ rule.rule_code }}</p></details>
              <span class="chip" :class="rule.status === 'approved' ? 'chip-ok' : 'chip-warn'">{{ statuses[rule.status] || rule.status }}</span>
            </td>
            <td><span class="chip" :class="rule.origin?.severity === 'severe' ? 'chip-danger' : rule.origin?.severity === 'moderate' ? 'chip-warn' : ''">{{ severities[rule.origin?.severity] || rule.origin?.severity || directions[rule.direction] || rule.direction }}</span></td>
            <td class="mono">{{ displayPoints(rule) }}</td>
            <td class="rule-body">
              <p>{{ rule.rule_text }}</p>
              <p v-if="rule.origin?.trigger">触发条件：{{ rule.origin.trigger }}</p>
              <p v-if="rule.checker_params?.match">触发条件：{{ rule.checker_params.match }}</p>
              <p v-for="level in rule.levels" :key="level.code">{{ level.code }} · {{ level.points }} 分：{{ level.descriptor }}</p>
              <p v-if="rule.mutex_group" class="faint">互斥组：{{ rule.mutex_group }}，同组最多命中一档。</p>
              <p v-if="rule.cap_points != null" class="faint">累计扣分上限 {{ Number(rule.cap_points) }} 分。</p>
              <details v-if="rule.reviewed_at"><summary class="faint">确认记录</summary><p class="faint">确认人 {{ rule.reviewed_by }} · {{ rule.reviewed_at }}</p></details>
            </td>
            <td class="rule-source">
              <span>{{ ['ai_inferred','llm'].includes(rule.origin?.source) ? 'AI 建议' : sources[rule.creation_method] || rule.creation_method }}</span>
              <p v-for="ref in rule.origin?.source_refs || []" :key="String(ref)" class="faint">{{ ref }}</p>
              <details v-if="rule.origin?.generation_fingerprint"><summary>生成记录</summary><p>{{ rule.origin.generation_metadata?.model_name }}</p><p>{{ rule.origin.generation_fingerprint }}</p></details>
              <details v-for="(source, index) in rule.sources" :key="index">
                <summary>{{ source.sheet }} · 第 {{ source.row }} 行 · {{ source.locator }}</summary>
                <p>{{ source.text }}</p>
              </details>
              <span v-if="!rule.sources?.length" class="faint">暂无原文定位</span>
            </td>
            <td>
              <div v-if="['draft', 'review'].includes(rule.status)" class="rule-actions">
                <button class="btn btn-sm" :disabled="busy || !editable" @click="$emit('exclude', rule)">{{ excluded.has(rule.id) ? '撤销排除' : '排除' }}</button>
                <button class="btn btn-sm confirm" :disabled="busy || !editable || excluded.has(rule.id)" @click="$emit('confirm', rule)">{{ busy ? '处理中…' : '确认' }}</button>
              </div>
              <span v-else class="faint">{{ statuses[rule.status] || rule.status }}</span>
            </td>
          </tr>
          <tr v-if="!rules.length"><td colspan="6" class="table-empty">当前评分项没有可核对的规则，请补齐细则并保存重新校验。</td></tr>
        </tbody>
      </table>
    </div>
    <div class="card-foot review-foot">
      <div><p>未确认规则不会进入可执行评分版本。批量仅处理当前评分项，排除项保留待确认。</p>
        <p v-if="!editable" class="faint">当前版本不可编辑或你没有确认权限。</p></div>
      <button class="btn btn-primary" :disabled="busy || !editable || !pending.length" @click="$emit('confirm-all', pending)">
        {{ busy ? '确认中…' : `确认并应用全部（${pending.length}）` }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.table { table-layout: fixed; }
.table th, .table td { padding: 14px 10px; white-space: normal; overflow-wrap: anywhere; }
.table th:nth-child(1) { width: 17%; }
.table th:nth-child(2) { width: 10%; }
.table th:nth-child(3) { width: 7%; }
.table th:nth-child(4) { width: 29%; }
.table th:nth-child(5) { width: 22%; }
.table th:nth-child(6) { width: 15%; }
.rule-body, .rule-source { white-space: normal; overflow-wrap: anywhere; }
.rule-actions, .review-foot { display: flex; gap: 12px; align-items: center; justify-content: space-between; }
.rule-actions { flex-wrap: wrap; gap: 6px; }
.confirm { color: var(--accent, #185e52); }
.excluded { opacity: .6; }
p { margin: 4px 0; }
td { vertical-align: top; }
summary { cursor: pointer; margin-top: 8px; }
@media(max-width: 700px) {
  .review-foot { align-items: stretch; flex-direction: column; }
  .table { min-width: 640px; }
  .table td:last-child, .table th:last-child { position: sticky; right: 0; background: var(--surface); }
}
</style>
