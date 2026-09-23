<script setup>
import { ref } from "vue";

/**
 * 第 3 步 · 规则审查（解析重构方案 §7）。
 *
 * 覆盖率只能说明原文有没有被用到，审查检查的是“拆得对不对、映射得对不对”。
 * 先由代码做前置检查、估算规模，确认后才调用模型；模型只报告问题、不修改规则。
 * 审查可以跳过，但发布时会记录；单条问题可以豁免，必须写原因。
 */
const props = defineProps({
  review: { type: Object, default: () => ({ reviewed: false }) },
  estimate: { type: Object, default: null },
  busy: { type: Boolean, default: false },
  editable: { type: Boolean, default: false },
  connectionId: { type: String, default: "" },
});
const emit = defineEmits(["estimate", "run", "dismiss"]);

const scope = ref("priority");
const reasons = ref({});
const TYPE = {
  omission: "遗漏", distortion: "失真", granularity: "粒度", match_too_broad: "匹配过宽",
  match_too_narrow: "匹配过窄", undecidable: "难以判定", cross_duplicate: "跨项重复扣分",
};
const SEVERITY = { high: "高", medium: "中", low: "低" };
</script>

<template>
  <section class="card card-pad" data-test="rule-audit">
    <h2 class="card-title">规则审查（可选）</h2>
    <p v-if="!review?.reviewed" class="card-note">审查由 AI 对照原文检查拆分与映射是否正确，只报告问题不修改规则。可以跳过，但发布时会记录“未经审查即发布”。</p>
    <p v-else-if="review.stale" class="notice notice-warn">规则已修改，审查结果已过期，建议重新审查。</p>
    <div class="actions">
      <label class="field"><span class="field-label">审查范围</span>
        <select v-model="scope" class="select" :disabled="busy">
          <option value="priority">优先项（多判断、区间分值、AI 起草、跨项共用）</option>
          <option value="all">全部评分项</option>
        </select>
      </label>
      <button class="btn" data-test="review-estimate" :disabled="busy || !editable" @click="emit('estimate', { scope })">估算审查规模</button>
      <button v-if="estimate" class="btn btn-primary" data-test="review-run" :disabled="busy || !editable || !connectionId" @click="emit('run', { scope })">
        确认审查 {{ estimate.criteria_codes?.length || 0 }} 个评分项（调用 {{ estimate.estimate?.calls }} 次）
      </button>
    </div>
    <p v-if="estimate && !connectionId" class="faint">请先在第 1 步选择 AI 连接。</p>

    <template v-if="(review?.prechecks || estimate?.prechecks || []).length">
      <h3>代码前置检查</h3>
      <ul><li v-for="(item, index) in review?.prechecks || estimate?.prechecks" :key="index">{{ item.criterion_code ? `${item.criterion_code} · ` : '' }}{{ item.message }}</li></ul>
    </template>

    <template v-if="review?.reviewed">
      <h3>审查发现（{{ review.findings.length }}）</h3>
      <p v-if="!review.findings.length" class="faint">没有发现问题。</p>
      <div v-for="finding in review.findings" :key="finding.id" class="finding" :data-test="`finding-${finding.id}`">
        <p><span class="chip" :class="finding.severity === 'high' ? 'chip-danger' : 'chip-warn'">{{ TYPE[finding.type] || finding.type }} · {{ SEVERITY[finding.severity] }}</span>
          {{ finding.criterion_code || '跨评分项' }} · {{ finding.problem }}</p>
        <p class="faint">原文：“{{ finding.quote }}”<template v-if="finding.example"> · 例：{{ finding.example }}</template></p>
        <p v-if="finding.status === 'dismissed'" class="faint">已豁免：{{ finding.dismiss_reason }}</p>
        <div v-else class="actions">
          <input v-model="reasons[finding.id]" class="input" placeholder="豁免原因" :disabled="busy || !editable" />
          <button class="btn btn-sm" data-test="dismiss" :disabled="busy || !editable || !(reasons[finding.id] || '').trim()"
            @click="emit('dismiss', { id: finding.id, reason: reasons[finding.id].trim() })">豁免</button>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.actions { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; }
.finding { border-top: 1px solid #eeefeb; padding: 10px 0; }
.finding p { margin: 4px 0; }
</style>
