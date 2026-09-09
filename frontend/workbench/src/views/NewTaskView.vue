<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import { api, ApiError, StaleContextError } from "@/api/client.js";
import { requiresOwnConnection, useUploadStore } from "@/stores/upload.js";
import { useSessionStore } from "@/stores/session.js";

const router = useRouter();
const route = useRoute();
const upload = useUploadStore();

const form = reactive({ name: "", department: "", major: "", rubric_id: "", ai_connection_id: "" });
const rubrics = ref([]);
const batchId = ref(null);
const precheck = ref(null);
const error = ref(null);
const busy = ref(false);

/** 只有已发布的评分标准可用于评分——草稿列出但不可选。 */
const publishable = computed(() => rubrics.value.filter((r) => r.status === "published"));
const drafts = computed(() => rubrics.value.filter((r) => r.status !== "published"));

const canCreate = computed(() => form.name.trim() && form.rubric_id);
// `connectionMissing` 定义在下方（与连接加载放在一起），这里只做组合。
const canStart = computed(
  () => precheck.value?.can_start === true && !connectionMissing.value,
);

/** 区间而不是精确值：一个精确数字会被当成承诺。 */
const etaText = computed(() => {
  const estimate = precheck.value?.duration_estimate;
  if (!estimate?.available) return "";
  const minutes = (seconds) => Math.max(1, Math.round(seconds / 60));
  const low = minutes(estimate.low_seconds);
  const high = minutes(estimate.high_seconds);
  return low === high ? `${low} 分钟` : `${low}–${high} 分钟`;
});

async function loadRubrics() {
  try {
    rubrics.value = (await api.get("/rubrics")) || [];
  } catch (err) {
    if (!(err instanceof StaleContextError)) rubrics.value = [];
  }
}

// --- AI 连接（D-027）-------------------------------------------------------
//
// 平台没配默认模型时**必选**：不绑连接就评分，评出来的是 Mock 假分，而假结果会
// 被当成真结论沿用下去。平台配好后不强制——那正是「所有用户可正常使用」的含义。
const session = useSessionStore();
const connections = ref([]);
const connectionRequired = computed(() =>
  requiresOwnConnection(session.capabilities?.llm),
);
const connectionMissing = computed(
  () => connectionRequired.value && !form.ai_connection_id,
);

async function loadConnections() {
  try {
    connections.value = (await api.get("/ai-connections")) || [];
  } catch (err) {
    if (!(err instanceof StaleContextError)) connections.value = [];
  }
}

async function ensureBatch() {
  if (batchId.value) return batchId.value;
  const batch = await api.post("/batches", {
    name: form.name.trim(),
    rubric_id: form.rubric_id,
    department: form.department || null,
    major: form.major || null,
    // 建批次时冻结连接：之后轮换密钥或改配置，旧批次会拒绝继续跑，而不是
    // 悄悄换一个模型接着评。
    ai_connection_id: form.ai_connection_id || null,
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
  // 先清干净再加载。反过来的话，用户在这两个请求返回之前选的文件会被这次
  // reset 静默清掉——界面上文件凭空消失，没有任何解释。
  upload.reset();
  await Promise.all([loadRubrics(), upload.loadCapabilities(), loadConnections()]);

  // 从草稿继续：刷新会丢掉内存里的队列，但材料已经在服务端归档了。不恢复就会
  // 让用户重新选一遍并重传，产生重复对象（计划 §5-E「已归档的文件不重传」）。
  const resume = route.query.batch;
  if (!resume) return;
  batchId.value = String(resume);
  try {
    await upload.restoreFromServer(batchId.value);
    if (upload.uploadedPaperIds.length) await runPrecheck();
  } catch (err) {
    if (!(err instanceof StaleContextError)) {
      error.value = "无法恢复该草稿的材料列表：" + (err?.message || "请刷新重试");
    }
  }
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
            <input
              v-model="form.rubric_id"
              class="rubric-radio"
              type="radio"
              :value="rubric.id"
            />
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

        <!-- 2.5 AI 连接 -->
        <section v-if="connectionRequired" class="card card-pad">
          <div class="step">
            <span class="step-num mono">·</span>
            <h2 class="card-title">AI 连接</h2>
            <span class="faint step-note">本部署未配置平台默认模型，必须选择</span>
          </div>
          <label class="field">
            <span class="field-label">用哪个连接评分</span>
            <select v-model="form.ai_connection_id" class="select">
              <option value="">请选择</option>
              <option v-for="item in connections" :key="item.id" :value="item.id">
                {{ item.name }} · {{ item.provider_type }} · {{ item.model_name }}
              </option>
            </select>
          </label>
          <p v-if="!connections.length" class="notice notice-warn">
            你还没有可用的 AI 连接。请先在
            <RouterLink :to="{ name: 'account' }">账户与连接</RouterLink>
            绑定一个，否则无法开始评分。
          </p>
          <p class="faint">
            建任务时会冻结这个连接：之后轮换密钥或改配置，本任务会拒绝继续跑，
            而不是悄悄换一个模型接着评。
          </p>
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
            <!-- 取消的是「还没开始的那些」；已经发出去的那一个仍会走完并记下
                 结果——文件在服务端已经归档，谎称没传成功只会让用户再传一次。 -->
            <button v-if="busy" class="btn" type="button" @click="upload.cancel()">
              取消剩余上传
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

            <!-- 预计耗时（决策 12）。样本不足时如实说「暂无估计」——编一个分钟数
                 会被当成承诺，而 §5-D 明写它不参与租约与超时判定。 -->
            <p v-if="precheck.duration_estimate" class="faint eta">
              <template v-if="precheck.duration_estimate.available">
                预计耗时约 {{ etaText }}（按最近
                {{ precheck.duration_estimate.sample_count }} 次任务估算，仅供参考，
                不作为超时判定依据）
              </template>
              <template v-else>{{ precheck.duration_estimate.message }}</template>
            </p>

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

/* 设计稿用卡片表达可选项：选中靠边框与底色，没有系统圆点。卡片外面一圈边框、
   里面再留一个原生 radio，两种选中语义叠在一起，看起来突兀。
   控件仍然存在（键盘与读屏要靠它），只是从视觉上移除。 */
.rubric-radio {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  pointer-events: none;
}

/* 焦点要看得见：视觉上藏掉控件之后，键盘用户需要卡片自己给出焦点态。 */
.rubric:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-surface);
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
  /* 原生文件选择框有固有宽度（约 333px），在窄屏上比 `.drop` 的可用空间还宽，
     会把整页撑出横向滚动条。约束它，别让控件自己决定页面宽度。 */
  max-width: 100%;
  box-sizing: border-box;
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
