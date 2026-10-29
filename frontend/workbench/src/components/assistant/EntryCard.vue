<script setup>
/**
 * 对话里的入口卡片（评分助手 v2 设计稿）：流程卡片只显示成“01 核对评分规则 · 说明 · 在右侧查看”，
 * 点开在工作区显示那一步；结果卡片同样显示成入口，点开对应的结果标签。要老师做的事在工作区里做。
 */
import { computed } from "vue";

import { STEPS, entryStep } from "@/lib/assistant-workspace.js";
import { finishedCount } from "@/lib/score-jobs.js";
import { useAssistantStore } from "@/stores/assistant.js";

const props = defineProps({
  card: { type: Object, required: true },
  plan: { type: Object, default: null },
  /** 结果卡片：{tag, tone, title}；流程卡片不传。 */
  artifact: { type: Object, default: null },
  active: Boolean,
});

const store = useAssistantStore();
const step = computed(() => (props.artifact ? null : entryStep(props.card)));
const index = computed(() => (props.plan?.steps || []).findIndex((item) => item.key === step.value));

const meta = computed(() => {
  const card = props.card;
  if (props.artifact) return props.artifact.meta || "";
  const status = card.status;
  switch (card.type) {
    case "pick_rubric_files": return status === "proposed" ? "等你上传评分规则和模板" : status === "done" ? "已上传" : "没有导入";
    case "rubric_wait": return status === "done" ? "已发布" : status === "running" ? "等你核对并发布" : "没有发布";
    case "pick_files":
      if (status === "done") return `${card.result?.count ?? ""} 份`;
      return status === "proposed" ? `等你选择 · 评分标准《${card.rubric_name || ""}》` : "没有选择";
    case "upload_papers":
      if (status === "done") return [`${card.result?.ready ?? 0} 份可以评分`,
        card.result?.removable ? `${card.result.removable} 份无法评分` : ""].filter(Boolean).join(" · ");
      return status === "failed" ? "上传或预检没有完成" : `上传与预检 · ${card.file_count} 份`;
    case "confirm_start": {
      if (status === "proposed") return [`${card.ready_count ?? card.file_count} 份可以评分`,
        card.warnings?.length ? `${card.warnings.length} 份需注意` : "", card.removable?.length ? `${card.removable.length} 份无法评分` : ""]
        .filter(Boolean).join(" · ");
      return { running: "正在开始", done: "已开始评分", dismissed: "这次没有评", expired: "已过期", failed: "没有开始" }[status] || "";
    }
    case "job_progress": {
      const job = store.jobs[card.batch_id];
      return job ? `${finishedCount(job)}/${job.total_items} 已完成` : "评分进度";
    }
    case "overview": return "评分完成";
    case "blocked_files": return status === "proposed" ? "有文件无法评分，等你决定" : "已处理";
    default: return "";
  }
});

const num = computed(() => (props.artifact ? props.artifact.tag : index.value >= 0 ? String(index.value + 1).padStart(2, "0") : "··"));
const title = computed(() => (props.artifact ? props.artifact.title : STEPS[step.value]?.title || "评分任务"));

function open() {
  if (props.artifact) store.openArtifact(props.card.id);
  else store.openStep(step.value && props.plan?.steps?.some((item) => item.key === step.value && item.status === "done") ? step.value : null);
}
</script>

<template>
  <button class="entry" :class="{ active, artifact: Boolean(artifact) }" type="button" :data-entry="artifact ? card.type : step" @click="open">
    <span class="num" :class="artifact ? `tone-${artifact.tone}` : ''">{{ num }}</span>
    <span class="copy">
      <span class="entry-title">{{ title }}</span>
      <span v-if="meta" class="entry-meta">{{ meta }}</span>
    </span>
    <span class="cta">{{ active ? "正在右侧显示" : "在右侧查看" }}</span>
  </button>
</template>

<style scoped>
.entry { margin-top: 12px; width: 100%; max-width: 460px; display: flex; align-items: center; gap: 12px; padding: 10px 14px 10px 10px;
  border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); text-align: left; cursor: pointer; font: inherit; }
.entry:hover { border-color: var(--border-input-hover); }
.entry.active { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(23, 92, 80, 0.08); }
.num { flex: none; width: 34px; height: 34px; border-radius: 8px; display: grid; place-items: center; font-family: var(--font-mono);
  font-size: 11.5px; font-weight: 600; background: var(--chip-bg); color: var(--text-secondary); }
.artifact .num { font-family: var(--font-sans); font-size: 11.5px; }
.num.tone-accent { background: var(--accent-soft); color: var(--accent); }
.num.tone-info { background: var(--info-bg); color: var(--info-ink); }
.num.tone-warn { background: var(--warn-bg); color: var(--warn-ink); }
.entry.active .num { background: var(--accent); color: #fff; }
.copy { flex: 1; min-width: 0; }
.entry-title { display: block; font-size: 13.5px; font-weight: 600; color: var(--text); }
.entry-meta { display: block; margin-top: 2px; font-size: 12px; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cta { flex: none; font-size: 12px; color: var(--text-muted); }
.entry.active .cta { color: var(--accent); }
</style>
