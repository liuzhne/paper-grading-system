<script setup>
import { computed, onMounted, ref } from "vue";
import { RouterLink } from "vue-router";

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

const distribution = computed(() =>
  continueWith.value ? store.distributionFor(continueWith.value.id) : null,
);

/**
 * 分布条形：只按当前这批的最大分归一，**不做跨批次分桶**。
 *
 * 分桶策略与「上一批次」的定义都还没定（计划 §11）：rubric 满分可变，非论文
 * Profile 量纲也不同，先编一套档位出来会把两个不可比的批次画进同一张图。
 */
const bars = computed(() => {
  const payload = distribution.value;
  if (!payload || !payload.scores.length) return [];
  const ceiling = payload.max_score || Math.max(...payload.scores);
  return payload.scores.map((score) => ({
    score,
    height: ceiling ? Math.max(4, Math.round((score / ceiling) * 100)) : 4,
  }));
});

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
  // 分布只为「继续处理」那一个批次加载：列表里每个批次都拉一次，等于为一张
  // 不显示的图付出 N 次请求。
  if (continueWith.value) {
    try {
      await store.loadDistribution(continueWith.value.id);
    } catch {
      // 分布是补充信息，取不到不该挡住整个工作台。
    }
  }
});
</script>

<template>
  <div>
    <header class="page-head">
      <p class="page-eyebrow">评审中心</p>
      <h1 class="page-title">工作台</h1>
      <p class="page-sub">从进行中的批次继续，或处理需要人工确认的给分。</p>
      <RouterLink class="btn btn-primary" :to="{ name: 'task-new' }">
        新建评分任务
      </RouterLink>
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

      <!-- 分数分布。分桶未定（计划 §11），这里画的是原始有效终分。 -->
      <section v-if="distribution" class="card card-pad dist">
        <div class="card-head">
          <h2 class="card-title">分数分布</h2>
          <span class="faint mono">{{ continueWith?.name }}</span>
        </div>

        <template v-if="bars.length">
          <div class="bars" role="img" :aria-label="`${bars.length} 份材料的终分分布`">
            <div
              v-for="(bar, index) in bars"
              :key="index"
              class="bar"
              :style="{ height: bar.height + '%' }"
              :title="`${bar.score} / ${distribution.max_score ?? '—'}`"
            ></div>
          </div>
          <div class="mini">
            <div><span class="faint">已出结果</span><b class="mono">{{ distribution.scored_count }}</b></div>
            <div>
              <span class="faint">平均</span>
              <!-- 只判 null 会让 undefined 走进 toFixed 并抛异常，整块卡片连同
                   页面一起白屏——白屏读起来像「系统坏了」，不像「这个数还没有」。 -->
              <b class="mono">{{ distribution.average == null ? "—" : distribution.average.toFixed(1) }}</b>
            </div>
            <div><span class="faint">满分</span><b class="mono">{{ distribution.max_score ?? "—" }}</b></div>
          </div>
        </template>
        <p v-else class="faint">该批次还没有有效终分。</p>

        <!-- 缺结果不是 0 分：并进分布会把平均分拉低成一个假数字。 -->
        <p v-if="distribution.without_results" class="faint dist-note">
          另有 {{ distribution.without_results }} 份材料没有有效结果，未计入分布。
        </p>
        <!-- 阻塞项的总分尚不成立：不说出来，这条曲线会被当成这批的最终形态。 -->
        <p v-if="distribution.blocking_open" class="faint dist-note">
          还有 {{ distribution.blocking_open }} 个阻塞任务未解决，分布仍会变化。
        </p>
        <p v-if="distribution.bucketing == null" class="faint dist-note">
          分档口径与跨批次比较尚未确定，此处显示原始终分。
        </p>
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


.dist .bars {
  display: flex;
  align-items: flex-end;
  gap: 6px;
  height: 120px;
  margin: 12px 0;
}

.dist .bar {
  flex: 1;
  min-width: 6px;
  background: var(--accent, #4a6cf7);
  border-radius: 3px 3px 0 0;
}

.dist-note {
  margin-top: 6px;
  font-size: 12px;
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
