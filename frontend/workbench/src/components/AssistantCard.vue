<script setup>
/**
 * 评分助手的卡片。卡片只存类型与编号，分数、扣分点与证据渲染时现取。
 *
 * 按钮不直接调业务接口：等待中的卡片通过 `resume` 通知后端的流程图继续，选择卡片
 * 通过 `select` 发起一次查询或确认；写动作由后端经共享守卫执行（方案 T4、T14）。
 */
import { computed, onMounted, ref, watch } from "vue";

import { api, StaleContextError } from "@/api/client.js";
import { MAX_FILES, PLAN_STATUS_TEXT, PLAN_STEP_MARKS, papersInUploadOrder, publishedRubrics, scoreOverview } from "@/lib/assistant-flow.js";
import { DOCUMENT_TYPES, documentTypeLabel, rememberedDocumentType } from "@/lib/document-types.js";
import { finishedCount, jobPercent, jobStatusLabel } from "@/lib/score-jobs.js";
import { deductionReasons } from "@/lib/score-items.js";
import { useAssistantStore } from "@/stores/assistant.js";
import { useUploadStore } from "@/stores/upload.js";

const props = defineProps({
  message: { type: Object, required: true },
  card: { type: Object, required: true },
});

const store = useAssistantStore();
const upload = useUploadStore();
const loadError = ref(null);

const waiting = computed(() => store.isPending(props.card));
const proposed = computed(() => props.card.status === "proposed");
const disabled = computed(() => store.sending || store.uploading);

const STATUS_TEXT = { running: "进行中…", done: "已完成", failed: "未完成", dismissed: "已放弃", expired: "已过期" };
const statusText = computed(() => STATUS_TEXT[props.card.status] || "");
// 打断确认卡片：done 是放弃了上一步，dismissed 是选了继续上一步。
const SWITCH_STATUS_TEXT = { done: "已放弃上一步", dismissed: "已继续上一步", expired: "已过期" };

function filesFrom(event) {
  const files = Array.from(event.target.files || []);
  event.target.value = "";
  return files;
}

// --- 选文件 / 开始评分 -------------------------------------------------------------
function onPickFiles(event) {
  const files = filesFrom(event);
  if (files.length) store.pickFiles(props.card, files);
}
function onReplaceFiles(event) {
  const files = filesFrom(event);
  if (files.length) store.replaceFiles(files);
}
const filesReady = computed(() => Boolean(store.filesForPending()));
const uploadingHere = computed(() => store.pending?.kind === "upload_papers" && store.pending?.card_id === props.card.id);
const uploadProgress = computed(() => {
  const total = upload.queue.length;
  if (!total) return "";
  const done = upload.queue.filter((entry) => ["done", "failed", "rejected"].includes(entry.status)).length;
  return `上传与解析 ${done}/${total}`;
});
const rubricChoices = ref(null);
async function showRubricChoices() {
  try {
    rubricChoices.value = publishedRubrics((await api.get("/rubrics")) || []);
  } catch (err) {
    if (!(err instanceof StaleContextError)) loadError.value = "无法加载评分标准列表";
  }
}

// --- 评分标准导入 ----------------------------------------------------------------
const rubricForm = ref({ name: "", businessProfileKey: rememberedDocumentType(), rulesFile: null, templateFile: null });
const rubricFormError = ref(null);
function submitRubricFiles() {
  rubricFormError.value = null;
  const form = rubricForm.value;
  if (!form.name.trim()) { rubricFormError.value = "请填写评分标准名称。"; return; }
  if (!form.rulesFile && !form.templateFile) { rubricFormError.value = "请至少选择一份评分规则或评分模板文件。"; return; }
  store.importRubric(props.card, { ...form, name: form.name.trim() });
}
const rubricWaitPath = computed(() => {
  // 导入草稿确认后就成了一份草稿标准，再打开草稿已无意义，改为定位到这份标准。
  const rubricId = store.conversation?.focus?.rubric_id;
  return rubricId ? `/rubrics?rubric=${rubricId}` : `/rubrics?import_session=${props.card.import_session_id}`;
});

