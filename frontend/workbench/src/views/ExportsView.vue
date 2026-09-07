<script setup>
import { computed, onMounted, ref, watch } from "vue";

import { api, ApiError, StaleContextError } from "@/api/client.js";
import { useBatchesStore } from "@/stores/batches.js";

/**
 * 输出中心（前端 v2 计划 §5-F）。
 *
 * 两条口径直接体现在界面上：
 * - **正式成绩与审计导出的门槛不同。** 未完成复核只挡住成绩单，报告与结构化
 *   数据仍可导出，只是会标注「未完成」——新界面不该收紧旧服务已有的能力，
 *   也不该放宽它。
 * - **「已生成」不是「已下载」。** 历史里的状态就写「已生成」。
 */
const batches = useBatchesStore();
const selected = ref(null);
const precheck = ref(null);
const history = ref([]);
const error = ref(null);
const busy = ref(false);

const candidates = computed(() =>
  batches.batches.filter((b) =>
    ["scored", "scored_with_errors", "reviewed", "archived"].includes(b.status),
  ),
);

const CHANNELS = [
  {
    key: "html_report",
    title: "HTML 评审报告",
    format: "html",
    desc: "逐份材料的评分明细、证据引用与复核记录，可在浏览器查看或打印。",
    final: false,
  },
  {
    key: "xlsx",
    title: "成绩单",
    format: "xlsx",
    desc: "按批次汇总各评分项与总分，含复核后的最终分数。",
    final: true,
  },
  {
    key: "json",
    title: "结构化数据",
    format: "grading-core/run-export@2",
    desc: "完整评分过程数据，含每项给分、证据位置与所用标准版本，供归档或二次分析。",
    final: false,
  },
  {
    key: "sheets",
    title: "写入 Google Sheets",
    format: "sheets",
    desc: "将成绩写入配置好的在线表格。",
    final: true,
  },
];

function channelAvailable(channel) {
  if (!precheck.value) return false;
  if (precheck.value.channels[channel.key] === false) return false;
  // 成绩类通道要求复核清零；审计类导出允许中间状态。
  return channel.final ? precheck.value.can_export_final : precheck.value.can_export_audit;
}

function channelReason(channel) {
  if (!precheck.value) return "";
  if (precheck.value.channels[channel.key] === false) {
    return "当前部署未启用该通道。";
  }
  if (channel.final && !precheck.value.can_export_final) {
    return precheck.value.blocking.map((b) => b.message).join(" ");
  }
  return "";
}

const STATUS_LABEL = { generated: "已生成", succeeded: "成功", failed: "失败" };

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN");
}

async function load(id) {
  if (!id) {
    precheck.value = null;
    history.value = [];
    return;
  }
  error.value = null;
  try {
    precheck.value = await api.get(`/batches/${id}/export-precheck`);
    history.value = (await api.get(`/batches/${id}/export-history`)).entries;
  } catch (err) {
    if (!(err instanceof StaleContextError)) error.value = err?.message || "加载失败";
  }
}

async function onExport(channel) {
  busy.value = true;
  error.value = null;
  try {
    await api.post(`/batches/${selected.value}/export-events`, {
      channel: channel.key,
      scope: "batch",
      result_revision: precheck.value.result_revision,
    });
    await load(selected.value);
  } catch (err) {
    error.value =
      err instanceof ApiError && err.status === 409
        ? err.detail || "批次结果已更新，请刷新后重试。"
        : err?.message || "导出失败";
  } finally {
    busy.value = false;
  }
}

watch(selected, (id) => load(id));
onMounted(async () => {
  await batches.load();
  selected.value = candidates.value[0]?.id ?? null;
});
</script>

