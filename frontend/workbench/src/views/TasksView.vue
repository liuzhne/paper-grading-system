<script setup>
import { computed, onMounted } from "vue";
import { useRouter } from "vue-router";

import { useBatchesStore } from "@/stores/batches.js";
import { STAGES, STAGE_ORDER, stageLabel, stageTone } from "@/stores/stages.js";

const store = useBatchesStore();
const router = useRouter();

const filters = computed(() =>
  STAGE_ORDER.filter((code) => store.stageCounts[code] > 0).map((code) => ({
    code,
    label: stageLabel(code),
    count: store.stageCounts[code],
  })),
);

function toneClass(code) {
  return { ok: "chip-ok", warn: "chip-warn", active: "chip-ok" }[stageTone(code)] || "";
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN");
}

function percent(ratio) {
  // 空批次的 completion_ratio 为 null：显示占位而不是 0%，两者含义不同。
  return ratio === null || ratio === undefined
    ? null
    : `${Math.round(ratio * 100)}%`;
}

function openBatch(batch) {
  router.push({ name: "grade", params: { batchId: batch.id } });
}

onMounted(async () => {
  await store.load();
  // 进度按需逐个取：列表接口不返回计数，避免让它承担聚合职责。
  await Promise.all(store.batches.map((batch) => store.loadProgress(batch.id)));
});
</script>

<template>
  <div>
    <header class="page-head">
      <p class="page-eyebrow">评审</p>
      <h1 class="page-title">评分任务</h1>
      <p class="page-sub">一个批次固定绑定一个已发布的评分标准版本。</p>
    </header>

    <p v-if="store.error" class="notice notice-danger" role="alert">{{ store.error }}</p>

    <div v-if="filters.length" class="filters" role="group" aria-label="按阶段筛选">
      <button
        class="filter"
        :class="{ active: store.stageFilter === null }"
        type="button"
        @click="store.setStageFilter(null)"
      >
        全部 {{ store.total }}
      </button>
      <button
        v-for="filter in filters"
        :key="filter.code"
        class="filter"
        :class="{ active: store.stageFilter === filter.code }"
        type="button"
        @click="store.setStageFilter(filter.code)"
      >
        {{ filter.label }} {{ filter.count }}
      </button>
    </div>

    <div class="layout">
      <div class="card table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>批次名称</th>
              <th>材料</th>
              <th>进度</th>
              <th>阶段</th>
              <th>更新时间</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="batch in store.visible"
              :key="batch.id"
              class="row"
              tabindex="0"
              @click="openBatch(batch)"
              @keydown.enter="openBatch(batch)"
            >
              <td>
                <div class="name">{{ batch.name }}</div>
                <div class="faint sub">{{ batch.department || "—" }}{{ batch.major ? ` · ${batch.major}` : "" }}</div>
              </td>
              <td class="num">{{ store.progressFor(batch.id)?.counts.total ?? "—" }}</td>
              <td>
                <div v-if="percent(store.progressFor(batch.id)?.completion_ratio)" class="progress">
                  <span class="bar">
                    <span
                      class="fill"
                      :style="{ width: percent(store.progressFor(batch.id)?.completion_ratio) }"
                    ></span>
                  </span>
                  <span class="num faint">{{ percent(store.progressFor(batch.id)?.completion_ratio) }}</span>
                </div>
                <span v-else class="faint">暂无材料</span>
                <!-- 阶段字段不表达任务故障，失败与待恢复必须单独显示。 -->
                <div
                  v-if="store.progressFor(batch.id)?.counts.failed"
                  class="faint failed"
                >
                  {{ store.progressFor(batch.id).counts.failed }} 份失败
                </div>
                <div
                  v-if="['failed', 'canceled'].includes(store.progressFor(batch.id)?.job?.status)"
                  class="faint failed"
                >
                  任务{{ store.progressFor(batch.id).job.status === "failed" ? "失败" : "已取消" }} · 待恢复
                </div>
              </td>
              <td>
                <span class="chip" :class="toneClass(batch.status)">
                  {{ stageLabel(batch.status) }}
                </span>
                <div class="faint mono code">{{ batch.status }}</div>
              </td>
              <td class="num muted">{{ formatTime(batch.updated_at) }}</td>
            </tr>
            <tr v-if="!store.visible.length && !store.loading">
              <td class="table-empty" colspan="5">
                {{ store.stageFilter ? "该阶段暂无批次。" : "还没有评分任务。" }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <aside class="card card-pad legend">
        <h2 class="card-title">状态说明</h2>
        <p class="card-note">界面标签与后端状态字段的对应关系。</p>
        <ul>
          <li v-for="code in STAGE_ORDER" :key="code">
            <span class="dot" :class="`dot-${STAGES[code].tone}`" aria-hidden="true"></span>
            <span>
              <span class="legend-label">{{ STAGES[code].label }}</span>
              <span class="faint legend-hint">{{ STAGES[code].hint }}</span>
              <span class="faint mono code">{{ code }}</span>
            </span>
          </li>
        </ul>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.filter {
  padding: 6px 14px;
  border-radius: 20px;
  border: 1px solid var(--border-input);
  background: var(--surface);
  color: var(--text-secondary);
  font-size: 13px;
  cursor: pointer;
}

.filter.active {
  background: var(--sidebar-bg);
  border-color: var(--sidebar-bg);
  color: #fff;
}

.layout {
  display: flex;
  gap: 20px;
  align-items: flex-start;
  flex-wrap: wrap;
}

.layout > .table-wrap {
  flex: 999 1 460px;
  min-width: 0;
}

.legend {
  flex: 1 1 260px;
}

.row {
  cursor: pointer;
}

.row:hover,
.row:focus-visible {
  background: var(--surface-muted);
}

.name {
  font-weight: 550;
}

.sub,
.code {
  font-size: 11.5px;
  margin-top: 3px;
}

.failed {
  font-size: 11.5px;
  margin-top: 4px;
  color: var(--danger);
}

.progress {
  display: flex;
  align-items: center;
  gap: 9px;
}

.bar {
  width: 64px;
  height: 5px;
  border-radius: 3px;
  background: var(--border-light);
  overflow: hidden;
  flex: none;
}

.fill {
  display: block;
  height: 100%;
  border-radius: 3px;
  background: var(--accent);
}

.legend ul {
  list-style: none;
  margin: 16px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.legend li {
  display: flex;
  gap: 9px;
  align-items: flex-start;
  font-size: 12.5px;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex: none;
  margin-top: 6px;
  background: var(--text-ghost);
}

.dot-ok {
  background: var(--ok);
}

.dot-active {
  background: var(--ok-dot);
}

.dot-warn {
  background: var(--warn);
}

.legend-label {
  display: block;
}

.legend-hint {
  display: block;
  margin-top: 2px;
}
</style>
