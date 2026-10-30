<script setup>
/**
 * 单篇得分解释（`paper_result` 卡片）：总分、各评分项得分条、扣分最多的几处（带原文与位置）、修改建议。
 * 分数与证据从接口现取，卡片只存编号（B8）。
 */
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import { api, StaleContextError } from "@/api/client.js";
import { deductionReasons } from "@/lib/score-items.js";

import WsLayout from "./WsLayout.vue";

const props = defineProps({
  card: { type: Object, required: true },
  head: { type: Object, required: true },
});

const TOP = 3;
const router = useRouter();
const items = ref(null);
const loadError = ref(null);

onMounted(async () => {
  try {
    items.value = await api.get(`/scoring-runs/${props.card.run_id}/items?include_view=true`);
  } catch (err) {
    if (!(err instanceof StaleContextError)) loadError.value = "无法加载这一份的评分项";
  }
});

const scoreOf = (item) => Number(item.final_score ?? item.ai_score ?? 0);
const total = computed(() => (items.value || []).reduce((sum, item) => sum + scoreOf(item), 0));
const max = computed(() => (items.value || []).reduce((sum, item) => sum + Number(item.max_score || 0), 0));
const rows = computed(() => (items.value || []).map((item) => {
  const ratio = item.max_score ? scoreOf(item) / Number(item.max_score) : 0;
  return { id: item.id, code: item.criterion_code, name: item.criterion_name || item.criterion_code,
    score: item.final_score ?? item.ai_score, max: Number(item.max_score), pct: `${Math.round(ratio * 100)}%`,
    low: ratio < 0.5, review: item.need_manual_review };
}));
const deductions = computed(() => (items.value || []).flatMap((item) => {
  const view = (item.evidence_view || []).find((entry) => entry.quote) || null;
  const where = view ? [view.section_title, view.page_start ? `第 ${view.page_start} 页` : ""].filter(Boolean).join(" · ") : "";
  const entries = (item.deduction_items || []).filter((entry) => Number(entry?.points) > 0);
  if (entries.length) {
    return entries.map((entry, index) => ({ key: `${item.id}-${index}`, code: item.criterion_code,
      issue: entry.reason || entry.rule_ref || "扣分", points: Number(entry.points), quote: view?.quote || "", where }));
  }
  return deductionReasons(item).map((reason, index) => ({ key: `${item.id}-r${index}`, code: item.criterion_code,
    issue: reason, points: null, quote: view?.quote || "", where }));
}));
const lost = computed(() => deductions.value.reduce((sum, entry) => sum + (entry.points || 0), 0));
const top = computed(() => deductions.value.slice().sort((a, b) => (b.points || 0) - (a.points || 0)).slice(0, TOP));
const suggestions = computed(() => (items.value || []).filter((item) => item.revision_suggestion)
  .map((item) => ({ id: item.id, code: item.criterion_code, text: item.revision_suggestion })));
const gradeHref = computed(() => router.resolve(`/batches/${props.card.batch_id}/grade?paper=${props.card.paper_id}`).href);
</script>

<template>
  <WsLayout v-bind="head">
    <div v-if="items" class="body">
      <div class="headline">
        <span class="mono big">{{ Math.round(total * 100) / 100 }}</span>
        <span class="of">/ {{ max }}</span>
        <span class="sum">{{ deductions.length ? `${deductions.length} 处扣分` : "没有记录扣分" }}{{ lost ? ` · 合计 −${Math.round(lost * 100) / 100}` : "" }}</span>
      </div>

      <div class="row-list scores">
        <div v-for="row in rows" :key="row.id" class="row score-row">
          <span class="row-idx">{{ row.code }}</span>
          <span class="score-name">{{ row.name }}<span v-if="row.review" class="tag tag-warn">需复核</span></span>
          <span class="score-bar"><span :class="{ low: row.low }" :style="{ width: row.pct }"></span></span>
          <span class="mono score-num">{{ row.score ?? "—" }}/{{ row.max }}</span>
        </div>
      </div>

      <section v-if="top.length">
        <div class="sec-head"><strong>扣分最多的 {{ top.length }} 处</strong></div>
        <div class="hits">
          <div v-for="hit in top" :key="hit.key" class="hit">
            <div class="hit-head">
              <span class="mono row-idx">{{ hit.code }}</span>
              <strong>{{ hit.issue }}</strong>
              <span v-if="hit.points" class="mono hit-points">−{{ hit.points }}</span>
            </div>
            <p v-if="hit.quote" class="quote">“{{ hit.quote }}”</p>
            <p v-if="hit.where" class="mono where">{{ hit.where }}</p>
          </div>
        </div>
      </section>

      <section v-if="suggestions.length">
        <div class="sec-head"><strong>修改建议</strong><span>按扣分理由整理</span></div>
        <ul class="suggestions">
          <li v-for="item in suggestions" :key="item.id" data-test="card-suggestion"><span class="mono row-idx">{{ item.code }}</span>{{ item.text }}</li>
        </ul>
      </section>
    </div>
    <p v-if="loadError" class="notice notice-danger pad">{{ loadError }}</p>
    <template #foot>
      <p class="foot-text">{{ deductions.length > top.length ? `其余 ${deductions.length - top.length} 处扣分可在结果复核中逐条查看。` : "可在结果复核中对照原文查看。" }}</p>
      <a class="btn btn-primary" :href="gradeHref" target="_blank" rel="noopener">在结果复核中打开</a>
    </template>
  </WsLayout>
</template>

<style scoped>
.body { padding: 14px 24px 0; display: flex; flex-direction: column; gap: 4px; }
.pad { margin: 12px 24px 0; }
.headline { display: flex; align-items: baseline; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; }
.big { font-size: 30px; font-weight: 600; }
.of { font-size: 14px; color: var(--text-muted); }
.sum { margin-left: auto; font-size: 12.5px; color: var(--text-muted); }
.score-row { display: grid !important; grid-template-columns: 40px minmax(0, 1fr) minmax(60px, 96px) 56px; gap: 10px; padding: 9px 14px; font-size: 13px; }
.score-name { display: flex; align-items: center; gap: 6px; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.score-bar { display: block; height: 8px; border-radius: 3px; background: var(--border-row); overflow: hidden; }
.score-bar > span { display: block; height: 100%; border-radius: 3px; background: var(--accent); }
.score-bar > span.low { background: var(--amber); }
.score-num { text-align: right; }
.hits { display: flex; flex-direction: column; gap: 8px; }
.hit { padding: 12px 14px; border: 1px solid var(--border-light); border-radius: var(--radius-md); }
.hit-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 13.5px; }
.hit-points { margin-left: auto; font-size: 15px; font-weight: 600; color: var(--danger); }
.quote { margin: 8px 0 0; padding: 9px 11px; background: var(--surface-muted); border-radius: 6px; font-size: 12.5px; line-height: 1.75; color: var(--text-secondary); }
.where { margin: 6px 0 0; font-size: 11px; color: var(--text-faint); }
.suggestions { margin: 0; padding: 0; list-style: none; display: grid; gap: 8px; font-size: 13px; line-height: 1.7; }
.suggestions li { display: flex; gap: 10px; }
</style>
