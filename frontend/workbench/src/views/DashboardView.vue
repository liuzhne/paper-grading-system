<script setup>
import { computed, onMounted, ref } from "vue";

import { api, StaleContextError } from "@/api/client.js";
import { useBatchesStore } from "@/stores/batches.js";
import { stageLabel, stageTone } from "@/stores/stages.js";

const store = useBatchesStore();
const overview = ref(null);
const error = ref(null);

/** 待办口径的批次：已复核与已归档不再占用注意力。 */
const TODO_STAGES = ["draft", "parsing", "scoring", "scored", "scored_with_errors"];

const recent = computed(() =>
  [...store.batches]
    .sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))
    .slice(0, 5),
);

/** 最该继续处理的批次：优先含异常项，其次评分中。 */
const continueWith = computed(() => {
  const candidates = store.batches.filter((b) => TODO_STAGES.includes(b.status));
  return (
    candidates.find((b) => b.status === "scored_with_errors") ||
    candidates.find((b) => b.status === "scoring") ||
    candidates[0] ||
    null
  );
});

const continueProgress = computed(() =>
  continueWith.value ? store.progressFor(continueWith.value.id) : null,
);

function toneClass(code) {
  return { ok: "chip-ok", warn: "chip-warn", active: "chip-ok" }[stageTone(code)] || "";
}

function percent(ratio) {
  return ratio === null || ratio === undefined ? null : `${Math.round(ratio * 100)}%`;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN");
}

onMounted(async () => {
  try {
    overview.value = await api.get("/batches/overview");
  } catch (err) {
    if (!(err instanceof StaleContextError)) {
      error.value = err?.message || "加载工作台数据失败";
    }
  }
  await store.load();
  await Promise.all(store.batches.map((b) => store.loadProgress(b.id)));
});
</script>

<template>
  <div>
    <header class="page-head">
      <p class="page-eyebrow">评审中心</p>
      <h1 class="page-title">工作台</h1>
      <p class="page-sub">从进行中的批次继续，或处理需要人工确认的给分。</p>
    </header>

    <p v-if="error" class="notice notice-danger" role="alert">{{ error }}</p>

    <!-- KPI。计数来自服务端结果选择集合，与评分任务页同源。 -->
    <div v-if="overview" class="kpis card">
      <div class="kpi">
        <div class="kpi-label">进行中批次</div>
        <div class="kpi-value mono">{{ overview.active_batches }}</div>
        <div class="kpi-foot faint">共 {{ overview.material_counts.total }} 份材料</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">待评材料</div>
        <div class="kpi-value mono">{{ overview.material_counts.pending }}</div>
        <div class="kpi-foot faint">尚未产出结果</div>
      </div>
      <div class="kpi kpi-warn">
        <div class="kpi-label"><span class="dot dot-warn"></span>已评待复核</div>
        <div class="kpi-value mono">
          {{ overview.material_counts.scored - overview.material_counts.reviewed }}
        </div>
        <div class="kpi-foot faint">已出结果，尚未人工确认</div>
      </div>
      <div class="kpi kpi-danger">
        <div class="kpi-label"><span class="dot dot-danger"></span>失败材料</div>
        <div class="kpi-value mono">{{ overview.material_counts.failed }}</div>
        <div class="kpi-foot faint">需重新处理</div>
      </div>
    </div>

    <div class="grid">
      <!-- 继续处理 -->
      <section class="card card-pad continue">
        <p class="page-eyebrow">继续处理</p>
        <template v-if="continueWith">
          <h2 class="continue-name">{{ continueWith.name }}</h2>
          <p class="faint continue-sub">
            {{ continueWith.department || "—" }}
            <template v-if="continueWith.major"> · {{ continueWith.major }}</template>
            <template v-if="continueProgress"> · {{ continueProgress.counts.total }} 份材料</template>
          </p>

          <ol class="steps" aria-label="批次流程">
            <li v-for="step in ['解析', '评分', '复核', '导出']" :key="step">{{ step }}</li>
          </ol>

          <div v-if="continueProgress" class="mini">
            <div><span class="faint">已出结果</span><b class="mono">{{ continueProgress.counts.scored }}</b></div>
            <div><span class="faint">已复核</span><b class="mono">{{ continueProgress.counts.reviewed }}</b></div>
            <div><span class="faint">失败</span><b class="mono">{{ continueProgress.counts.failed }}</b></div>
          </div>

          <RouterLink class="btn btn-primary continue-cta" :to="{ name: 'review' }">
            前往结果复核
          </RouterLink>
        </template>
        <p v-else class="faint">当前没有需要继续处理的批次。</p>
      </section>

      <!-- 最近批次 -->
      <section class="card recent">
        <div class="card-head">
          <h2 class="card-title">评分任务</h2>
          <RouterLink :to="{ name: 'tasks' }">查看全部 {{ store.total }} 个</RouterLink>
        </div>
        <div class="table-wrap">
          <table class="table">
            <thead>
              <tr>
                <th>批次</th>
                <th>材料</th>
                <th>阶段</th>
                <th>更新</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="batch in recent" :key="batch.id">
                <td>
                  <div class="name">{{ batch.name }}</div>
                  <div class="faint sub">
                    {{ batch.department || "—" }}
                    <template v-if="percent(store.progressFor(batch.id)?.completion_ratio)">
                      · {{ percent(store.progressFor(batch.id)?.completion_ratio) }}
                    </template>
                  </div>
                </td>
                <td class="num">{{ store.progressFor(batch.id)?.counts.total ?? "—" }}</td>
                <td>
                  <span class="chip" :class="toneClass(batch.status)">{{ stageLabel(batch.status) }}</span>
                </td>
                <td class="num muted">{{ formatTime(batch.updated_at) }}</td>
              </tr>
              <tr v-if="!recent.length && !store.loading">
                <td class="table-empty" colspan="4">还没有评分任务。</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  overflow: hidden;
}

