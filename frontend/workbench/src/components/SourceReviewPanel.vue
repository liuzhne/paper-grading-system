<script setup>
import { computed, ref, watch } from 'vue';
import { suggestionFor } from '@/lib/parse-coverage.js';
const props = defineProps({ state: Object, progress: Object, previews: { type: Array, default: () => [] }, criteria: { type: Array, default: () => [] }, busy: Boolean, editable: Boolean, connection: Object });
const emit = defineEmits(['resolve', 'classify', 'accept-suggestions']);
// 父组件可用 v-model:filter 直接打开某一筛选（例如第 2 步底部的「疑似规则」阻断项）。
const filter = defineModel('filter', { type: String, default: 'pending' });
const activeId = ref('');
const selected = ref(new Set());
const batchCode = ref('');
const locked = computed(() => props.busy || !props.editable);
const pending = computed(() => props.state?.coverage?.unclaimed || []);
const previewMap = computed(() => new Map(props.previews.map(u => [u.unit_id, u])));
const confidenceLabels = { high: '高置信', medium: '中置信', low: '低置信' };
/** 把 AI 归类结果转成可直接采纳的动作；建议的评分项已不存在时只展示、不可采纳。 */
function adviceFor(unitId) {
  const s = suggestionFor(props.state, unitId);
  if (!s) return null;
  const exclude = ['context', 'noise'].includes(s.label);
  const criterion = exclude ? null : props.criteria.find(c => c.code === s.suggested_criterion);
  return {
    label: exclude ? '不是规则' : criterion ? `归入 ${criterion.code} ${criterion.name || ''}`.trim() : `${s.label === 'rule' ? '疑似规则' : '疑似要求'}，未匹配到现有评分项`,
    confidence: confidenceLabels[s.confidence] || s.confidence || '',
    high: s.confidence === 'high',
    reason: s.reason || '',
    action: exclude ? 'not_rule' : criterion ? 'assign' : null,
    criterionCode: criterion?.code,
  };
}
const all = computed(() => {
  const pendingMap = new Map(pending.value.map(u => [u.unit_id, u]));
  const list = props.previews.map(u => ({ ...u, ...pendingMap.get(u.unit_id), pending: pendingMap.has(u.unit_id) }));
  for (const u of pending.value) if (!previewMap.value.has(u.unit_id)) list.push({ ...u, pending: true });
  return list.map(u => ({ ...u, advice: adviceFor(u.unit_id) }));
});
const filtered = computed(() => all.value.filter(u => filter.value === 'processed' ? !u.pending && u.locator?.review?.extracted_by === 'human' : filter.value === 'excluded' ? !u.pending && u.locator?.review?.status !== 'consumed' : u.pending && (filter.value === 'pending' || (filter.value === 'blocking' ? u.blocking : !u.blocking))));
const groups = computed(() => {
  const result = new Map();
  for (const u of filtered.value) {
    const path = u.locator?.section_path || u.locator?.heading_path;
    const key = (Array.isArray(path) ? path.join(' / ') : path) || u.locator?.sheet_name || u.locator?.sheet || '未标注章节';
    if (!result.has(key)) result.set(key, []);
    result.get(key).push(u);
  }
  return [...result.entries()];
});
const active = computed(() => filtered.value.find(u => u.unit_id === activeId.value) || filtered.value.find(u => u.blocking) || filtered.value[0]);
const context = computed(() => {
  const index = all.value.findIndex(u => u.unit_id === active.value?.unit_id);
  return index < 0 ? [] : all.value.slice(Math.max(0, index - 1), index + 2).filter(u => u.unit_id?.split(':')[0] === active.value?.unit_id?.split(':')[0]);
});
const suggestions = computed(() => classifyScope.value.map(u => ({ unit: u, suggestion: suggestionFor(props.state, u.unit_id) })).filter(({ suggestion: s }) => s?.confidence === 'high' && ((['rule', 'requirement'].includes(s.label) && props.criteria.some(c => c.code === s.suggested_criterion)) || ['context', 'noise'].includes(s.label))));
const counts = computed(() => {
  const docs = props.state?.coverage?.documents || [];
  return { assigned: docs.reduce((n,d) => n + (d.counts?.consumed || 0), 0), excluded: docs.reduce((n,d) => n + (d.counts?.context || 0) + (d.counts?.structural || 0) + (d.counts?.ignored_by_rule || 0), 0) };
});
watch(() => pending.value.map(u => u.unit_id).join('|'), (_, old) => {
  const ids = new Set(pending.value.map(u => u.unit_id));
  selected.value = new Set([...selected.value].filter(id => ids.has(id)));
  if (!ids.has(activeId.value)) {
    const previous = (old || '').split('|');
    const index = Math.max(0, previous.indexOf(activeId.value));
    activeId.value = pending.value[Math.min(index, pending.value.length - 1)]?.unit_id || '';
  }
});
function toggle(id, checked) { const next = new Set(selected.value); checked ? next.add(id) : next.delete(id); selected.value = next; }
function resolve(action, code, ids = [active.value?.unit_id]) {
  if (locked.value || !ids.length || !ids[0]) return;
  emit('resolve', { unitIds: ids, action, ...(code ? { criterionCode: code } : {}), reason: code ? `用户指派到评分项 ${code}` : action === 'restore' ? '用户恢复原文重新归类' : '用户确认不是评分规则' });
}
function adopt(unit) {
  const advice = unit?.advice;
  if (locked.value || !unit?.pending || !advice?.action) return;
  emit('resolve', { unitIds: [unit.unit_id], action: advice.action, ...(advice.criterionCode ? { criterionCode: advice.criterionCode } : {}), reason: '用户采纳 AI 归类建议' });
}
function move(delta) { const i = filtered.value.findIndex(u => u.unit_id === active.value?.unit_id); activeId.value = filtered.value[(i + delta + filtered.value.length) % filtered.value.length]?.unit_id || ''; }
function keydown(event) {
  if (event.ctrlKey || event.metaKey || event.altKey || event.target?.isContentEditable || ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(event.target?.tagName)) return;
  const key = event.key.toLowerCase();
  if (key === 'j' || key === 'k') { event.preventDefault(); move(key === 'j' ? 1 : -1); }
  else if (!locked.value && active.value?.pending && key === 'x') { event.preventDefault(); resolve('not_rule'); }
  else if (!locked.value && active.value?.pending && /^[1-9]$/.test(key) && props.criteria[Number(key) - 1]) { event.preventDefault(); resolve('assign', props.criteria[Number(key) - 1].code); }
}
const classification = computed(() => props.state?.unit_classifications);
const retryIds = computed(() => [...new Set([...(classification.value?.failed_unit_ids || []), ...(classification.value?.unclassified_unit_ids || [])])].filter(id => pending.value.some(u => u.unit_id === id)));
const classifyScope = computed(() => selected.value.size ? pending.value.filter(u => selected.value.has(u.unit_id)) : filtered.value.filter(u => u.pending));
// 送去 AI 归类的范围：显式勾选时照勾选重新判断；否则跳过已有有效建议的单元，
// 这样一轮中途停下后再点就是「继续剩余」，不会把已拿到建议的单元再付费跑一遍。
const skippedSuggested = computed(() => selected.value.size ? 0 : classifyScope.value.filter(u => suggestionFor(props.state, u.unit_id)).length);
const classifyTargets = computed(() => selected.value.size ? classifyScope.value : classifyScope.value.filter(u => !suggestionFor(props.state, u.unit_id)));
const classifyLabel = computed(() => props.busy ? '正在处理…' : !skippedSuggested.value ? `用 AI 给出归类建议（${classifyTargets.value.length} 条）` : classifyTargets.value.length ? `继续为剩余 ${classifyTargets.value.length} 条给出归类建议` : '均已有 AI 建议');
const progressNote = computed(() => {
  const p = props.progress;
  if (!p) return '';
  const failedNote = p.failed ? `${p.failed} 条未获得建议` : '';
  if (p.running) return `每批最多 3 条，最多 3 批并行处理中…${failedNote ? `已有 ${failedNote}，其余批次继续。` : ''}`;
  if (p.stopped === 'repeated') return '连续两批都没有拿到有效结果，本轮已停止，以免继续消耗额度；已完成的建议已保留，可稍后继续处理剩余内容。';
  if (p.stopped) return '模型服务出错，本轮已停止；已完成的建议已保留，可稍后继续处理剩余内容。';
  if (p.completed + (p.failed || 0) < p.total) return '本轮已停止，已发出的批次处理完毕，剩余内容可继续处理。';
  return failedNote ? `本轮处理完成，${failedNote}，可重试失败内容。` : '本轮处理完成。';
});
const failureLabels = { output_truncated: '模型输出达到长度上限，请检查连接的输出 token 配置', invalid_json: '模型未返回有效 JSON', invalid_output: '模型返回格式不完整', error_envelope: '模型服务返回错误', provider_error: '模型调用失败，请测试当前连接', request_timeout: '模型响应超时，本轮已停止；已完成的建议已保留，可缩小选择范围后重试', authentication_failed: 'API Key 验证失败，请在账户与连接中检查密钥', permission_denied: '当前连接没有模型访问权限', model_or_endpoint_not_found: '模型或接口地址不存在，请检查连接配置', rate_limited: '模型调用受到限流，请稍后重试', quota_exhausted: '模型额度已用完（余额不足或配额耗尽），等待不会恢复，请充值或更换连接', network_error: '连接模型服务失败，请检查服务端网络', provider_unavailable: '模型服务暂不可用，请稍后重试', capacity_unavailable: '模型服务容量不足，请稍后重试', circuit_open: '模型服务连续失败，已暂停调用，请稍后重试', invalid_request: '模型请求参数不兼容，请检查连接协议与模型配置', context_length_exceeded: '输入超出模型上下文限制，请减少选中的内容', request_too_large: '请求内容过大，请减少选中的内容', unprocessable_request: '模型无法处理当前请求，请检查连接配置', conflict: '模型请求冲突，请稍后重试', canceled: '模型调用已取消', unknown: '模型调用出现未知错误，请测试当前连接', invalid_envelope: '模型返回格式不完整', incomplete_output: '模型未完成输出', empty_content: '模型没有返回内容', refused: '模型拒绝回答这批内容（安全策略），可调整后重试' };
const failureMessages = computed(() => [...new Set((classification.value?.rejected || []).map(r => failureLabels[r.error]).filter(Boolean))]);
const signalLabels = { comment: '文档批注', score: '分值表述', verb: '扣分或评分动词', normative: '规范要求', profile: '领域关键词' };
</script>
<template>
<section class="card source-review" tabindex="0" aria-label="原文识别校对工作区" @keydown="keydown">
  <div class="progress-summary"><span>已排除或作为上下文 {{ counts.excluded }} 个单元</span><span>已归入 {{ counts.assigned }} 个单元</span><strong>待处理 {{ pending.length }} 个单元</strong><button class="btn btn-sm" @click="filter = 'excluded'">查看已排除内容</button></div>
  <div class="progress-track" aria-hidden="true"><span v-for="(value,i) in [counts.excluded, counts.assigned, pending.length]" :key="i" :class="`segment-${i}`" :style="{flex: value}" /></div>
  <div class="review-toolbar"><div class="filters" role="tablist" aria-label="原文处理状态"><button v-for="[value,label] in [['pending','待处理'],['blocking','疑似规则'],['requirements','疑似要求 / 其他'],['processed','已处理']]" :key="value" role="tab" :aria-selected="filter === value" :class="{ active: filter === value }" @click="filter = value">{{ label }} <span v-if="value === 'pending'">{{ pending.length }}</span></button></div><span class="faint">AI 连接：{{ connection ? `${connection.name} · ${connection.model_name}` : '未启用' }}</span><button class="btn btn-sm" data-test="classify" :disabled="locked || !connection || !classifyTargets.length" @click="emit('classify', { unitIds: classifyTargets.map(u => u.unit_id) })">{{ classifyLabel }}</button><span v-if="skippedSuggested" class="faint" data-test="classify-skipped">已有建议的 {{ skippedSuggested }} 条不再重复调用；需要重新判断时请先勾选。</span></div>
  <p v-if="progress" class="review-help" role="status">本轮已获得 {{ progress.completed }} / {{ progress.total }} 条建议。{{ progressNote }}</p>
  <div v-if="classification && !classification.stale" class="classification-result" role="status"><span>当前归类结果：成功 {{ classification.results?.length || 0 }} 条 · 失败 {{ classification.failed_unit_ids?.length || 0 }} 条 · 未返回 {{ classification.unclassified_unit_ids?.length || 0 }} 条</span><p v-for="message in failureMessages" :key="message">{{ message }}</p><button v-if="retryIds.length" class="btn btn-sm" :disabled="locked || !connection" @click="emit('classify', { unitIds: retryIds })">重试失败或未返回内容（{{ retryIds.length }}）</button></div>
  <p class="review-help">疑似规则必须处理；其余未归入的内容不作为新增评分依据。AI 建议需由你确认采纳。</p>
  <p v-if="state?.unit_classifications?.stale" class="notice notice-warn">评分项已变化，之前的 AI 建议已过期，请重新判断。</p>
  <div v-if="suggestions.length" class="bulk-bar"><span>当前范围有 {{ suggestions.length }} 条高置信建议可采纳</span><button class="btn btn-sm" :disabled="locked" @click="emit('accept-suggestions', suggestions.map(({unit, suggestion}) => ({ unitIds: [unit.unit_id], action: ['context','noise'].includes(suggestion.label) ? 'not_rule' : 'assign', criterionCode: suggestion.suggested_criterion, reason: '用户确认采纳高置信 AI 归类建议' })))">采纳高置信 AI 建议</button></div>
  <div v-if="selected.size" class="bulk-bar"><strong>已选 {{ selected.size }} 条</strong><select v-model="batchCode" class="select" aria-label="批量归入评分项" :disabled="locked"><option value="">选择评分项</option><option v-for="c in criteria" :key="c.code" :value="c.code">{{ c.code }} · {{ c.name }}</option></select><button class="btn btn-sm" :disabled="locked || !batchCode" @click="resolve('assign', batchCode, [...selected])">批量归入</button><button class="btn btn-sm" data-test="batch-not-rule" :disabled="locked" @click="resolve('not_rule', null, [...selected])">不是规则</button></div>
  <div class="review-columns">
    <div class="paragraph-list"><p v-if="!filtered.length" class="empty">当前分类没有内容。</p><section v-for="[name, units] in groups" :key="name"><div class="group-heading"><strong>{{ name }} <span class="faint">{{ units.length }} 个单元</span></strong><button class="btn btn-sm" :disabled="locked || !units.some(u => u.pending)" @click="units.filter(u => u.pending).forEach(u => toggle(u.unit_id, true))">选择本组</button></div><div v-for="u in units" :key="u.unit_id" class="paragraph-row" :class="{ selected: active?.unit_id === u.unit_id }" :data-test="`unit-${u.unit_id}`"><input v-if="u.pending" type="checkbox" :aria-label="`选择 ${u.unit_id}`" :checked="selected.has(u.unit_id)" :disabled="locked" @change="toggle(u.unit_id, $event.target.checked)"><div class="paragraph-body"><button class="paragraph" @click="activeId = u.unit_id"><span class="paragraph-meta"><span :class="['chip', u.blocking ? 'chip-danger' : 'chip-warn']">{{ u.pending ? u.blocking ? '疑似规则 · 必须处理' : u.suspected ? '疑似要求' : '待核对' : '已处理' }}</span><span class="mono faint">{{ u.unit_id }}</span></span><span class="excerpt">{{ u.text }}</span></button><p v-if="u.pending && u.advice" class="ai-line" :class="{ tentative: !u.advice.high }" :data-test="`ai-line-${u.unit_id}`"><span>AI：{{ u.advice.label }}</span><span class="mono faint">{{ u.advice.confidence }}</span><button v-if="u.advice.action" class="adopt-link" type="button" :disabled="locked" @click="adopt(u)">采纳</button></p></div></div></section></div>
    <aside v-if="active" class="context-panel"><h3>原文对照 <small class="mono faint">{{ active.unit_id }}</small></h3><div v-for="u in context" :key="u.unit_id" class="context-text" :class="{ current: u.unit_id === active.unit_id }">{{ u.text }}</div><h4>识别依据</h4><p>{{ active.signals?.length ? active.signals.map(s => signalLabels[s] || s).join('、') : active.locator?.review?.reason || '请结合上下文人工核对' }}</p><div v-if="active.advice" class="ai-card" :class="{ tentative: !active.advice.high }" data-test="ai-card"><span class="ai-badge">AI</span><div class="ai-card-body"><p><strong>{{ active.advice.label }}</strong> <span class="mono faint">{{ active.advice.confidence }}</span></p><p v-if="active.advice.reason" class="faint">{{ active.advice.reason }}</p></div><button v-if="active.pending && active.advice.action" class="btn btn-sm adopt-button" type="button" :disabled="locked" @click="adopt(active)">采纳</button></div><template v-if="active.pending"><h4>归入评分项</h4><div class="criterion-buttons"><button v-for="(c,i) in criteria" :key="c.code" class="btn" :disabled="locked" @click="resolve('assign', c.code)"><kbd>{{ i < 9 ? i + 1 : '' }}</kbd>{{ c.code }} {{ c.name }}</button></div><div class="review-actions"><button class="btn" :disabled="locked" @click="resolve('not_rule')">不是规则 <kbd>X</kbd></button><button class="btn" @click="move(1)">跳过 <kbd>J</kbd></button></div></template><button v-if="!active.pending" class="btn btn-sm" :disabled="locked" @click="resolve('restore')">恢复到待归类</button><p class="faint">聚焦工作区后：1–9 归入 · X 不是规则 · J / K 下一段 / 上一段</p></aside>
  </div>
