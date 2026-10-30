<script setup>
/**
 * 待评分文件：选择、上传预检、确认开始共用一个视图（v2 界面改造方案 §4.1）。
 * 选文件 → 浏览器直传 → 后端预检 → 确认卡片带回可评、需注意、无法评分；“开始评分”与卡片上同一个动作。
 */
import { computed, onMounted, ref, watch } from "vue";

import { api, StaleContextError } from "@/api/client.js";
import { MAX_FILES } from "@/lib/assistant-flow.js";
import { fileBadge } from "@/lib/assistant-workspace.js";
import { useAssistantStore } from "@/stores/assistant.js";
import { uploadStatusLabel, useUploadStore } from "@/stores/upload.js";

import WsLayout from "./WsLayout.vue";

const props = defineProps({
  context: { type: Object, required: true },
  head: { type: Object, required: true },
});

const store = useAssistantStore();
const upload = useUploadStore();
const dragging = ref(false);
const summary = ref(null);
const loadError = ref(null);

const kind = computed(() => store.pending?.kind || null);
const picking = computed(() => kind.value === "pick_files");
const uploading = computed(() => kind.value === "upload_papers");
const confirming = computed(() => kind.value === "confirm_start");
const confirmCard = computed(() => props.context.cards.confirm_start || null);
const pickCard = computed(() => props.context.cards.pick_files || null);
const batchId = computed(() => props.context.batchId || null);
const disabled = computed(() => store.sending || store.uploading);
const filesLost = computed(() => uploading.value && !store.filesForPending());

async function loadSummary() {
  if (!batchId.value) return;
  try {
    summary.value = await api.get(`/batches/${batchId.value}/summary`);
  } catch (err) {
    if (!(err instanceof StaleContextError)) loadError.value = "无法加载任务里的文件";
  }
}
onMounted(loadSummary);
watch(() => [batchId.value, kind.value, confirmCard.value?.status], loadSummary);

function choose(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  if (filesLost.value) store.replaceFiles(list);
  else if (picking.value) store.pickFiles({ id: store.pending.card_id }, list);
}
function onPick(event) {
  choose(event.target.files);
  event.target.value = "";
}
function onDrop(event) {
  dragging.value = false;
  choose(event.dataTransfer?.files);
}

// 上传中：按本页的上传队列；之后：按任务里的材料，叠上确认卡片带回的预检结果。
const rows = computed(() => {
  if (uploading.value && upload.queue.length) {
    return upload.queue.map((entry) => ({
      key: entry.id, name: entry.name, sub: fileBadge(entry.name).ext,
      status: uploadStatusLabel(entry.status),
      tone: entry.status === "done" ? "accent" : ["failed", "rejected"].includes(entry.status) ? "danger" : "muted",
      note: entry.error || "",
    }));
  }
  const papers = (summary.value?.papers || []).slice().reverse();
  const removable = new Map((confirmCard.value?.removable || []).map((row) => [row.paper_id, row.message]));
  const warnings = new Map((confirmCard.value?.warnings || []).map((row) => [row.paper_id, row.message]));
  return papers.map((paper) => {
    const owner = paper.student_name || paper.title || "";
    const sub = [owner, fileBadge(paper.file_name).ext].filter(Boolean).join(" · ");
    if (removable.has(paper.paper_id)) {
      return { key: paper.paper_id, name: paper.file_name, sub, status: "无法评分", tone: "danger", note: removable.get(paper.paper_id) };
    }
    if (warnings.has(paper.paper_id)) {
      return { key: paper.paper_id, name: paper.file_name, sub, status: "需注意", tone: "warn", note: warnings.get(paper.paper_id) };
    }
    const failed = paper.paper_status === "failed";
    return { key: paper.paper_id, name: paper.file_name, sub, status: failed ? "解析失败" : "通过",
      tone: failed ? "danger" : "accent", note: "" };
  });
});

const uploadCount = computed(() => {
  const total = upload.queue.length;
  const done = upload.queue.filter((entry) => ["done", "failed", "rejected"].includes(entry.status)).length;
  return { done, total };
});

const summaryLine = computed(() => {
  const card = confirmCard.value;
  if (uploading.value) return uploadCount.value.total ? `已上传 ${uploadCount.value.done}/${uploadCount.value.total} 份` : "正在上传…";
  if (!card) return rows.value.length ? `${rows.value.length} 份` : "";
  const parts = [`${card.file_count} 份`, `${card.ready_count} 份可以评分`];
  if (card.warnings?.length) parts.push(`${card.warnings.length} 份需注意`);
  if (card.removable?.length) parts.push(`${card.removable.length} 份无法评分`);
  return parts.join(" · ");
});

