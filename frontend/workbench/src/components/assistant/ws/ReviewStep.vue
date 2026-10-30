<script setup>
/**
 * 核对评分规则（导入流程第 2 步）：逐项确认或排除扣分细则，全部处理后发布。
 * 确认、排除、恢复、提交审核、发布调用与评分标准页同一组接口；分享范围与有阻断项的处理留在评分标准页。
 * 排除即时保存，排除的细则不参与发布与评分（排除落库改造方案）。
 * 导入的识别结果还没确认时（评分项、分值的结构核对），先引导到评分标准页确认。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";

import { api, StaleContextError } from "@/api/client.js";
import RuleCard from "@/components/RuleCard.vue";
import { useAssistantStore } from "@/stores/assistant.js";
import { useRubricsStore } from "@/stores/rubrics.js";

import WsLayout from "./WsLayout.vue";

const props = defineProps({
  context: { type: Object, required: true },
  head: { type: Object, required: true },
  /** 工作区够宽时评分项列在左边，窄时是一排可横滑的小标签。 */
  wide: Boolean,
});

const router = useRouter();
const store = useAssistantStore();
const rubricsStore = useRubricsStore();
const session = ref(null);
const rubric = ref(null);
const workspace = ref(null);
const selected = ref(null);
const basisOpen = ref(true);
const busy = ref(false);
const actionError = ref(null);
const loadError = ref(null);

const sessionId = computed(() => props.context.importSessionId || null);
const rubricId = computed(() => session.value?.rubric_id || props.context.rubricId || null);
const published = computed(() => rubric.value?.status === "published");
const editable = computed(() => Boolean(rubric.value) && rubric.value.status === "draft" && !busy.value);

async function load() {
  loadError.value = null;
  try {
    if (sessionId.value) session.value = await api.get(`/rubrics/import-sessions/${sessionId.value}`);
  } catch (err) {
    if (err instanceof StaleContextError) return;
    session.value = null; // 过期的导入会话不影响按评分标准核对
  }
  if (!rubricId.value) return;
  try {
    const [nextRubric, nextWorkspace] = await Promise.all([
      api.get(`/rubrics/${rubricId.value}`), api.get(`/rubrics/${rubricId.value}/review-workspace`),
    ]);
    rubric.value = nextRubric;
    workspace.value = nextWorkspace;
    if (!selected.value || !criteria.value.some((item) => item.id === selected.value)) {
      selected.value = (criteria.value.find((item) => item.pending) || criteria.value[0])?.id || null;
    }
  } catch (err) {
    if (!(err instanceof StaleContextError)) loadError.value = "无法加载评分规则";
  }
}
// 在另一个标签页（评分标准页）确认了识别结果或改了细则，回到这里时重新取。
function onVisible() {
  if (document.visibilityState === "visible") load();
}
onMounted(() => {
  load();
  document.addEventListener("visibilitychange", onVisible);
});
onBeforeUnmount(() => document.removeEventListener("visibilitychange", onVisible));
watch(() => [sessionId.value, props.context.rubricId], load);

const rules = computed(() => workspace.value?.rules || []);
const criteria = computed(() => (rubric.value?.criteria || []).map((item) => {
  const own = rules.value.filter((rule) => rule.criterion_id === item.id);
  const pendingCount = own.filter((rule) => ["draft", "review"].includes(rule.status)).length;
  const approved = own.filter((rule) => rule.status === "approved").length;
  const rejected = own.filter((rule) => rule.status === "rejected").length;
  const active = own.length - rejected; // 参与评分的细则；至少保留 1 条
  return { ...item, rules: own, pendingCount, approved, rejected, active, pending: pendingCount > 0 || active === 0,
    label: active === 0 ? "无细则" : pendingCount ? `${pendingCount} 待确认` : "已确认",
    tone: active === 0 ? "danger" : pendingCount ? "warn" : "accent" };
}));
const current = computed(() => criteria.value.find((item) => item.id === selected.value) || null);
const pendingCriteria = computed(() => criteria.value.filter((item) => item.pending));
const pendingRules = computed(() => rules.value.filter((rule) => ["draft", "review"].includes(rule.status)).length);
const nextPending = computed(() => {
  const list = criteria.value;
  const index = list.findIndex((item) => item.id === selected.value);
  return [...list.slice(index + 1), ...list.slice(0, Math.max(index, 0))].find((item) => item.pending) || null;
});
const basis = computed(() => {
  const seen = new Set();
  return (current.value?.rules || []).flatMap((rule) => rule.sources || [])
    .filter((item) => item.text && !seen.has(item.text) && seen.add(item.text));
});
const hasAiPending = computed(() => (current.value?.rules || []).some((rule) => rule.ai_origin && ["draft", "review"].includes(rule.status)));
const blockers = computed(() => workspace.value?.structural_blockers || []);

