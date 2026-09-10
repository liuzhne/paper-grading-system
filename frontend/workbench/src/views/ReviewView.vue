<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";

import { useBatchesStore } from "@/stores/batches.js";
import { useReviewStore } from "@/stores/review.js";

const batches = useBatchesStore();
const review = useReviewStore();
const selected = ref(null);

/** 只有还需要复核的批次值得出现在选择器里。 */
const candidates = computed(() =>
  batches.batches.filter((b) =>
    ["scored", "scored_with_errors", "reviewed"].includes(b.status),
  ),
);

const canComplete = computed(
  () =>
    review.ordinaryPending === 0 &&
    review.blockingOpen === 0 &&
    review.entries.length === 0,
);

function formatScore(entry) {
  if (entry.ai_score === null || entry.ai_score === undefined) return "—";
  return `${entry.ai_score} / ${entry.max_score ?? "—"}`;
}

function formatConfidence(value) {
  // Core 不写 confidence。显示成 0% 会让人以为模型毫无把握，那是另一回事。
  return value === null || value === undefined ? "未提供" : `${Math.round(value * 100)}%`;
}

/** 已确认占比。分母为 0 时不该走到这里——模板已经先挡掉了。 */
const confirmedPercent = computed(() => {
  const stats = review.stats;
  if (!stats?.total_items) return "0%";
  return `${Math.round((stats.confirmed_items / stats.total_items) * 100)}%`;
});

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN");
}

async function refresh(batchId) {
  if (!batchId) return;
  await review.load(batchId);
  await Promise.all([review.loadStats(batchId), review.loadTimeline(batchId)]);
}

watch(selected, (id) => refresh(id));

onMounted(async () => {
  await batches.load();
  selected.value = candidates.value[0]?.id ?? null;
});

async function onAccept() {
  await review.acceptVisible("采纳系统给分");
  await Promise.all([review.loadStats(), review.loadTimeline()]);
}

async function onAcceptOne(entry) {
  if (!(await review.acceptOne(entry, "逐项确认系统给分"))) return;
  await Promise.all([review.loadStats(), review.loadTimeline()]);
}

async function onComplete() {
  if (await review.completeReview()) {
    await batches.load();
    await Promise.all([review.loadStats(), review.loadTimeline()]);
  }
}
</script>

