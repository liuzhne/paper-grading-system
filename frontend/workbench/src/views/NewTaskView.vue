<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";

import { api, ApiError, StaleContextError } from "@/api/client.js";
import { useUploadStore } from "@/stores/upload.js";

const router = useRouter();
const upload = useUploadStore();

const form = reactive({ name: "", department: "", major: "", rubric_id: "" });
const rubrics = ref([]);
const batchId = ref(null);
const precheck = ref(null);
const error = ref(null);
const busy = ref(false);

/** 只有已发布的评分标准可用于评分——草稿列出但不可选。 */
const publishable = computed(() => rubrics.value.filter((r) => r.status === "published"));
const drafts = computed(() => rubrics.value.filter((r) => r.status !== "published"));

const canCreate = computed(() => form.name.trim() && form.rubric_id);
const canStart = computed(() => precheck.value?.can_start === true);

async function loadRubrics() {
  try {
    rubrics.value = (await api.get("/rubrics")) || [];
  } catch (err) {
    if (!(err instanceof StaleContextError)) rubrics.value = [];
  }
}

async function ensureBatch() {
  if (batchId.value) return batchId.value;
  const batch = await api.post("/batches", {
    name: form.name.trim(),
    rubric_id: form.rubric_id,
    department: form.department || null,
    major: form.major || null,
  });
  batchId.value = batch.id;
  return batch.id;
}

function onPick(event) {
  upload.stage(event.target.files);
  event.target.value = "";
}

async function onUpload() {
  busy.value = true;
  error.value = null;
  try {
    const id = await ensureBatch();
    await upload.uploadAll(id);
    await runPrecheck();
  } catch (err) {
    if (!(err instanceof StaleContextError)) {
      error.value = err instanceof ApiError ? err.detail || err.message : err?.message;
    }
  } finally {
    busy.value = false;
  }
}

async function runPrecheck() {
  if (!batchId.value || !upload.uploadedPaperIds.length) {
    precheck.value = null;
    return;
  }
  precheck.value = await api.post(`/batches/${batchId.value}/upload-precheck`, {
    paper_ids: upload.uploadedPaperIds,
  });
}

async function onSaveDraft() {
  busy.value = true;
  error.value = null;
  try {
    await ensureBatch();
    // 存草稿只保存批次与已归档文件，不创建评分 job。
    await router.push({ name: "tasks" });
  } catch (err) {
    error.value = err?.message || "保存草稿失败";
  } finally {
    busy.value = false;
  }
}

async function onStart() {
  busy.value = true;
  error.value = null;
  try {
    await api.post(`/batches/${batchId.value}/score`, {});
    await router.push({ name: "grade", params: { batchId: batchId.value } });
  } catch (err) {
    error.value = err instanceof ApiError ? err.detail || err.message : err?.message;
  } finally {
    busy.value = false;
  }
}

onMounted(async () => {
  await Promise.all([loadRubrics(), upload.loadCapabilities()]);
  upload.reset();
});
</script>