async function confirmRules(list) {
  busy.value = true;
  actionError.value = null;
  store.noteTaskEdit();
  let done = 0;
  try {
    for (const rule of list) {
      await api.post(`/rubrics/${rubricId.value}/rules/${encodeURIComponent(rule.rule_code)}/confirm`, {
        compilation_id: workspace.value.compilation_id, rule_id: rule.id, content_token: rule.content_token,
        reason: "在评分助手里核对条款内容与来源后确认",
      });
      done += 1;
    }
  } catch (err) {
    actionError.value = `已确认 ${done} 条，后面的停下了：${err?.message || "确认失败"}。刷新后核对再试。`;
  } finally {
    await load();
    busy.value = false;
  }
}

/** 排除 / 恢复一条细则：即时保存；恢复后回到待确认。 */
async function setExcluded(rule, exclude) {
  busy.value = true;
  actionError.value = null;
  store.noteTaskEdit();
  try {
    const code = encodeURIComponent(rule.rule_code);
    const body = { compilation_id: workspace.value.compilation_id, rule_id: rule.id, content_token: rule.content_token,
      reason: exclude ? "在评分助手里核对后排除，不参与评分" : "在评分助手里恢复已排除的细则，重新核对" };
    if (exclude) await api.post(`/rubrics/${rubricId.value}/rules/${code}/exclude`, body);
    else await api.post(`/rubrics/${rubricId.value}/rules/${code}/restore`, body);
  } catch (err) {
    actionError.value = `${exclude ? "排除" : "恢复"}没有成功：${err?.message || "操作失败"}。刷新后核对再试。`;
  } finally {
    await load();
    busy.value = false;
  }
}

async function confirmCurrent() {
  const list = (current.value?.rules || []).filter((rule) => ["draft", "review"].includes(rule.status));
  const next = nextPending.value;
  await confirmRules(list);
  if (!actionError.value && next) selected.value = next.id;
}

/** 发布：草稿先提交审核，再发布当前执行草稿；分享范围不改（要分享给本组织，在评分标准页发布）。 */
async function publish() {
  busy.value = true;
  actionError.value = null;
  store.noteTaskEdit();
  try {
    if (rubric.value.status === "draft") await api.post(`/rubrics/${rubricId.value}/submit-review`);
    await rubricsStore.publish(rubricId.value, { compilationId: workspace.value?.compilation_id, visibility: null,
      reason: "在评分助手里核对后发布" });
  } catch (err) {
    actionError.value = `发布没有成功：${err?.message || "发布失败"}。可以在评分标准页查看原因。`;
  } finally {
    await load();
    busy.value = false;
  }
  if (published.value && store.pending?.kind === "wait_publish") {
    await store.resume(store.pending.card_id, { action: "check" }); // 不等轮询，马上接着走
  }
}

const sessionLink = computed(() => router.resolve(`/rubrics?import_session=${sessionId.value}`).href);
const rubricLink = computed(() => (rubricId.value ? { href: router.resolve(`/rubrics?rubric=${rubricId.value}`).href, label: "在评分标准页打开" } : null));
const awaitingStructure = computed(() => !rubricId.value && session.value?.status === "draft");
const footer = computed(() => {
  if (!rubric.value) return { text: awaitingStructure.value ? "识别结果确认后，这里逐项核对扣分细则。" : "" };
  if (published.value) return { text: `${rubric.value.version} 已发布，发布后版本不可修改。` };
  const head = `已确认 ${criteria.value.length - pendingCriteria.value.length}/${criteria.value.length} 项 · 待确认 ${pendingRules.value} 条细则`;
  if (current.value?.pendingCount) {
    return { text: head, primary: `确认本项 ${current.value.pendingCount} 条${nextPending.value ? "，下一项" : ""}`, act: confirmCurrent };
  }
  if (nextPending.value && nextPending.value.active) {
    return { text: head, primary: `下一项待确认：${nextPending.value.code}`, act: () => { selected.value = nextPending.value.id; } };
  }
  if (pendingCriteria.value.length) return { text: `${head}。有评分项还没有细则，请在评分标准页补齐。`, link: true };
  if (blockers.value.length) return { text: `细则已全部确认，还有 ${blockers.value.length} 个问题要在评分标准页处理。`, link: true };
  return { text: `${criteria.value.length} 项已全部确认，发布后用于本次评分（仅自己可见；要分享给本组织，在评分标准页发布）。`,
    primary: `发布评分标准 ${rubric.value.version}`, act: publish };
});
</script>