// --- 实时数据：作业、汇总、评分项 ---------------------------------------------------
const job = computed(() => store.jobs[props.card.batch_id] || null);
const summary = ref(null);
const items = ref(null);

async function loadSummary() {
  try {
    summary.value = await api.get(`/batches/${props.card.batch_id}/summary`);
  } catch (err) {
    if (!(err instanceof StaleContextError)) loadError.value = "无法加载任务结果";
  }
}
async function loadItems() {
  try {
    items.value = await api.get(`/scoring-runs/${props.card.run_id}/items?include_view=true`);
  } catch (err) {
    if (!(err instanceof StaleContextError)) loadError.value = "无法加载评分项";
  }
}
async function loadJob() {
  try {
    store.jobs[props.card.batch_id] = await api.get(`/batches/${props.card.batch_id}/score-jobs/latest`);
  } catch (err) {
    if (!(err instanceof StaleContextError)) loadError.value = "无法加载评分进度";
  }
}
onMounted(() => {
  if (["overview", "review_list"].includes(props.card.type)) loadSummary();
  if (props.card.type === "paper_result") loadItems();
  if (props.card.type === "job_progress" && !store.jobs[props.card.batch_id]) loadJob();
});
watch(() => job.value?.status, (status, previous) => {
  if (previous && status !== previous && ["overview", "review_list"].includes(props.card.type)) loadSummary();
});

const overview = computed(() => scoreOverview(summary.value?.papers || []));
const flagged = computed(() => papersInUploadOrder(summary.value?.papers || [])
  .map((paper, index) => ({ ...paper, ordinal: index + 1 }))
  .filter((paper) => paper.latest_need_manual_review));

function evidenceLabel(view) {
  const parts = [];
  if (view.page_start) parts.push(`第 ${view.page_start} 页`);
  if (view.section_title) parts.push(view.section_title);
  return parts.join(" · ") || "原文";
}
</script>