<template>
  <div>
    <RouterLink class="faint back" :to="{ name: 'tasks' }">← 返回评分任务</RouterLink>
    <header class="page-head">
      <h1 class="page-title">新建评分任务</h1>
      <p class="page-sub">上传后系统先做解析预检，再按所选标准版本执行评分。</p>
    </header>

    <p v-if="error" class="notice notice-danger" role="alert">{{ error }}</p>

    <div class="layout">
      <div class="main">
        <!-- 1 批次信息 -->
        <section class="card card-pad">
          <div class="step"><span class="step-num mono">1</span><h2 class="card-title">批次信息</h2></div>
          <div class="form-grid">
            <label class="field">
              <span class="field-label">批次名称</span>
              <input v-model="form.name" class="input" type="text" required />
            </label>
            <label class="field">
              <span class="field-label">归属单位</span>
              <input v-model="form.department" class="input" type="text" />
            </label>
            <label class="field">
              <span class="field-label">类别 / 专业</span>
              <input v-model="form.major" class="input" type="text" />
            </label>
          </div>
          <div class="field">
            <span class="field-label">评审方式</span>
            <div class="modes">
              <span class="mode active">单评</span>
              <span class="mode disabled" title="双评/仲裁为独立里程碑，首版未启用">双评取均</span>
              <span class="mode disabled" title="双评/仲裁为独立里程碑，首版未启用">双评 + 仲裁</span>
            </div>
            <span class="field-hint">首版仅支持单评；双评与仲裁作为独立里程碑另行设计。</span>
          </div>
        </section>

        <!-- 2 评分标准 -->
        <section class="card card-pad">
          <div class="step">
            <span class="step-num mono">2</span>
            <h2 class="card-title">评分标准</h2>
            <span class="faint step-note">只能选择已发布版本</span>
          </div>
          <label
            v-for="rubric in publishable"
            :key="rubric.id"
            class="rubric"
            :class="{ selected: form.rubric_id === rubric.id }"
          >
            <input v-model="form.rubric_id" type="radio" :value="rubric.id" />
            <span>
              <span class="rubric-name">{{ rubric.name }}</span>
              <span class="chip chip-ok rubric-chip">{{ rubric.version }} 已发布</span>
            </span>
          </label>
          <p v-if="!publishable.length" class="faint">当前组织还没有已发布的评分标准。</p>

          <div v-for="rubric in drafts" :key="rubric.id" class="rubric muted-rubric">
            <span class="rubric-name">{{ rubric.name }}</span>
            <span class="chip chip-warn rubric-chip">{{ rubric.version }} {{ rubric.status }}</span>
            <span class="faint rubric-note">未发布，不可用于评分</span>
          </div>
        </section>

        <!-- 3 上传材料 -->
        <section class="card card-pad">
          <div class="step">
            <span class="step-num mono">3</span>
            <h2 class="card-title">上传材料</h2>
            <span v-if="upload.queue.length" class="faint step-note mono">
              已选 {{ upload.queue.length }} 份
            </span>
          </div>

          <label class="drop">
            <input type="file" multiple :accept="upload.acceptedExtensions.join(',')" @change="onPick" />
            <span>选择文件（可多选）</span>
            <span class="faint drop-note">
              支持 {{ upload.acceptedExtensions.join("、") || "…" }}<template v-if="upload.maxSizeMb">，单份上限 {{ upload.maxSizeMb }} MB</template>
            </span>
          </label>

          <ul v-if="upload.queue.length" class="queue">
            <li v-for="entry in upload.queue" :key="entry.id" :class="entry.status">
              <span class="q-name">{{ entry.name }}</span>
              <span class="q-status faint mono">{{ entry.status }}</span>
              <span v-if="entry.error" class="q-error">{{ entry.error }}</span>
            </li>
          </ul>

          <div class="btn-row upload-actions">
            <button class="btn" type="button" :disabled="busy || !canCreate || !upload.queue.length" @click="onUpload">
              上传并解析
            </button>
            <button v-if="upload.failedCount" class="btn" type="button" :disabled="busy" @click="upload.retryFailed(batchId)">
              重试失败项（{{ upload.failedCount }}）
            </button>
          </div>

          <!-- 预检结果 -->
          <div v-if="precheck" class="precheck">
            <p v-if="precheck.blocking_count" class="notice notice-danger">
              {{ precheck.blocking_count }} 份材料存在阻断问题，处理后才能开始评分。
            </p>
            <p v-else-if="precheck.warning_count" class="notice notice-warn">
              {{ precheck.warning_count }} 份材料有提示项，可以开始评分。
            </p>
            <p v-else class="notice">全部 {{ precheck.ready_count }} 份材料解析正常。</p>

            <ul v-if="precheck.findings.length" class="findings">
              <li v-for="finding in precheck.findings" :key="finding.paper_id + finding.code">
                <span class="chip" :class="finding.severity === 'blocking' ? 'chip-danger' : 'chip-warn'">
                  {{ finding.severity === "blocking" ? "阻断" : "提示" }}
                </span>
                <span class="mono">{{ finding.student_id || finding.file_name }}</span>
                <span class="finding-msg">{{ finding.message }}</span>
              </li>
            </ul>
          </div>
        </section>
      </div>

      <aside class="card card-pad summary">
        <h2 class="card-title">任务摘要</h2>
        <dl class="summary-list">
          <div><dt>材料份数</dt><dd class="mono">{{ upload.uploadedPaperIds.length }}</dd></div>
          <div><dt>解析正常</dt><dd class="mono">{{ precheck?.ready_count ?? "—" }}</dd></div>
          <div><dt>阻断项</dt><dd class="mono">{{ precheck?.blocking_count ?? "—" }}</dd></div>
        </dl>
        <button class="btn btn-primary full" type="button" :disabled="busy || !canStart" @click="onStart">
          开始评分
        </button>
        <button class="btn full" type="button" :disabled="busy || !canCreate" @click="onSaveDraft">
          存为草稿
        </button>
        <p class="field-hint">
          存草稿只保存批次与已归档的文件，不创建评分任务；之后可继续上传或直接开始评分。
        </p>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.back {
  font-size: 13px;
}

