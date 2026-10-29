<script setup>
/**
 * 导入评分标准（导入流程第 1 步）：等上传时是导入表单（与评分标准页同一个动作），之后是来源文件的解析结果。
 */
import { computed, onMounted, ref, watch } from "vue";

import { api, ApiError, StaleContextError } from "@/api/client.js";
import { DOCUMENT_TYPES, rememberedDocumentType } from "@/lib/document-types.js";
import { fileBadge } from "@/lib/assistant-workspace.js";
import { useAssistantStore } from "@/stores/assistant.js";

import WsLayout from "./WsLayout.vue";

const props = defineProps({
  context: { type: Object, required: true },
  head: { type: Object, required: true },
});

const store = useAssistantStore();
const session = ref(null);
const loadError = ref(null);
const form = ref({ name: "", businessProfileKey: rememberedDocumentType(), rulesFile: null, templateFile: null });
const formError = ref(null);

const waiting = computed(() => store.pending?.kind === "create_import_session");
const card = computed(() => props.context.cards.pick_rubric_files || null);
const sessionId = computed(() => props.context.importSessionId || null);
const disabled = computed(() => store.sending || store.uploading);

async function load() {
  if (!sessionId.value) return;
  try {
    session.value = await api.get(`/rubrics/import-sessions/${sessionId.value}`);
  } catch (err) {
    if (err instanceof StaleContextError) return;
    loadError.value = err instanceof ApiError && err.status === 410 ? "导入草稿已过期，解析结果不再保留。" : "无法加载导入结果";
  }
}
onMounted(load);
watch(sessionId, load);

function submit() {
  formError.value = null;
  const input = form.value;
  if (!input.name.trim()) { formError.value = "请填写评分标准名称。"; return; }
  if (!input.rulesFile && !input.templateFile) { formError.value = "请至少选择一份评分规则或评分模板文件。"; return; }
  store.noteTaskEdit();
  store.importRubric(card.value || { id: store.pending.card_id }, { ...input, name: input.name.trim() });
}

const sources = computed(() => {
  const value = session.value;
  if (!value) return [];
  const criteria = value.criteria || [];
  const total = criteria.reduce((sum, item) => sum + Number(item.max_score || 0), 0);
  const list = [];
  if (value.files?.template) {
    list.push({ name: value.files.template, role: "评分模板", result: `识别 ${criteria.length} 个评分项，合计 ${total} 分` });
  }
  if (value.files?.rules) {
    const doc = (value.coverage?.documents || []).find((item) => item.doc_role === "rules");
    const detail = doc?.total ? `解析 ${doc.total} 段，其中 ${doc.handled ?? 0} 段已处理，作为扣分细则的原文依据` : "作为扣分细则的原文依据";
    list.push({ name: value.files.rules, role: "评分规则", result: value.files?.template ? detail : `识别 ${criteria.length} 个评分项，合计 ${total} 分；${detail}` });
  }
  return list.map((item) => ({ ...item, badge: fileBadge(item.name) }));
});
const criteria = computed(() => session.value?.criteria || []);
</script>

<template>
  <WsLayout v-bind="head">
    <div class="body">
      <form v-if="waiting" class="import-form" @submit.prevent="submit">
        <label class="field"><span class="field-label">评分标准名称</span>
          <input v-model="form.name" class="input" maxlength="100" placeholder="例如：2026 年论文评分标准" /></label>
        <div class="field" role="radiogroup" aria-label="文档类型"><span class="field-label">文档类型（上传后不能再改）</span>
          <div class="btn-row">
            <label v-for="option in DOCUMENT_TYPES" :key="option.key" class="type-choice">
              <input v-model="form.businessProfileKey" type="radio" name="assistant-document-type" :value="option.key" />
              {{ option.label }}<span class="faint">（{{ option.hint }}）</span>
            </label>
          </div>
        </div>
        <label class="field"><span class="field-label">评分规则文件（Word 或 Excel）</span>
          <input type="file" accept=".xlsx,.xls,.docx" @change="form.rulesFile = $event.target.files?.[0] || null" /></label>
        <label class="field"><span class="field-label">评分模板文件（评分表）</span>
          <input type="file" accept=".docx,.xlsx" @change="form.templateFile = $event.target.files?.[0] || null" /></label>
        <p v-if="formError" class="notice notice-danger">{{ formError }}</p>
      </form>

      <template v-else>
        <div class="sources">
          <div v-for="item in sources" :key="item.name" class="source">
            <span class="ext" :class="`ext-${item.badge.tone}`">{{ item.badge.ext }}</span>
            <div class="row-main">
              <div class="source-head"><strong>{{ item.name }}</strong><span class="pill">{{ item.role }}</span></div>
              <p class="result">{{ item.result }}</p>
            </div>
          </div>
        </div>
        <template v-if="criteria.length">
          <div class="sec-head"><strong>识别出的评分项</strong><span>{{ criteria.length }} 项 · 合计 {{ session.total_score }} 分</span></div>
          <div class="row-list">
            <div v-for="(item, index) in criteria" :key="item.code || index" class="row">
              <span class="row-idx">{{ item.code }}</span>
              <span class="row-main">{{ item.name }}</span>
              <span class="mono max">{{ Number(item.max_score) }} 分</span>
            </div>
          </div>
        </template>
        <p v-if="loadError" class="notice notice-warn">{{ loadError }}</p>
      </template>
    </div>

    <template #foot>
      <template v-if="waiting">
        <p class="foot-text">导入后在下一步逐项核对扣分细则，全部确认后发布。</p>
        <button class="btn" type="button" :disabled="disabled" @click="store.resume(store.pending.card_id, { action: 'cancel' })">不导入了</button>
        <button class="btn btn-primary" type="button" :disabled="disabled" @click="submit">导入</button>
      </template>
      <p v-else class="foot-text">评分模板决定评分项和分值，评分规则文件提供扣分细则的原文依据。</p>
    </template>
  </WsLayout>
</template>

<style scoped>
.body { padding: 14px 24px 0; }
.import-form { display: grid; gap: 14px; }
.type-choice { display: inline-flex; gap: 4px; align-items: center; font-size: 13px; }
.sources { display: flex; flex-direction: column; gap: 8px; }
.source { display: flex; gap: 12px; align-items: flex-start; padding: 12px 14px; border: 1px solid var(--border-light); border-radius: var(--radius-md); }
.source .ext { width: 36px; height: 36px; border-radius: 8px; }
.source-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 13.5px; }
.pill { font-size: 11.5px; padding: 1px 8px; }
.result { margin: 5px 0 0; font-size: 12.5px; line-height: 1.7; color: var(--neutral-ink); }
.max { flex: none; font-size: 12.5px; color: var(--text-secondary); }
</style>