const primaryLabel = computed(() => {
  const card = confirmCard.value;
  if (!card) return "开始评分";
  const head = card.removable?.length ? `移除 ${card.removable.length} 份，` : "";
  return `${head}开始评分 · ${card.ready_count} 份`;
});

const footText = computed(() => {
  const card = confirmCard.value;
  if (picking.value) return `PDF、Word 均可，一次最多 ${MAX_FILES} 份；选好后自动上传并预检。`;
  if (uploading.value) return filesLost.value ? "页面刷新后，本页内存里的文件已失效；任务已建好，重新选择同一批文件后接着上传。"
    : `正在上传与预检 ${uploadCount.value.done}/${uploadCount.value.total}`;
  if (confirming.value && card) {
    const notes = [];
    if (card.warnings?.length) notes.push("需注意的照常评分，结果建议复核");
    if (card.removable?.length) notes.push("无法评分的开始时移除");
    return notes.length ? `${summaryLine.value}（${notes.join("；")}）` : `${summaryLine.value}，确认后开始评分。`;
  }
  if (!card) return "";
  return { done: "已开始评分。", running: "正在开始评分…", dismissed: "这次没有开始评分，任务留作草稿。",
    expired: "这张确认已过期。", failed: "开始评分没有成功，原因见对话。" }[card.status] || "";
});
</script>

<template>
  <WsLayout v-bind="head">
    <div class="body">
      <p v-if="pickCard?.rubric_name" class="rubric-line">
        评分标准《{{ pickCard.rubric_name }}》<span class="mono">{{ pickCard.rubric_version }}</span>
      </p>

      <label v-if="picking || filesLost" class="drop" :class="{ over: dragging }"
             @dragover.prevent="dragging = true" @dragleave="dragging = false" @drop.prevent="onDrop">
        <input type="file" multiple accept=".docx,.pdf" :disabled="disabled" @change="onPick" />
        <strong>{{ filesLost ? "重新选择这批文件" : "把待评分的文件拖到这里，或点击选择" }}</strong>
        <span>PDF、Word 均可，可一次选择多份；最多 {{ MAX_FILES }} 份</span>
        <span class="btn btn-primary btn-sm pick">选择文件</span>
      </label>

      <template v-if="rows.length">
        <p class="count">{{ summaryLine }}</p>
        <div class="row-list">
          <div v-for="(row, index) in rows" :key="row.key" class="row" :class="{ 'is-warn': row.tone === 'warn' || row.tone === 'danger' }">
            <span class="row-idx">{{ String(index + 1).padStart(2, "0") }}</span>
            <div class="row-main">
              <div class="row-title">{{ row.name }}</div>
              <div v-if="row.sub" class="row-sub">{{ row.sub }}</div>
              <div v-if="row.note" class="row-note" :class="{ danger: row.tone === 'danger' }">{{ row.note }}</div>
            </div>
            <span class="status-dot" :class="`dot-${row.tone}`">{{ row.status }}</span>
          </div>
        </div>
      </template>
      <p v-if="loadError" class="notice notice-danger">{{ loadError }}</p>
    </div>

    <template #foot>
      <p class="foot-text">{{ footText }}</p>
      <template v-if="confirming">
        <button class="btn" type="button" :disabled="disabled" @click="store.answerConfirm('cancel')">不评了</button>
        <button class="btn btn-primary" type="button" :disabled="disabled" data-test="start-scoring"
                @click="store.answerConfirm('confirm')">{{ primaryLabel }}</button>
      </template>
      <button v-else-if="uploading" class="btn btn-primary" type="button" disabled>开始评分</button>
    </template>
  </WsLayout>
</template>

<style scoped>
.body { padding: 14px 24px 0; }
.rubric-line { margin: 0 0 12px; font-size: 12.5px; color: var(--text-muted); }
.drop {
  display: flex; flex-direction: column; align-items: center; gap: 6px;
  padding: 40px 20px; border: 1.5px dashed #cfcec7; border-radius: var(--radius-lg);
  background: #fcfbf9; text-align: center; cursor: pointer;
}
.drop:hover, .drop.over { border-color: var(--accent); background: #f7faf9; }
.drop input { position: absolute; width: 1px; height: 1px; opacity: 0; }
.drop strong { font-size: 15px; font-weight: 650; }
.drop span { font-size: 12.5px; color: var(--text-muted); line-height: 1.7; }
.drop .pick { margin-top: 10px; color: #fff; }
.count { margin: 16px 0 10px; font-size: 12.5px; color: var(--neutral-ink); }
.drop + .count { margin-top: 20px; }
.row-note.danger { color: var(--danger); }
.dot-accent { color: var(--accent); }
.dot-warn { color: var(--warn-ink); }
.dot-danger { color: var(--danger); }
.dot-muted { color: var(--text-muted); }
</style>