<template>
  <div class="acard" :class="[`acard-${card.type}`, `is-${card.status || 'info'}`]" :data-card-id="card.id">
    <!-- 计划：这次要走的步骤，每走一步后端在原卡片上更新（计划卡片方案） -->
    <template v-if="card.type === 'plan'">
      <div class="plan-head">
        <strong>{{ card.flow === "import" ? "导入并评分的步骤" : "评分的步骤" }}</strong>
        <span class="faint small">{{ PLAN_STATUS_TEXT[card.status] || "" }}</span>
      </div>
      <ol class="plan-steps">
        <li v-for="(step, index) in card.steps" :key="step.key" :class="`step-${step.status}`">
          <span class="mark" aria-hidden="true">{{ PLAN_STEP_MARKS[step.status]?.mark || "○" }}</span>
          <span class="step-body">
            <span class="step-label">{{ index + 1 }}. {{ step.label }}</span>
            <span class="sr-only">（{{ PLAN_STEP_MARKS[step.status]?.text || step.status }}）</span>
            <span v-if="step.detail" class="faint small step-detail">{{ step.detail }}</span>
          </span>
        </li>
      </ol>
    </template>

    <!-- 快捷按钮 -->
    <div v-else-if="card.type === 'quick_actions'" class="chips">
      <button v-for="action in card.actions" :key="action.label" class="btn btn-sm" type="button"
              :disabled="disabled" @click="store.send(action.text, action.intent)">{{ action.label }}</button>
    </div>

    <!-- 深链：在右侧工作区打开 -->
    <button v-else-if="card.type === 'link'" class="btn btn-sm" type="button" @click="store.showWorkspace(card.path)">
      {{ card.label }} →
    </button>

    <!-- 选择待评分文件 -->
    <template v-else-if="card.type === 'pick_files'">
      <label v-if="waiting" class="file-pick">
        <input type="file" multiple accept=".docx,.pdf" :disabled="disabled" @change="onPickFiles" />
        <span class="btn btn-primary btn-sm">选择文件</span>
        <span class="faint">最多 {{ MAX_FILES }} 份</span>
      </label>
      <p v-else-if="card.result" class="faint">已选择 {{ card.result.count }} 份</p>
      <p v-else class="faint">{{ statusText }}</p>
    </template>

    <!-- 关键节点：开始评分 -->
    <template v-else-if="card.type === 'confirm_start'">
      <dl class="facts">
        <div><dt>评分标准</dt><dd>{{ card.rubric_name }} <span class="mono faint">{{ card.rubric_version }}</span></dd></div>
        <div><dt>文档类型</dt><dd>{{ documentTypeLabel(card.business_profile_key) }}</dd></div>
        <div><dt>文件</dt><dd class="mono">{{ card.file_count }} 份</dd></div>
        <div><dt>评分模型</dt><dd>{{ card.connection_label }}</dd></div>
      </dl>
      <template v-if="waiting && proposed">
        <div v-if="filesReady" class="btn-row">
          <button class="btn btn-primary" type="button" :disabled="disabled" @click="store.resume(card.id, { action: 'confirm' })">开始评分</button>
          <button class="btn" type="button" :disabled="disabled" @click="showRubricChoices">换评分标准</button>
          <button class="btn" type="button" :disabled="disabled" @click="store.resume(card.id, { action: 'cancel' })">不评了</button>
        </div>
        <div v-else class="notice notice-warn">
          页面刷新后，内存里的文件已失效。
          <button class="btn btn-sm" type="button" :disabled="disabled" @click="store.resume(card.id, { action: 'repick' })">重新选择文件</button>
        </div>
        <div v-if="rubricChoices" class="choices">
          <p v-if="!rubricChoices.length" class="faint">没有其它已发布的评分标准。</p>
          <button v-for="rubric in rubricChoices" :key="rubric.id" class="choice" type="button"
                  :disabled="disabled || rubric.id === card.rubric_id"
                  @click="store.resume(card.id, { action: 'change_rubric', rubric_id: rubric.id })">
            {{ rubric.name }} <span class="mono faint">{{ rubric.version }}</span>
            <span class="faint">· {{ documentTypeLabel(rubric.business_profile_key) }}</span>
          </button>
        </div>
      </template>
      <template v-else-if="uploadingHere">
        <p v-if="filesReady" class="faint">正在上传与解析… <span class="mono">{{ uploadProgress }}</span></p>
        <label v-else class="file-pick">
          <input type="file" multiple accept=".docx,.pdf" :disabled="disabled" @change="onReplaceFiles" />
          <span class="btn btn-primary btn-sm">重新选择这批文件</span>
          <span class="faint">页面刷新后文件已失效，任务已建好，选好后接着上传</span>
        </label>
      </template>
      <p v-else class="faint">{{ statusText }}<template v-if="card.result?.error">：{{ card.result.error }}</template></p>
    </template>

    <!-- 无法解析的文件：移除并继续 -->
    <template v-else-if="card.type === 'blocked_files'">
      <div v-if="waiting && proposed" class="btn-row">
        <button class="btn btn-primary" type="button" :disabled="disabled" @click="store.resume(card.id, { action: 'remove' })">移除并评其余文件</button>
        <button class="btn" type="button" :disabled="disabled" @click="store.resume(card.id, { action: 'cancel' })">先不评</button>
      </div>
      <p v-else class="faint">{{ statusText }}</p>
    </template>

    <!-- 评分进度（实时） -->
    <template v-else-if="card.type === 'job_progress'">
      <template v-if="job">
        <div class="progress-head">
          <span class="chip">{{ jobStatusLabel(job) }}</span>
          <span class="mono">{{ finishedCount(job) }}/{{ job.total_items }}</span>
        </div>
        <div class="bar"><span class="fill" :style="{ width: `${jobPercent(job)}%` }"></span></div>
        <p class="faint mono small">完成 {{ job.succeeded_count + job.skipped_count }} · 失败 {{ job.failed_count }} · 评分中 {{ job.running_count }}</p>
      </template>
      <button class="btn btn-sm" type="button" @click="store.showWorkspace(`/tasks/${card.batch_id}/run`)">在右侧查看进度</button>
    </template>

    <!-- 失败诊断 -->
    <template v-else-if="card.type === 'diagnosis'">
      <ul class="groups">
        <li v-for="group in card.groups" :key="group.code">
          <div class="group-head"><strong>{{ group.label }}</strong> <span class="mono faint">{{ group.count }} 份 · {{ group.code }}</span></div>
          <p class="advice">{{ group.advice }}</p>
          <p class="faint small">{{ group.file_names.join("、") }}<template v-if="group.count > group.file_names.length"> 等</template></p>
        </li>
      </ul>
      <div class="btn-row">
        <button v-if="proposed" class="btn btn-primary btn-sm" type="button" :disabled="disabled"
                @click="store.select(card.id, { action: 'retry' })">重试失败项</button>
        <button class="btn btn-sm" type="button" @click="store.showWorkspace('/account')">账户与连接</button>
      </div>
    </template>

    <!-- 分数概览 -->
    <template v-else-if="card.type === 'overview'">
      <div v-if="summary" class="stats">
        <div><span class="num mono">{{ overview.scored }}/{{ overview.total }}</span><span>已出分</span></div>
        <div><span class="num mono">{{ overview.average ?? "—" }}</span><span>平均分</span></div>
        <div><span class="num mono">{{ overview.max ?? "—" }}</span><span>最高</span></div>
        <div><span class="num mono">{{ overview.min ?? "—" }}</span><span>最低</span></div>
        <div><span class="num mono">{{ overview.needReview }}</span><span>需复核</span></div>
      </div>
      <button class="btn btn-sm" type="button" @click="store.showWorkspace(`/batches/${card.batch_id}/grade`)">在右侧查看每一份</button>
    </template>

    <!-- 需要复核的材料 -->
    <template v-else-if="card.type === 'review_list'">
      <ul v-if="summary" class="plain-list">
        <li v-for="paper in flagged" :key="paper.paper_id">
          <button class="choice" type="button" :disabled="disabled" @click="store.send(`第${paper.ordinal}篇为什么扣分`)">
            {{ paper.ordinal }}. {{ paper.student_name || paper.file_name }}
            <span class="mono faint">{{ paper.latest_final_score ?? "未出总分" }}</span>
          </button>
        </li>
      </ul>
    </template>

    <!-- 选择任务 / 选择材料 -->
    <template v-else-if="card.type === 'choose_batch' || card.type === 'choose_paper'">
      <div v-if="proposed" class="choices">
        <template v-if="card.type === 'choose_batch'">
          <button v-for="batch in card.batches" :key="batch.id" class="choice" type="button" :disabled="disabled"
                  @click="store.select(card.id, { batch_id: batch.id })">{{ batch.name }}</button>
        </template>
        <template v-else>
          <button v-for="paper in card.papers" :key="paper.paper_id" class="choice" type="button" :disabled="disabled"
                  @click="store.select(card.id, { paper_id: paper.paper_id })">{{ paper.label }}</button>
        </template>
      </div>
      <p v-else class="faint">{{ statusText }}</p>
    </template>

    <!-- 某一篇的得分、扣分点与证据 -->
    <template v-else-if="card.type === 'paper_result'">
      <div v-if="items" class="items">
        <div v-for="item in items" :key="item.id" class="item" :class="{ flagged: item.need_manual_review }">
          <div class="item-top">
            <span class="item-name">{{ item.criterion_name || item.criterion_code }}</span>
            <span class="mono">{{ item.final_score ?? item.ai_score ?? "—" }}<span class="faint"> / {{ item.max_score }}</span></span>
          </div>
          <span v-if="item.need_manual_review" class="chip chip-warn">需复核</span>
          <ul v-if="deductionReasons(item).length" class="deductions">
            <li v-for="(line, index) in deductionReasons(item)" :key="index">{{ line }}</li>
          </ul>
          <p v-else class="faint small">{{ item.reason }}</p>
          <p v-if="item.revision_suggestion" class="small suggestion" data-test="card-suggestion">
            <span class="faint">修改建议：</span>{{ item.revision_suggestion }}
          </p>
          <details v-if="item.evidence_view?.length" class="evidence">
            <summary class="faint small">证据 {{ item.evidence_view.length }} 条</summary>
            <blockquote v-for="(view, index) in item.evidence_view" :key="index">
              <span class="faint small">{{ evidenceLabel(view) }}</span>
              <span>{{ view.quote || "（无引文）" }}</span>
            </blockquote>
          </details>
        </div>
      </div>
      <button class="btn btn-sm" type="button" @click="store.showWorkspace(`/batches/${card.batch_id}/grade?paper=${card.paper_id}`)">在右侧对照原文</button>
    </template>

    <!-- 重试 / 取消 -->
    <template v-else-if="card.type === 'confirm_retry' || card.type === 'confirm_cancel'">
      <div v-if="proposed" class="btn-row">
        <button class="btn btn-sm" :class="card.type === 'confirm_cancel' ? 'btn-danger' : 'btn-primary'" type="button" :disabled="disabled"
                @click="store.select(card.id, { action: 'confirm' })">
          {{ card.type === "confirm_cancel" ? "取消剩余评分" : "重试" }}
        </button>
        <button class="btn btn-sm" type="button" :disabled="disabled" @click="store.select(card.id, { action: 'cancel' })">算了</button>
      </div>
      <p v-else class="faint">{{ statusText }}</p>
    </template>

    <!-- 打断保护：上一步还在等老师回应时，先问要不要放弃 -->
    <template v-else-if="card.type === 'confirm_switch_flow'">
      <div v-if="proposed" class="btn-row">
        <button class="btn btn-sm btn-primary" type="button" :disabled="disabled"
                @click="store.switchFlow(card, 'switch')">放弃并{{ card.to_label }}</button>
        <button class="btn btn-sm" type="button" :disabled="disabled"
                @click="store.switchFlow(card, 'keep')">继续“{{ card.from_label }}”</button>
      </div>
      <p v-else class="faint">{{ SWITCH_STATUS_TEXT[card.status] || statusText }}</p>
    </template>

    <!-- 上传评分规则与模板 -->
    <template v-else-if="card.type === 'pick_rubric_files'">
      <form v-if="waiting" class="rubric-form" @submit.prevent="submitRubricFiles">
        <label class="field"><span class="field-label">评分标准名称</span>
          <input v-model="rubricForm.name" class="input" maxlength="100" placeholder="例如：2026 年采购方案评审标准" /></label>
        <div class="field" role="radiogroup" aria-label="文档类型"><span class="field-label">文档类型（上传后不能再改）</span>
          <div class="btn-row">
            <label v-for="option in DOCUMENT_TYPES" :key="option.key" class="type-choice">
              <input v-model="rubricForm.businessProfileKey" type="radio" name="assistant-document-type" :value="option.key" />
              {{ option.label }}<span class="faint">（{{ option.hint }}）</span>
            </label>
          </div>
        </div>
        <label class="field"><span class="field-label">评分规则文件</span>
          <input type="file" accept=".xlsx,.xls,.docx" @change="rubricForm.rulesFile = $event.target.files?.[0] || null" /></label>
        <label class="field"><span class="field-label">评分模板文件</span>
          <input type="file" accept=".docx,.xlsx" @change="rubricForm.templateFile = $event.target.files?.[0] || null" /></label>
        <p v-if="rubricFormError" class="notice notice-danger">{{ rubricFormError }}</p>
        <div class="btn-row">
          <button class="btn btn-primary btn-sm" type="submit" :disabled="disabled">导入</button>
          <button class="btn btn-sm" type="button" :disabled="disabled" @click="store.resume(card.id, { action: 'cancel' })">不导入了</button>
        </div>
      </form>
      <p v-else class="faint">{{ statusText }}</p>
    </template>

    <!-- 等待用户在工作区核对并发布 -->
    <template v-else-if="card.type === 'rubric_wait'">
      <p class="faint">{{ waiting ? "等待你在右侧核对并发布…" : statusText }}</p>
      <button v-if="waiting" class="btn btn-sm" type="button" @click="store.showWorkspace(rubricWaitPath)">在右侧打开</button>
    </template>

    <p v-if="loadError" class="notice notice-danger">{{ loadError }}</p>
  </div>