<template>
  <div>
    <header class="page-head">
      <p class="page-eyebrow">标准与输出</p>
      <h1 class="page-title">输出中心</h1>
      <p class="page-sub">选择范围，再选择交付方式。</p>
    </header>

    <p v-if="error" class="notice notice-danger" role="alert">{{ error }}</p>

    <label class="field picker">
      <span class="field-label">评分任务</span>
      <select v-model="selected" class="select">
        <option v-for="batch in candidates" :key="batch.id" :value="batch.id">
          {{ batch.name }}
        </option>
      </select>
    </label>

    <p v-if="!candidates.length && !batches.loading" class="notice">
      还没有可导出的批次。
    </p>

    <div v-else class="layout">
      <div class="main">
        <div class="channels">
          <div
            v-for="channel in CHANNELS"
            :key="channel.key"
            class="card card-pad channel"
            :class="{ off: !channelAvailable(channel) }"
          >
            <div class="channel-head">
              <span class="channel-title">{{ channel.title }}</span>
              <span class="faint mono channel-fmt">{{ channel.format }}</span>
            </div>
            <p class="faint channel-desc">{{ channel.desc }}</p>
            <p v-if="channelReason(channel)" class="channel-reason">
              {{ channelReason(channel) }}
            </p>
            <button
              class="btn"
              :class="{ 'btn-primary': channelAvailable(channel) }"
              type="button"
              :disabled="busy || !channelAvailable(channel)"
              @click="onExport(channel)"
            >
              {{ channelAvailable(channel) ? "生成" : "不可用" }}
            </button>
          </div>
        </div>

        <section class="card">
          <div class="card-head">
            <h2 class="card-title">导出历史</h2>
            <p class="card-note">
              「已生成」表示文件已产出；客户端是否成功下载不在服务端记录范围内。
            </p>
          </div>
          <div class="table-wrap">
            <table class="table">
              <thead>
                <tr><th>时间</th><th>通道</th><th>范围</th><th>操作人</th><th>状态</th></tr>
              </thead>
              <tbody>
                <tr v-for="entry in history" :key="entry.id">
                  <td class="num muted">{{ formatTime(entry.created_at) }}</td>
                  <td>
                    <span class="mono">{{ entry.channel }}</span>
                    <span v-if="entry.legacy_target_type" class="faint mono legacy">
                      （旧记录 {{ entry.legacy_target_type }}）
                    </span>
                  </td>
                  <td class="muted">{{ entry.scope }}</td>
                  <td :class="{ faint: !entry.actor_id }">{{ entry.actor_display }}</td>
                  <td>
                    <span class="chip" :class="entry.status === 'failed' ? 'chip-danger' : 'chip-ok'">
                      {{ STATUS_LABEL[entry.status] || entry.status }}
                    </span>
                  </td>
                </tr>
                <tr v-if="!history.length">
                  <td class="table-empty" colspan="5">该批次还没有导出记录。</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <aside class="card card-pad side">
        <h2 class="card-title">导出前检查</h2>
        <p class="card-note">未通过的项不阻止审计导出，但会阻止正式成绩。</p>

        <ul v-if="precheck" class="checks">
          <li v-for="item in precheck.blocking" :key="item.code">
            <span class="dot dot-danger"></span>
            <span>{{ item.message }}</span>
          </li>
          <li v-for="item in precheck.warnings" :key="item.code">
            <span class="dot dot-warn"></span>
            <span>{{ item.message }}</span>
          </li>
          <li v-if="!precheck.blocking.length && !precheck.warnings.length">
            <span class="dot dot-ok"></span>
            <span>全部检查通过。</span>
          </li>
        </ul>

        <p v-if="precheck && !precheck.channels.sheets" class="faint field-hint">
          当前部署未启用在线表格写入。
        </p>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.picker {
  max-width: 380px;
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

.side {
  flex: 1 1 270px;
}

.channels {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}

.channel {
  display: flex;
  flex-direction: column;
  margin-bottom: 0;
}

.channel.off {
  background: var(--surface-muted);
}

.channel-head {
  display: flex;
  align-items: baseline;
  gap: 9px;
  margin-bottom: 7px;
}

.channel-title {
  font-size: 14.5px;
  font-weight: 650;
}

.channel.off .channel-title {
  color: var(--text-faint);
}

.channel-fmt {
  font-size: 11px;
}

.channel-desc {
  font-size: 12.5px;
  line-height: 1.7;
  margin: 0 0 12px;
  flex: 1;
}

.channel-reason {
  font-size: 12px;
  line-height: 1.7;
  color: var(--warn-ink);
  margin: 0 0 12px;
}

.legacy {
  font-size: 11px;
}

.checks {
  list-style: none;
  margin: 16px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.checks li {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  font-size: 12.5px;
  line-height: 1.7;
}

.dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  flex: none;
  margin-top: 5px;
}

.dot-ok {
  background: var(--ok);
}

.dot-warn {
  background: var(--warn);
}

.dot-danger {
  background: var(--danger);
}
</style>
