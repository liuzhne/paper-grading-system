<script setup>
defineProps({ rules: { type: Array, required: true }, disabled: Boolean });
const selects = {
  direction: ['评分方向', ['band','deduct','bonus','none']],
  effect_type: ['生效方式', ['score','review','block_submission','report_only']],
  judge_type: ['判定方式', ['deterministic','semantic']],
  strictness: ['要求程度', ['required','preferred','unknown']],
};
const labels = { band:'分档', deduct:'扣分', bonus:'加分', none:'不计分', score:'评分', review:'人工复核', block_submission:'阻止提交', report_only:'仅报告', deterministic:'确定性检查', semantic:'语义判断', required:'必须', preferred:'建议', unknown:'未指定' };
const advanced = { checker_key:'检查器', checker_params:'检查器参数', evidence_policy:'证据要求', positive_example:'正例', negative_example:'反例', boundary_example:'边界示例', depends_on_rule_codes:'依赖规则' };
function updateJson(event, object, key) {
  const input = event.target;
  try { object[key] = JSON.parse(input.value); input.setCustomValidity(''); }
  catch { input.setCustomValidity('请输入有效 JSON'); input.reportValidity(); }
}
</script>

<template>
  <details class="card card-pad">
    <summary>编辑当前评分项的原子规则</summary>
    <p class="faint">每条规则独立保存到新草稿，保留原始来源；保存后重新核对确认。</p>
    <fieldset v-for="rule in rules" :key="rule.id" :disabled="disabled" class="atomic-editor">
      <legend>{{ rule.rule_code }}</legend>
      <label class="field"><span class="field-label">规则名称</span><input v-model="rule.changes.name" class="input" /></label>
      <label class="field"><span class="field-label">条款正文</span><textarea v-model="rule.changes.rule_text" class="input" /></label>
      <div class="grid-2">
        <label v-for="([label, options], key) in selects" :key="key" class="field"><span class="field-label">{{ label }}</span><select v-model="rule.changes[key]" class="select"><option v-for="option in options" :key="option" :value="option">{{ labels[option] }}</option></select></label>
        <label class="field"><span class="field-label">规则分值</span><input v-model="rule.changes.max_points" type="number" min="0" step="0.01" class="input" @change="rule.changes.max_points ||= null" /></label>
        <label class="field"><span class="field-label">单条累计上限</span><input aria-label="单条累计上限" :disabled="rule.changes.repeat_policy !== 'capped'" v-model="rule.changes.cap_points" type="number" min="0" step="0.01" class="input" @change="rule.changes.cap_points ||= null" />
          <template v-if="rule.changes.repeat_policy !== 'capped' && rule.changes.cap_points != null">
            <span class="faint">当前命中方式不允许单条累计上限。清除后需保存并重新核对。</span>
            <button type="button" class="btn btn-sm" @click="rule.changes.cap_points = null">清除不适用的单条上限</button>
          </template>
        </label>
        <label class="field"><span class="field-label">重复命中方式</span><select v-model="rule.changes.repeat_policy" class="select"><option :value="null">不适用</option><option value="once">一次</option><option value="per_occurrence">每次</option><option value="capped">累计封顶</option></select></label>
        <label class="field"><span class="field-label">互斥组</span><input v-model="rule.changes.mutex_group" class="input" @change="rule.changes.mutex_group ||= null" /></label>
      </div>
      <label class="field"><span class="field-label">适用范围</span><input v-model="rule.changes.applies_to" class="input" /></label>
      <div v-for="(level, index) in rule.changes.levels" :key="index" class="level-editor">
        <label class="field"><span class="field-label">档位编号</span><input v-model="level.code" class="input" /></label>
        <label class="field"><span class="field-label">档位分值</span><input v-model="level.points" type="number" min="0" step="0.01" class="input" /></label>
        <label class="field"><span class="field-label">档位说明</span><textarea v-model="level.descriptor" class="input" /></label>
        <button class="btn btn-sm" @click="rule.changes.levels.splice(index, 1)">删除此档位</button>
      </div>
      <button class="btn btn-sm" @click="rule.changes.levels.push({code:'',points:0,descriptor:''})">添加档位</button>
      <details><summary>证据、依赖与示例</summary>
        <label v-for="(label, key) in advanced" :key="key" class="field"><span class="field-label">{{ label }}</span>
          <textarea v-if="['checker_params','evidence_policy','depends_on_rule_codes'].includes(key)" :value="JSON.stringify(rule.changes[key], null, 2)" class="input" @change="updateJson($event, rule.changes, key)" />
          <textarea v-else v-model="rule.changes[key]" class="input" @change="rule.changes[key] ||= null" />
        </label>
      </details>
    </fieldset>
  </details>
</template>
<style scoped>
.atomic-editor { border:1px solid var(--border); border-radius:8px; margin-top:16px; padding:16px; min-width:0; }
.level-editor { padding:12px; margin-bottom:12px; background:var(--bg); }
</style>