</template>

<style scoped>
.acard { margin-top: 10px; padding: 12px 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
.acard-quick_actions, .acard-link { padding: 0; border: 0; background: none; }
.is-dismissed, .is-expired { opacity: 0.6; }
.chips, .choices { display: flex; flex-wrap: wrap; gap: 8px; }
.choice { padding: 6px 10px; border: 1px solid var(--border-input); border-radius: var(--radius-sm); background: var(--surface-muted); cursor: pointer; text-align: left; font: inherit; }
.choice:hover:not(:disabled) { border-color: var(--accent); }
.choice:disabled { opacity: 0.55; cursor: default; }
.choices { margin-top: 10px; }
.file-pick { display: inline-flex; align-items: center; gap: 10px; cursor: pointer; }
.file-pick input { position: absolute; width: 1px; height: 1px; opacity: 0; }
.facts { display: grid; gap: 6px; margin: 0 0 12px; }
.facts div { display: flex; gap: 12px; }
.facts dt { width: 72px; flex: none; color: var(--text-muted); }
.facts dd { margin: 0; }
.progress-head, .item-top, .group-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.bar { height: 6px; margin: 10px 0 6px; border-radius: 3px; background: var(--surface-sunken); overflow: hidden; }
.fill { display: block; height: 100%; background: var(--accent); transition: width 0.3s; }
.small { font-size: 12px; }
.groups, .plain-list, .deductions { margin: 0 0 10px; padding: 0; list-style: none; }
.groups li { padding: 8px 0; border-bottom: 1px solid var(--border-row); }
.advice { margin: 4px 0; }
.stats { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; margin-bottom: 10px; }
.stats > div { padding: 8px; border-radius: var(--radius-sm); background: var(--surface-muted); }
.stats .num { display: block; font-size: 17px; }
.stats span:last-child { color: var(--text-muted); font-size: 12px; }
.plain-list li + li { margin-top: 6px; }
.items { display: grid; gap: 10px; margin-bottom: 10px; }
.item { padding: 10px; border-radius: var(--radius-sm); background: var(--surface-muted); }
.item.flagged { background: var(--warn-surface); border: 1px solid var(--warn-border); }
.item-name { font-weight: 600; }
.deductions { margin: 6px 0 0; padding-left: 16px; list-style: disc; }
.deductions li { margin: 2px 0; color: var(--danger-ink); }
.suggestion { margin: 4px 0 0; }
.evidence { margin-top: 6px; }
.evidence blockquote { display: grid; gap: 2px; margin: 6px 0 0; padding: 6px 10px; border-left: 3px solid var(--accent-border); background: var(--surface); }
.rubric-form { display: grid; gap: 10px; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
@media (max-width: 760px) { .stats { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
.type-choice { display: inline-flex; gap: 4px; align-items: center; font-size: 13px; }
.plan-head { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; margin-bottom: 8px; }
.plan-steps { display: grid; gap: 6px; margin: 0; padding: 0; list-style: none; }
.plan-steps li { display: flex; gap: 8px; align-items: flex-start; }
.plan-steps .mark { width: 16px; flex: none; text-align: center; color: var(--text-muted); }
.step-body { display: flex; flex-wrap: wrap; gap: 2px 8px; min-width: 0; }
.step-done .mark { color: var(--accent); }
.step-current .step-label { font-weight: 600; }
.step-current .mark { color: var(--accent); }
.step-blocked .mark, .step-blocked .step-label { color: var(--warn-ink); }
.step-failed .mark, .step-failed .step-label { color: var(--danger-ink); }
.step-todo .step-label, .step-skipped .step-label { color: var(--text-muted); }
.step-skipped .step-label { text-decoration: line-through; }
</style>
