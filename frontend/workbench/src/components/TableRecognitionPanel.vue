<script setup>
import { computed, ref } from 'vue';
import { canRetryAiTask, isActiveAiTask, structureTaskProgress } from '@/lib/ai-tasks.js';
// task：最近一次结构识别任务（后台执行，关闭页面不会中断；成功后建议出现在右侧差异面板）。
const props = defineProps({ state: Object, previews: { type: Array, default: () => [] }, busy: Boolean, editable: Boolean, connection: Object, estimate: Object, task: { type: Object, default: null } });
const emit = defineEmits(['suggest-structure', 'task-action']);
const running = computed(() => isActiveAiTask(props.task));
const source = ref(false);
const extraction = computed(() => props.state?.extraction);
const labels = { code:'编号', name:'评分项名称', max_score:'满分', description:'评分说明', deduction_rules:'扣分规则', dimension:'维度', item_label:'评分项标识', evidence_hints:'证据提示' };
const columns = computed(() => {
  const mapping = extraction.value?.mapping || {};
  const keys = new Set([...Object.values(mapping), ...(extraction.value?.unmapped_columns || []).map(c => c.column)]);
  return [...keys].sort((a,b) => Number(a)-Number(b)).map(number => ({ number, roles: Object.entries(mapping).filter(([,v]) => v === number).map(([k]) => labels[k] || k), unused: (extraction.value?.unmapped_columns || []).find(c => c.column === number) }));
});
function columnName(number) { let n = Number(number), result = ''; while(n > 0) { n--; result = String.fromCharCode(65 + n % 26) + result; n = Math.floor(n / 26); } return result; }
const rows = computed(() => {
  const result = new Map();
  for (const u of props.previews) {
    const match = u.unit_id?.match(/!R(\d+)C(\d+)$/);
    if (!match) continue;
    const key = u.unit_id.slice(0, u.unit_id.lastIndexOf('C'));
    const sheet = u.locator?.sheet ?? u.unit_id.slice(u.unit_id.indexOf(':') + 1, u.unit_id.lastIndexOf('!'));
    if (!result.has(key)) result.set(key, { key, number: Number(match[1]), sheet, cells: [], refs: [] });
    result.get(key).cells.push({ column: Number(match[2]), text: u.text });
    result.get(key).refs.push(...(u.locator?.review?.claimed_by || []));
  }
  return [...result.values()].map(row => ({ ...row, role: rowRole(row) }));
});
// 「识别为」只读取后端抽取结果与台账归属（`<评分项编号>.<字段>`），不按显示次序猜测对应关系。
function rowRole(row) {
  const x = extraction.value || {};
  if (x.sheet_title && row.sheet !== x.sheet_title) return { label: '其他工作表 · 未使用', kind: 'muted' };
  if (row.number === x.header_row) return { label: '表头', kind: 'header' };
  if (row.number === x.total_row?.row_number) return { label: '合计 · 不计入', kind: 'total' };
  const dropped = (x.dropped_rows || []).find(item => item.row_number === row.number);
  if (dropped) return { label: `未计入 · ${dropped.reason || '未识别'}`, kind: 'muted' };
  const codes = [...new Set(row.refs.filter(ref => !ref.startsWith('template_items.') && ref.lastIndexOf('.') > 0).map(ref => ref.slice(0, ref.lastIndexOf('.'))))];
  const record = (x.records || []).find(item => item.row_number === row.number);
  if (record) return { label: codes.length ? `评分项 → ${codes.join('、')}` : record.name ? `评分项 · ${record.name}` : '评分项', kind: 'criterion' };
  if (codes.length) return { label: `规则来源 → ${codes.join('、')}`, kind: 'criterion' };
  return { label: row.number < x.header_row ? '表头前 · 未使用' : '未使用', kind: 'muted' };
}
const maxColumn = computed(() => Math.max(1,...rows.value.flatMap(r => r.cells.map(c => c.column))));
</script>
<template>
<section v-if="extraction" class="card table-recognition">
  <header><div><strong>{{ extraction.sheet_title || '表格结构' }}</strong><p class="faint">先确认列用途与行类型，再核对评分项</p></div><div class="view-toggle" role="group" aria-label="表格查看方式"><button type="button" class="view-option" :class="{ selected: !source }" :aria-pressed="!source" @click="source = false">识别结果</button><button type="button" class="view-option" :class="{ selected: source }" :aria-pressed="source" @click="source = true">原表对照</button></div></header>
  <template v-if="!source"><div class="column-cards"><div v-for="column in columns" :key="column.number" class="column-card"><strong><span class="mono">{{ columnName(column.number) }}</span> {{ column.unused?.header || '' }}</strong><p>{{ column.roles.join(' / ') || '未使用' }}</p><small v-if="(column.roles.includes('评分项名称') || column.roles.includes('评分项标识')) && !extraction.mapping?.max_score" class="warn">满分 · 从文字提取，请核对</small></div></div><p class="row-summary">行识别：第 {{ extraction.header_row }} 行 · 表头；{{ extraction.records?.length || 0 }} 行 · 评分项<span v-if="extraction.total_row">；第 {{ extraction.total_row.row_number }} 行 · 合计（不计入）</span></p></template>
  <div v-else class="raw-table"><table><thead><tr><th>行</th><th v-for="n in maxColumn" :key="n">{{ columnName(n) }}</th><th class="role-col">识别为</th></tr></thead><tbody><tr v-for="row in rows" :key="row.key" :class="`row-${row.role.kind}`" :data-test="`raw-row-${row.number}`"><th>{{ row.number }}</th><td v-for="n in maxColumn" :key="n">{{ row.cells.find(c => c.column === n)?.text || '' }}</td><td class="role-col"><span class="role-chip" :class="row.role.kind">{{ row.role.label }}</span></td></tr></tbody></table><p v-if="!rows.length">没有可用的单元格预览，请使用文件原文预览。</p></div>
  <div class="structure-help"><p v-for="(trigger,i) in state?.triggers || []" :key="i" class="warn">{{ trigger.message }}</p><p class="faint">AI 只识别表头、列用途与行类型，不改写原文；确认差异后才合入。AI 连接：{{ connection ? `${connection.name} · ${connection.model_name}` : '未启用，请在账户与连接中启用' }}</p><p v-if="task && task.status !== 'succeeded'" class="structure-task" data-test="structure-task" :class="{ warn: task.status === 'failed' }"><span>{{ structureTaskProgress(task) }}</span><button v-if="running" class="btn btn-sm" data-test="structure-cancel" :disabled="busy || !editable" @click="emit('task-action', 'cancel')">取消识别</button><button v-if="canRetryAiTask(task)" class="btn btn-sm" data-test="structure-retry" :disabled="busy || !editable" @click="emit('task-action', 'retry')">重试</button></p><button v-if="!estimate" class="btn" data-test="structure-estimate" :disabled="busy || running || !editable" @click="emit('suggest-structure', { dryRun: true })">用 AI 识别表格结构…</button><template v-else><p>将发送约 {{ estimate.chars }} 字符，调用 {{ estimate.calls }} 次模型。</p><button class="btn btn-primary" data-test="structure-run" :disabled="busy || running || !editable || !connection" @click="emit('suggest-structure', { dryRun: false })">确认调用 AI 识别结构</button></template></div>