<template>
  <WsLayout v-bind="head" :link="rubricLink">
    <template #head-actions>
      <button class="text-btn" type="button" :disabled="busy" @click="load">刷新</button>
    </template>

    <div v-if="awaitingStructure" class="body">
      <div class="structure">
        <strong>先确认识别出的评分项</strong>
        <p>导入的 {{ session.criteria?.length || 0 }} 个评分项、合计 {{ session.total_score }} 分需要先在评分标准页核对并确认，确认后这里逐项核对扣分细则。</p>
        <a class="btn btn-outline btn-sm" :href="sessionLink" target="_blank" rel="noopener">在评分标准页确认识别结果</a>
      </div>
    </div>

    <div v-else-if="rubric" class="review" :class="{ wide }">
      <div v-if="!wide" class="chips" role="tablist" aria-label="评分项">
        <button v-for="item in criteria" :key="item.id" class="chip-btn" :class="{ on: item.id === selected }" type="button"
                role="tab" :aria-selected="item.id === selected" :title="item.name" @click="selected = item.id">
          <span class="mono">{{ item.code }}</span>
          <span class="mark" :class="`tone-${item.tone}`">{{ item.tone === "accent" ? "✓" : item.active ? item.pendingCount : "!" }}</span>
        </button>
      </div>
      <nav v-else class="nav" aria-label="评分项">
        <button v-for="item in criteria" :key="item.id" class="nav-item" :class="{ on: item.id === selected }" type="button" @click="selected = item.id">
          <span class="mono code">{{ item.code }}</span>
          <span class="nav-name">{{ item.name }}</span>
          <span class="nav-state" :class="`tone-${item.tone}`">{{ item.label }}</span>
        </button>
      </nav>

      <div v-if="current" class="detail">
        <div class="crit-head">
          <div class="row-main">
            <div class="mono crit-code">{{ current.code }} · 满分 {{ Number(current.max_score) }}</div>
            <div class="crit-name">{{ current.name }}</div>
          </div>
          <span class="pill" :class="published ? 'tag-accent' : `tag-${current.tone}`">
            {{ published ? "已发布" : current.active === 0 ? "至少保留 1 条细则" : current.pendingCount ? `${current.pendingCount} 条待确认` : "本项已确认" }}
          </span>
        </div>

        <div class="basis">
          <button class="basis-toggle" type="button" :aria-expanded="basisOpen" @click="basisOpen = !basisOpen">
            <strong>评分依据</strong>
            <span class="basis-sum">{{ current.description ? "评分说明" : "" }}{{ basis.length ? ` · 原文 ${basis.length} 段` : "" }}</span>
            <span class="basis-act">{{ basisOpen ? "收起" : "展开" }}</span>
          </button>
          <div v-if="basisOpen" class="basis-body">
            <div v-if="current.description">
              <div class="basis-label">评分说明</div>
              <p>{{ current.description }}</p>
            </div>
            <div v-for="(item, index) in basis" :key="index">
              <div class="basis-label">原文 <span class="tag">{{ [item.sheet, item.row ? `第 ${item.row} 行` : ""].filter(Boolean).join(" · ") }}</span></div>
              <p class="quote">{{ item.text }}</p>
            </div>
            <p v-if="!current.description && !basis.length" class="faint">没有记录评分说明或原文。</p>
          </div>
        </div>

        <div class="sec-head">
          <strong>扣分细则</strong>
          <span>{{ current.rules.length }} 条 · 已确认 {{ current.approved }}{{ current.pendingCount ? ` · 待确认 ${current.pendingCount}` : "" }}{{ current.rejected ? ` · 已排除 ${current.rejected}` : "" }}</span>
        </div>
        <div class="rules">
          <RuleCard v-for="rule in current.rules" :key="rule.id" :rule="rule" :editable="editable" :busy="busy"
                    :can-exclude="current.active > 1" @confirm="confirmRules([rule])"
                    @exclude="setExcluded(rule, true)" @restore="setExcluded(rule, false)" />
          <p v-if="!current.rules.length" class="empty">这一项还没有扣分细则。可以在评分标准页让 AI 起草或手动补齐。</p>
        </div>
        <p v-if="hasAiPending" class="ai-note">标为“AI”的细则由模型起草，请核对内容与来源后确认；不保留的点“排除”，排除的不参与评分。</p>
      </div>
    </div>
    <p v-if="loadError" class="notice notice-danger pad">{{ loadError }}</p>
    <p v-if="actionError" class="notice notice-danger pad" role="alert">{{ actionError }}</p>

    <template v-if="footer.text" #foot>
      <p class="foot-text">{{ footer.text }}</p>
      <a v-if="footer.link && rubricLink" class="btn" :href="rubricLink.href" target="_blank" rel="noopener">在评分标准页处理</a>
      <button v-if="footer.primary" class="btn btn-primary" type="button" :disabled="busy" data-test="review-primary" @click="footer.act">
        {{ busy ? "处理中…" : footer.primary }}
      </button>
    </template>
  </WsLayout>