<template>
  <div>
    <header class="page-head">
      <p class="page-eyebrow">人工在环</p>
      <h1 class="page-title">结果复核</h1>
      <p class="page-sub">先处理阻塞任务，再确认低置信度给分；所有人工改分都会留痕。</p>
    </header>

    <div class="toolbar">
      <label class="field picker">
        <span class="field-label">评分任务</span>
        <select v-model="selected" class="select">
          <option v-for="batch in candidates" :key="batch.id" :value="batch.id">
            {{ batch.name }}
          </option>
        </select>
      </label>
      <div class="btn-row">
        <button
          class="btn"
          type="button"
          :disabled="review.busy || !review.acceptableEntries.length"
          @click="onAccept"
        >
          采纳本页可采纳项（{{ review.acceptableEntries.length }}）
        </button>
        <button
          class="btn btn-primary"
          type="button"
          :disabled="review.busy || !canComplete"
          @click="onComplete"
        >
          完成复核
        </button>
      </div>
    </div>

    <p v-if="review.error" class="notice notice-danger" role="alert">{{ review.error }}</p>
    <p v-else-if="review.notice" class="notice" role="status">{{ review.notice }}</p>

    <p v-if="!candidates.length && !batches.loading" class="notice">
      当前没有已出结果、待复核的批次。
    </p>

    <div v-else class="layout">
      <div class="card table-wrap queue">
        <div class="card-head">
          <div>
            <h2 class="card-title">待确认给分</h2>
            <p class="card-note">
              阻塞任务 {{ review.blockingOpen }} 个 · 普通待确认 {{ review.ordinaryPending }} 项。
              阻塞代表结论本身还不成立，必须先处理。
            </p>
          </div>
        </div>
        <table class="table">
          <thead>
            <tr>
              <th>材料</th>
              <th>评分项</th>
              <th>系统给分</th>
              <th>置信度</th>
              <th>需要确认的原因</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="entry in review.entries" :key="entry.task_id || entry.score_item_id">
              <td>
                <span
                  v-if="entry.queue_type === 'blocking'"
                  class="chip chip-danger blocking"
                >阻塞</span>
                <span class="mono">{{ entry.student_id || "—" }}</span>
              </td>
              <td>
                <div>{{ entry.criterion_name || entry.criterion_code || "—" }}</div>
                <div class="faint mono code">{{ entry.criterion_code }}</div>
              </td>
              <td class="num">{{ formatScore(entry) }}</td>
              <td class="num" :class="{ faint: entry.confidence == null }">
                {{ formatConfidence(entry.confidence) }}
              </td>
              <td class="muted reason">{{ entry.review_reason || "—" }}</td>
              <td class="actions">
                <!-- 带上 paper：工作区默认落在列表首项，不带的话点「查看原文」
                     会打开另一份材料，而用户以为自己在看这一行。 -->
                <RouterLink
                  v-if="entry.paper_id"
                  class="btn btn-sm"
                  :to="{
                    name: 'grade',
                    params: { batchId: selected },
                    query: { paper: entry.paper_id },
                  }"
                >
                  查看原文
                </RouterLink>
                <span v-else class="faint">材料已删除</span>
                <!-- 阻塞任务与无 AI 分的项不给「确认」：前者缺的是结论本身，
                     后者根本没有分可采纳，点了必然失败。 -->
                <button
                  v-if="entry.acceptable"
                  class="btn btn-sm"
                  type="button"
                  :disabled="review.busy"
                  @click="onAcceptOne(entry)"
                >
                  确认
                </button>
              </td>
            </tr>
            <tr v-if="!review.entries.length && !review.loading">
              <td class="table-empty" colspan="6">
                该批次没有待确认项，可以完成复核。
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <aside class="side">
        <section v-if="review.stats" class="card card-pad">
          <h2 class="card-title">复核进度</h2>
          <p class="progress-line">
            <span class="mono big">{{ review.stats.confirmed_items }}</span>
            <span class="faint mono"> / {{ review.stats.total_items }} 项已确认</span>
          </p>
          <!-- 总数为 0 时不画：一条 0% 的进度条读起来是「一项都没确认」，而实际
               是「还没有项」。这两件事在复核页上意味着完全不同的下一步。 -->
          <div v-if="review.stats.total_items" class="progress-bar">
            <span class="bar">
              <span class="fill" :style="{ width: confirmedPercent }"></span>
            </span>
            <span class="faint mono pct">{{ confirmedPercent }}</span>
          </div>
          <dl class="stats">
            <div><dt>采纳系统给分</dt><dd class="mono">{{ review.stats.accepted_items }}</dd></div>
            <div><dt>人工调整</dt><dd class="mono">{{ review.stats.adjusted_items }}</dd></div>
            <div>
              <dt>平均调整幅度</dt>
              <dd class="mono">
                <template v-if="review.stats.average_adjustment !== null">
                  {{ review.stats.average_adjustment > 0 ? "+" : "" }}{{ review.stats.average_adjustment }}
                  <!-- 公开分母：读者据此判断这个平均值有多少代表性。 -->
                  <span class="faint">（n={{ review.stats.adjustment_sample_size }}）</span>
                </template>
                <span v-else class="faint">暂无调整</span>
              </dd>
            </div>
          </dl>
        </section>

        <section class="card card-pad">
          <h2 class="card-title">复核记录</h2>
          <ul v-if="review.timeline.length" class="timeline">
            <li v-for="entry in review.timeline" :key="entry.id">
              <div class="timeline-text">
                {{ entry.criterion_name || entry.criterion_code || "评分项" }}
                由 <b class="mono">{{ entry.before_score ?? "—" }}</b>
                改为 <b class="mono">{{ entry.after_score ?? "—" }}</b>
                <span class="faint">· {{ entry.reason }}</span>
              </div>
              <div class="faint mono timeline-time">{{ formatTime(entry.created_at) }}</div>
            </li>
          </ul>
          <p v-else class="faint">还没有复核记录。</p>
        </section>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 18px;
}

.picker {
  flex: 1 1 280px;
  max-width: 380px;
  margin-bottom: 0;
}

.layout {
  display: flex;
  gap: 20px;
  align-items: flex-start;
  flex-wrap: wrap;
}

.queue {
  flex: 999 1 460px;
  min-width: 0;
}

.side {
  flex: 1 1 260px;
}

.blocking {
  margin-right: 8px;
}

.code {
  font-size: 11.5px;
  margin-top: 3px;
}

.reason {
  font-size: 12.5px;
  max-width: 320px;
}

.actions {
  white-space: nowrap;
}

.actions > * + * {
  margin-left: 8px;
}

/* `.btn` 用在 `<a>` 上不会自己居中——原生 `<button>` 会，行内元素不会。 */
.actions .btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  text-decoration: none;
}

.progress-line {
  margin: 14px 0 8px;
}

.progress-bar {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 16px;
}

.bar {
  flex: 1 1 auto;
  height: 5px;
  border-radius: 3px;
  background: var(--border-light);
  overflow: hidden;
}

.fill {
  display: block;
  height: 100%;
  border-radius: 3px;
  background: var(--accent);
}

.pct {
  flex: none;
  font-size: 11.5px;
}

.big {
  font-size: 27px;
  font-weight: 500;
}

.stats {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 9px;
  font-size: 12.5px;
}

.stats > div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.stats dt {
  color: var(--text-muted);
}

.stats dd {
  margin: 0;
}

.timeline {
  list-style: none;
  margin: 14px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 13px;
}

.timeline-text {
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.timeline-time {
  font-size: 11.5px;
  margin-top: 4px;
}
</style>