</section>
</template>
<style scoped>
.table-recognition{overflow:hidden}header{display:flex;justify-content:space-between;gap:14px;padding:20px 22px;border-bottom:1px solid #e6e7e2}.view-toggle {
  display: inline-flex;
  align-self: center;
  flex-shrink: 0;
  gap: 4px;
  padding: 4px;
  border-radius: 12px;
  background: #f1f0ec;
}
.view-option {
  appearance: none;
  border: 0;
  border-radius: 9px;
  padding: 9px 18px;
  background: transparent;
  color: #787973;
  font: inherit;
  font-size: 14px;
  line-height: 1.5;
  white-space: nowrap;
  cursor: pointer;
  transition: background-color .15s, color .15s, box-shadow .15s;
}
.view-option.selected {
  background: #fff;
  color: #252923;
  box-shadow: 0 2px 4px #24282012;
}
.view-option:focus-visible {
  outline: 2px solid #175c50;
  outline-offset: 2px;
}
.column-cards{display:flex;gap:12px;padding:22px 22px 8px;flex-wrap:wrap}.column-card{flex:1;min-width:150px;border:1px solid #e4e6df;border-radius:8px;padding:16px}.column-card .mono{background:#f4f4ef;padding:5px 8px;margin-right:8px}.column-card p{font-size:13px;color:#175c50}.row-summary{padding:0 22px;font-size:12px;color:#7e8679}.structure-help{padding:16px 22px;background:#fffbf2;border-top:1px solid #eee5d5}.faint{font-size:12px;color:#8d9587;line-height:1.7}.warn{color:#986826;font-size:13px}.structure-task{display:flex;flex-wrap:wrap;align-items:center;gap:10px;font-size:13px}.raw-table{max-height:380px;overflow:auto;padding:15px}.raw-table table{border-collapse:collapse;min-width:100%}td,th{border:1px solid #e3e5dd;padding:10px;font-size:12px;min-width:80px;max-width:350px;white-space:pre-wrap}th{background:#f5f6f1}.role-col{min-width:120px}.role-chip{display:inline-block;font-size:11.5px;padding:2px 8px;border-radius:4px;white-space:nowrap;background:#eeede8;color:#6f716a}.role-chip.criterion{background:#e9efec;color:#175c50}tr.row-header td,tr.row-total td{background:#faf9f6;font-weight:600}tr.row-muted td{color:#8d9587}@media(max-width:650px){header{flex-direction:column}}
</style>