</section>
</template>
<style scoped>
.classification-result{padding:16px 22px;background:#fff8ec;font-size:13px}.progress-track{display:flex;height:5px;background:#f0f1eb}.segment-0{background:#b4bcb0}.segment-1{background:#43856c}.segment-2{background:#c99e53}.source-review{overflow:hidden;outline-color:#175c50}.progress-summary,.review-toolbar,.bulk-bar{display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:18px 22px;border-bottom:1px solid #e6e7e2}.progress-summary{background:#f3f6f3;color:#376255}.filters{display:flex;gap:4px;flex-wrap:wrap}.filters button{border:0;background:none;padding:9px 12px;border-radius:6px;color:#70786f;cursor:pointer}.filters .active{background:#e7efea;color:#175c50;font-weight:600}.review-help{padding:0 22px;color:#777f74;font-size:12px}.review-columns{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(280px,1fr)}.paragraph-list{max-height:680px;overflow:auto;border-right:1px solid #e5e7e1}.group-heading{display:flex;justify-content:space-between;align-items:center;padding:12px 18px;background:#f8f8f5;position:sticky;top:0}.paragraph-row{display:flex;align-items:start;gap:10px;padding:16px 18px;border-bottom:1px solid #eeefeb}.paragraph-row.selected{background:#f0f6f2;box-shadow:inset 3px 0 #175c50}.paragraph{border:0;background:none;text-align:left;cursor:pointer;min-width:0;width:100%;color:inherit;font:inherit}.paragraph-meta{display:flex;justify-content:space-between;gap:8px;margin-bottom:9px;flex-wrap:wrap}.excerpt{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;line-height:1.8;font-size:13px}.context-panel{padding:22px;max-height:680px;overflow:auto}.context-panel h3{margin-top:0;font-size:15px}.context-panel h4{font-size:13px;margin:20px 0 10px}.context-text{font-size:13px;line-height:1.9;color:#969a93;padding:10px;white-space:pre-wrap;overflow-wrap:anywhere}.context-text.current{color:#313b33;background:#fff9ec;border-left:3px solid #bc8e3d}.criterion-buttons{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.criterion-buttons .btn{white-space:normal;text-align:left;justify-content:start;font-size:12px}.review-actions{display:flex;gap:10px;margin-top:14px}kbd{font:11px monospace;background:#eff0eb;padding:3px 5px;margin-right:6px}.bulk-bar{background:#f0f5f1}.bulk-bar .select{width:auto}.empty{padding:30px;color:#8b9387}.paragraph-body{flex:1;min-width:0}.ai-line{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 0 0;font-size:12px;color:#175c50}.ai-line.tentative{color:#8a5a12}.adopt-link{border:0;background:none;padding:0;font:inherit;font-weight:600;color:#175c50;cursor:pointer}.adopt-link:disabled{color:#a3a59d;cursor:not-allowed}.ai-card{display:flex;align-items:flex-start;gap:10px;margin-top:12px;padding:11px 13px;border:1px solid #dfe8e4;background:#f7faf9;border-radius:9px}.ai-card.tentative{border-color:#f0e6d2;background:#fdfaf3}.ai-badge{flex:none;font-size:10.5px;padding:1px 6px;border-radius:4px;background:#e2ece8;color:#175c50;font-weight:600;margin-top:2px}.ai-card-body{flex:1;min-width:0;font-size:13px;overflow-wrap:anywhere}.ai-card-body p{margin:0}.ai-card-body p+p{margin-top:3px}.adopt-button{flex:none}.faint{font-size:12px}.chip{font-size:11px}@media(max-width:1279px){.review-columns{grid-template-columns:1fr}.paragraph-list{max-height:340px;border-right:0}.context-panel{max-height:none}.criterion-buttons{grid-template-columns:1fr}}
</style>