.layout {
  display: flex;
  gap: 20px;
  align-items: flex-start;
  flex-wrap: wrap;
}

.main {
  flex: 999 1 440px;
  min-width: 0;
}

.summary {
  flex: 1 1 280px;
  position: sticky;
  top: 24px;
}

.step {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 18px;
}

.step-num {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--accent);
  color: #fff;
  font-size: 11px;
  display: grid;
  place-items: center;
  flex: none;
}

.step-note {
  margin-left: auto;
  font-size: 12px;
}

.modes {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.mode {
  padding: 7px 14px;
  border-radius: 7px;
  border: 1px solid var(--border-input);
  font-size: 13px;
  color: var(--text-secondary);
}

.mode.active {
  border-color: var(--accent);
  background: var(--accent-selected);
  color: var(--accent);
  font-weight: 600;
}

.mode.disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.rubric {
  display: flex;
  align-items: center;
  gap: 10px;
  border: 1px solid var(--border-input);
  border-radius: 9px;
  padding: 13px 17px;
  margin-bottom: 10px;
  cursor: pointer;
}

.rubric.selected {
  border-color: var(--accent);
  background: var(--accent-surface);
}

.muted-rubric {
  cursor: default;
  opacity: 0.7;
}

.rubric-name {
  font-size: 14px;
  font-weight: 550;
}

.rubric-chip {
  margin-left: 10px;
}

.rubric-note {
  margin-left: auto;
  font-size: 12px;
}

.drop {
  display: block;
  border: 1.5px dashed var(--border-input);
  border-radius: 10px;
  padding: 26px;
  text-align: center;
  background: var(--surface-muted);
  cursor: pointer;
  margin-bottom: 16px;
}

.drop input {
  display: block;
  margin: 0 auto 10px;
}

.drop-note {
  display: block;
  font-size: 12px;
  margin-top: 6px;
}

.queue {
  list-style: none;
  margin: 0 0 14px;
  padding: 0;
  font-size: 12.5px;
}

.queue li {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 7px 0;
  border-bottom: 1px solid var(--border-row);
  flex-wrap: wrap;
}

.queue li.failed .q-name,
.queue li.rejected .q-name {
  color: var(--danger);
}

.q-name {
  flex: 1 1 auto;
  min-width: 0;
}

.q-error {
  flex-basis: 100%;
  color: var(--danger);
  font-size: 12px;
}

.upload-actions {
  margin-bottom: 4px;
}

.precheck {
  margin-top: 16px;
}

.findings {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.findings li {
  display: flex;
  align-items: baseline;
  gap: 9px;
  font-size: 12.5px;
  flex-wrap: wrap;
}

.finding-msg {
  flex: 1 1 240px;
  color: var(--text-secondary);
  line-height: 1.7;
}

.summary-list {
  margin: 16px 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  font-size: 12.5px;
}

.summary-list > div {
  display: flex;
  justify-content: space-between;
}

.summary-list dt {
  color: var(--text-muted);
}

.summary-list dd {
  margin: 0;
}

.full {
  width: 100%;
  margin-bottom: 10px;
}
</style>
