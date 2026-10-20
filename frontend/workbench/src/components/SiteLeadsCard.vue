<script setup>
import { computed, onMounted, ref } from "vue";

import { api, StaleContextError } from "@/api/client.js";

/**
 * 运维页「官网申请」：官网提交的试用与部署评估申请（docs/官网申请落库改造方案.md §3）。
 *
 * 只给平台管理员（父组件按 `view_platform_ops` 决定是否渲染，接口同样只认平台
 * 管理员）。本期不发通知，新申请靠这张卡片的数量提示。改状态与删除都写审计，
 * 审计里不记邮箱。
 */
const KIND_LABELS = { trial: "试用", deploy_assessment: "部署评估" };
const STATUS_OPTIONS = [
  { value: "new", label: "新申请" },
  { value: "contacted", label: "已联系" },
  { value: "closed", label: "已结束" },
  { value: "spam", label: "垃圾" },
];
const PAGE_SIZE = 50;

const filter = ref(/** @type {"new"|"all"} */ ("new"));
const page = ref(/** @type {{ items: any[], total: number, new_count: number } | null} */ (null));
const offset = ref(0);
const loading = ref(false);
const error = ref("");
const busyId = ref("");

const hasMore = computed(() => !!page.value && offset.value + page.value.items.length < page.value.total);

async function load(reset = true) {
  if (reset) offset.value = 0;
  loading.value = true;
  error.value = "";
  const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset.value) });
  if (filter.value === "new") query.set("status", "new");
  try {
    const result = await api.get(`/site/leads?${query}`);
    if (!result || !Array.isArray(result.items)) {
      page.value = null;
      return;
    }
    page.value = reset || !page.value ? result : { ...result, items: [...page.value.items, ...result.items] };
  } catch (err) {
    if (err instanceof StaleContextError) return;
    error.value = err?.message || "加载失败";
  } finally {
    loading.value = false;
  }
}

function loadMore() {
  if (!page.value) return;
  offset.value += PAGE_SIZE;
  load(false);
}

function switchFilter(value) {
  if (filter.value === value) return;
  filter.value = value;
  load();
}

async function setStatus(lead, status) {
  if (lead.status === status) return;
  busyId.value = lead.id;
  error.value = "";
  try {
    await api.patch(`/site/leads/${lead.id}`, { status });
    await load();
  } catch (err) {
    error.value = err?.message || "保存失败，请稍后重试。";
  } finally {
    busyId.value = "";
  }
}

async function remove(lead) {
  if (!window.confirm(`删除 ${lead.email} 的申请？删除后不能恢复。`)) return;
  busyId.value = lead.id;
  error.value = "";
  try {
    await api.del(`/site/leads/${lead.id}`);
    await load();
  } catch (err) {
    error.value = err?.message || "删除失败，请稍后重试。";
  } finally {
    busyId.value = "";
  }
}

function formatTime(value) {
  const date = new Date(`${value}Z`);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

function mailtoFor(lead) {
  const subject = lead.kind === "trial" ? "有据智评 · 试用申请" : "有据智评 · 部署评估";
  return `mailto:${lead.email}?subject=${encodeURIComponent(subject)}`;
}

onMounted(() => load());
</script>

<template>
  <section class="card" data-test="site-leads">
    <div class="card-head">
      <div>
        <h2 class="card-title">官网申请</h2>
        <p class="card-note">官网提交的试用与部署评估申请，仅平台管理员可见；到期自动删除。</p>
      </div>
      <span v-if="page" class="chip" :class="page.new_count ? 'chip-warn' : ''" data-test="site-leads-new-count">
        {{ page.new_count ? `${page.new_count} 条新申请` : "没有新申请" }}
      </span>
    </div>

    <div class="card-pad filters" role="group" aria-label="筛选">
      <button type="button" class="btn btn-sm" :class="{ 'btn-primary': filter === 'new' }" :aria-pressed="filter === 'new'" @click="switchFilter('new')">新申请</button>
      <button type="button" class="btn btn-sm" :class="{ 'btn-primary': filter === 'all' }" :aria-pressed="filter === 'all'" @click="switchFilter('all')">全部</button>
    </div>

    <p v-if="error" class="notice notice-danger card-pad" role="alert">{{ error }}</p>
    <div v-if="page && page.items.length" class="table-wrap">
      <table class="table">
        <thead>
          <tr><th>提交时间</th><th>类型</th><th>联系方式</th><th>备注</th><th>状态</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="lead in page.items" :key="lead.id" :data-lead="lead.id">
            <td class="nowrap">
              {{ formatTime(lead.last_submitted_at) }}
              <div v-if="lead.submit_count > 1" class="faint small">共提交 {{ lead.submit_count }} 次</div>
            </td>
            <td>{{ KIND_LABELS[lead.kind] || lead.kind }}</td>
            <td>
              <a :href="mailtoFor(lead)" class="mono">{{ lead.email }}</a>
              <div v-if="lead.organization_name" class="small">{{ lead.organization_name }}</div>
            </td>
            <td class="note">{{ lead.note || "—" }}</td>
            <td>
              <label class="sr-only" :for="`lead-status-${lead.id}`">状态</label>
              <select
                :id="`lead-status-${lead.id}`"
                class="select"
                :value="lead.status"
                :disabled="busyId === lead.id"
                @change="setStatus(lead, $event.target.value)"
              >
                <option v-for="option in STATUS_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
              </select>
            </td>
            <td>
              <button type="button" class="btn btn-sm btn-danger" :disabled="busyId === lead.id" @click="remove(lead)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else-if="page" class="card-pad faint">{{ filter === "new" ? "没有待处理的新申请。" : "还没有申请。" }}</p>
    <p v-else-if="loading" class="card-pad faint">加载中…</p>

    <div v-if="hasMore" class="card-pad">
      <button type="button" class="btn btn-sm" :disabled="loading" @click="loadMore">加载更多</button>
    </div>
    <div class="card-foot">
      状态说明：{{ STATUS_OPTIONS.map((o) => o.label).join(" → ") }}。改状态与删除都会写审计日志（不记邮箱）。
    </div>
  </section>
</template>

<style scoped>
.filters {
  display: flex;
  gap: 8px;
  padding-top: 0;
}

.nowrap {
  white-space: nowrap;
}

.small {
  font-size: 12.5px;
  margin-top: 2px;
}

.note {
  max-width: 320px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}
</style>