</template>

<style scoped>
.body { padding: 14px 24px 0; }
.pad { margin: 12px 24px 0; }
.structure { padding: 16px; border: 1px solid var(--warn-border); background: var(--warn-surface); border-radius: var(--radius-md); }
.structure strong { font-size: 14px; }
.structure p { margin: 6px 0 12px; font-size: 12.5px; line-height: 1.7; color: var(--neutral-ink); }
.review { display: grid; grid-template-columns: minmax(0, 1fr); align-items: start; }
.review.wide { grid-template-columns: 236px minmax(0, 1fr); }
.chips { position: sticky; top: 0; z-index: 2; display: flex; gap: 6px; overflow-x: auto; margin-top: 8px; padding: 10px 24px;
  background: var(--surface); border-bottom: 1px solid var(--border-light); }
.chip-btn { flex: none; display: flex; align-items: center; gap: 6px; height: 30px; padding: 0 11px; border: 1px solid var(--border);
  border-radius: 15px; background: var(--surface); color: var(--text-secondary); cursor: pointer; font-size: 12px; }
.chip-btn.on { background: var(--sidebar-bg); border-color: var(--sidebar-bg); color: #fff; }
.mark { font-size: 11.5px; font-weight: 600; }
.chip-btn.on .tone-accent { color: #9fd3c2; }
.chip-btn.on .tone-warn, .chip-btn.on .tone-danger { color: #f1c77a; }
.nav { position: sticky; top: 0; display: flex; flex-direction: column; gap: 2px; padding: 14px 0 24px 16px; }
.nav-item { display: flex; align-items: center; gap: 10px; padding: 9px 10px; border: 0; border-radius: 8px; background: transparent;
  text-align: left; cursor: pointer; font-size: 13px; color: var(--text-secondary); }
.nav-item.on { background: #eef3f1; color: var(--text); font-weight: 650; }
.code { flex: none; font-size: 11px; color: var(--text-faint); font-weight: 400; }
.nav-name { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.nav-state { flex: none; font-size: 11.5px; font-weight: 400; }
.tone-accent { color: var(--accent); }
.tone-warn { color: var(--warn-ink); }
.tone-danger { color: var(--danger); }
.detail { min-width: 0; padding: 16px 24px 0; }
.crit-head { display: flex; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
.crit-code { font-size: 11.5px; color: var(--text-faint); }
.crit-name { margin-top: 4px; font-size: 18px; font-weight: 700; }
.basis { margin-top: 16px; border: 1px solid var(--border-light); border-radius: var(--radius-md); overflow: hidden; }
.basis-toggle { width: 100%; display: flex; align-items: center; gap: 10px; padding: 10px 14px; border: 0; background: var(--surface-muted);
  cursor: pointer; text-align: left; font: inherit; }
.basis-toggle strong { flex: none; font-size: 13px; font-weight: 650; }
.basis-sum { flex: 1; min-width: 0; font-size: 12px; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.basis-act { flex: none; font-size: 12px; color: var(--accent); }
.basis-body { display: flex; flex-direction: column; gap: 14px; padding: 12px 14px 14px; border-top: 1px solid var(--border-light); }
.basis-label { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; font-size: 12px; font-weight: 650; color: var(--text-secondary); }
.basis-body p { margin: 0; font-size: 13px; line-height: 1.8; text-wrap: pretty; }
.basis-body .quote { color: var(--text-secondary); }
.rules { display: flex; flex-direction: column; gap: 8px; }
.empty { margin: 0; padding: 14px; border: 1px dashed var(--border); border-radius: var(--radius-md); font-size: 12.5px; color: var(--text-muted); }
.ai-note { margin: 10px 0 0; font-size: 12px; line-height: 1.7; color: var(--warn-ink); }
</style>
