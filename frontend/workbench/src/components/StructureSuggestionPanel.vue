<script setup>
import { computed, ref, watch } from "vue";

import { diffGroups, mergeSelection } from "@/lib/parse-coverage.js";

/**
 * AI 结构识别的差异视图（解析重构方案 §8）。
 *
 * 合入 = 按确认后的结构重新解析并生成新草稿：新增、补全默认合入（可排除）；
 * 修改须逐条勾选确认，未确认的保留当前值；冲突必须排除；会移除已有评分项的结构
 * 一律不能合入（已有评分项被旧版本规则引用，只能增、改，不能删）。
 */
const props = defineProps({
  suggestion: { type: Object, default: null },
  busy: { type: Boolean, default: false },
  editable: { type: Boolean, default: false },
});
const emit = defineEmits(["merge", "undo"]);

const confirm = ref(new Set());
const exclude = ref(new Set());
watch(() => props.suggestion?.fingerprint, () => { confirm.value = new Set(); exclude.value = new Set(); });

const groups = computed(() => diffGroups(props.suggestion?.items || []));
const plan = computed(() => mergeSelection(props.suggestion?.items || [], confirm.value, exclude.value));
const pending = computed(() => props.suggestion?.status === "pending");
const canMerge = computed(() => pending.value && !props.suggestion?.stale && !plan.value.blocked.length &&
  props.editable && !props.busy && (props.suggestion?.items || []).length > 0);

const FIELD = { name: "评分项名称", max_score: "满分", description: "评分说明", deduction_rules: "扣分规则" };

/** 模板中 ref 会被自动解包，所以按名称选择集合，而不是把集合本身传进来。 */
function toggle(name, id, checked) {
  const target = name === "confirm" ? confirm : exclude;
  const next = new Set(target.value);
  checked ? next.add(id) : next.delete(id);
  target.value = next;
}

function show(value) {
  if (value == null || value === "") return "（空）";
  return Array.isArray(value) ? value.join("；") : String(value);
}
</script>

<template>
  <section v-if="suggestion" class="card card-pad" data-test="structure-suggestion">
    <h2 class="card-title">AI 结构识别结果 <span class="chip">LLM 识别</span></h2>
    <p v-if="suggestion.stale" class="notice notice-warn">评分项或文件已变化，这份建议已过期，请重新识别。</p>
    <template v-if="pending">
      <p v-if="!suggestion.items.length" class="notice">按识别出的结构重新解析，与当前评分项没有差异。</p>
      <div v-for="item in groups.oneClick" :key="item.id" class="diff-row" :data-test="`item-${item.id}`">
        <label><input type="checkbox" :checked="!exclude.has(item.id)" :disabled="busy" @change="toggle('exclude', item.id, !$event.target.checked)" />
          <template v-if="item.kind === 'new'">新增评分项 {{ item.code }} · {{ item.after?.name }}（{{ item.after?.max_score }} 分）</template>
          <template v-else>补全 {{ item.code }} 的{{ FIELD[item.field] || item.field }}：{{ show(item.after) }}</template>
        </label>
      </div>
      <div v-for="item in groups.confirmable" :key="item.id" class="diff-row" :data-test="`item-${item.id}`">
        <label><input type="checkbox" :checked="confirm.has(item.id)" :disabled="busy" @change="toggle('confirm', item.id, $event.target.checked)" />
          第 {{ item.row_number || '—' }} 行 · 确认修改 {{ item.code }} 的{{ FIELD[item.field] || item.field }}：{{ show(item.before) }} → {{ show(item.after) }}</label>
      </div>
      <div v-for="item in groups.conflicts" :key="item.id" class="diff-row conflict" :data-test="`item-${item.id}`">
        <label><input type="checkbox" :checked="exclude.has(item.id)" :disabled="busy" @change="toggle('exclude', item.id, $event.target.checked)" />
          排除冲突项 {{ item.after?.name }}（{{ item.reason === 'duplicate_name' ? '名称重复' : '编号与已有评分项冲突' }}）</label>
      </div>
      <p v-if="groups.removed.length" class="notice notice-danger">该结构会移除已有评分项（{{ groups.removed.map((i) => i.code).join('、') }}），无法合入；请重新识别或重新导入文件。</p>
      <p v-if="plan.keptCurrent.length" class="faint">{{ plan.keptCurrent.length }} 处修改未确认，将保留当前值。</p>
      <p class="faint">合入会生成新的执行草稿；合入的内容仍需在第 2、3 步逐项确认。</p>
      <button class="btn btn-primary" data-test="merge" :disabled="!canMerge" @click="emit('merge', plan.payload)">按此结构合入</button>
    </template>
    <template v-else-if="suggestion.status === 'merged'">
      <p class="notice">已按 AI 识别的结构合入。发布前可以撤销（合入新增过评分项时无法撤销）。</p>
      <button class="btn" data-test="undo" :disabled="busy || !editable" @click="emit('undo')">撤销合入</button>
    </template>
    <p v-else-if="suggestion.status === 'undone'" class="notice">已撤销上次合入。</p>
  </section>
</template>

<style scoped>
.diff-row { padding: 8px 0; border-top: 1px solid #eeefeb; }
.diff-row label { display: flex; gap: 8px; align-items: flex-start; }
.diff-row.conflict { color: #b83a2d; }
</style>