.kpi {
  padding: 19px 22px;
  border-right: 1px solid var(--border-light);
}

.kpi:last-child {
  border-right: 0;
}

.kpi-warn {
  background: var(--warn-surface);
}

.kpi-danger {
  background: var(--danger-surface);
}

.kpi-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--text-muted);
  margin-bottom: 10px;
}

.kpi-value {
  font-size: 29px;
  font-weight: 500;
  line-height: 1;
}

.kpi-warn .kpi-value {
  color: var(--warn-ink);
}

.kpi-danger .kpi-value {
  color: var(--danger);
}

.kpi-foot {
  font-size: 12px;
  margin-top: 9px;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex: none;
}

.dot-warn {
  background: var(--warn);
}

.dot-danger {
  background: var(--danger);
}

.grid {
  display: flex;
  gap: 20px;
  align-items: flex-start;
  flex-wrap: wrap;
}

.continue {
  flex: 1 1 340px;
}

.recent {
  flex: 999 1 420px;
  min-width: 0;
}

.continue-name {
  margin: 12px 0 6px;
  font-size: 17px;
  font-weight: 650;
  line-height: 1.45;
}

.continue-sub {
  margin: 0 0 20px;
  font-size: 13px;
}

.steps {
  list-style: none;
  display: flex;
  justify-content: space-between;
  margin: 0 0 20px;
  padding: 0;
  font-size: 11.5px;
  color: var(--text-muted);
}

.mini {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  border: 1px solid var(--border-light);
  border-radius: 9px;
  overflow: hidden;
  margin-bottom: 20px;
}

.mini > div {
  padding: 12px 14px;
  border-right: 1px solid var(--border-light);
  font-size: 11.5px;
}

.mini > div:last-child {
  border-right: 0;
}

.mini b {
  display: block;
  margin-top: 5px;
  font-size: 16px;
  font-weight: 500;
}

.continue-cta {
  display: block;
  text-align: center;
  line-height: 36px;
  color: #fff;
}

.name {
  font-weight: 550;
}

.sub {
  font-size: 11.5px;
  margin-top: 3px;
}
</style>
