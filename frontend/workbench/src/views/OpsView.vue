<script setup>
import { computed, onMounted, ref } from "vue";

import { api, ApiError, StaleContextError } from "@/api/client.js";
import { useSessionStore } from "@/stores/session.js";

/**
 * 运维与质量（前端 v2 计划 §2.1、§10）。
 *
 * 设计稿没有画这一页。它把原先散落在旧 SPA 三处的内容集中起来：dashboard 的
 * 运行环境面板、batches 页的观察区、rubrics 页的校准锚点。
 *
 * 两条边界必须在界面上可见：
 * - **组织视图与平台视图分开。** 组织管理员看不到磁盘、数据库容量、部署安全
 *   这些宿主事实——它们是平台的属性，不是某个组织的。
 * - **只做观察，不授予发布权限。** 这一页不提供 GATE-03 批准入口，也不预填
 *   生产阈值。
 */
const session = useSessionStore();

const orgReadiness = ref(null);
const platformReadiness = ref(null);
const integrations = ref(null);
const errors = ref({});

const canOrg = computed(() => session.can("view_organization_ops"));
const canPlatform = computed(() => session.can("view_platform_ops"));

async function load(key, path, target) {
  try {
    target.value = await api.get(path);
  } catch (err) {
    if (err instanceof StaleContextError) return;
    target.value = null;
    errors.value = {
      ...errors.value,
      [key]:
        err instanceof ApiError && err.status === 403
          ? "当前角色无权查看该视图。"
          : err?.message || "加载失败",
    };
  }
}

function signalTone(status) {
  return { pass: "chip-ok", fail: "chip-danger", unavailable: "chip-warn" }[status] || "";
}

onMounted(async () => {
  await session.loadCapabilities();
  const jobs = [load("integrations", "/system/integrations", integrations)];
  if (canOrg.value) {
    jobs.push(load("org", "/system/organization-readiness", orgReadiness));
  }
  if (canPlatform.value) {
    jobs.push(load("platform", "/system/ops-readiness", platformReadiness));
  }
  await Promise.all(jobs);
});
</script>

<template>
  <div>
    <header class="page-head">
      <p class="page-eyebrow">运维</p>
      <h1 class="page-title">运维与质量</h1>
      <p class="page-sub">
        观察与配置录入。本页不提供发布批准入口，也不预填生产阈值。
      </p>
    </header>

    <!-- 组织视图 -->
    <section v-if="canOrg" class="card">
      <div class="card-head">
        <div>
          <h2 class="card-title">当前组织</h2>
          <p class="card-note">
            仅本组织的批任务指标。磁盘、数据库容量与部署安全属于宿主事实，不在此列。
          </p>
        </div>
        <span class="chip">{{ session.organization?.name || "—" }}</span>
      </div>

      <p v-if="errors.org" class="notice notice-danger card-pad">{{ errors.org }}</p>
      <div v-else-if="orgReadiness" class="card-pad">
        <dl class="signals">
          <div>
            <dt>活动批任务</dt>
            <dd class="mono">{{ orgReadiness.signals.batch_jobs.active_count }}</dd>
          </div>
          <div>
            <dt>心跳超时</dt>
            <dd class="mono" :class="{ danger: orgReadiness.signals.batch_jobs.stale_count }">
              {{ orgReadiness.signals.batch_jobs.stale_count }}
            </dd>
          </div>
          <div>
            <dt>LLM 失败率</dt>
            <dd>
              <span class="chip" :class="signalTone(orgReadiness.signals.batch_jobs.llm_failure_rate.status)">
                {{ orgReadiness.signals.batch_jobs.llm_failure_rate.maximum_observed ?? "无样本" }}
              </span>
              <span class="faint mono">
                 / 上限 {{ orgReadiness.signals.batch_jobs.llm_failure_rate.maximum_allowed }}
              </span>
            </dd>
          </div>
        </dl>
        <p class="faint field-hint">
          阈值由部署责任人批准并写入配置；本页只展示，不提供修改入口。
        </p>
      </div>
      <p v-else class="card-pad faint">加载中…</p>
    </section>

    <!-- 平台视图 -->
    <section v-if="canPlatform" class="card">
      <div class="card-head">
        <div>
          <h2 class="card-title">平台运行状况</h2>
          <p class="card-note">宿主级事实，仅平台管理员可见。</p>
        </div>
        <span
          v-if="platformReadiness"
          class="chip"
          :class="platformReadiness.ready_for_production ? 'chip-ok' : 'chip-warn'"
        >
          {{ platformReadiness.ready_for_production ? "就绪" : "有未通过项" }}
        </span>
      </div>

      <p v-if="errors.platform" class="notice notice-danger card-pad">{{ errors.platform }}</p>
      <div v-else-if="platformReadiness" class="table-wrap">
        <table class="table">
          <thead>
            <tr><th>信号</th><th>状态</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="(signal, name) in platformReadiness.signals" :key="name">
              <td class="mono">{{ name }}</td>
              <td><span class="chip" :class="signalTone(signal.status)">{{ signal.status }}</span></td>
              <td class="muted detail">
                <template v-if="name === 'disk'">剩余 {{ signal.free_gb }} GB / 下限 {{ signal.minimum_free_gb }}</template>
                <template v-else-if="name === 'database'">{{ signal.dialect }} · {{ signal.size_gb ?? "—" }} GB / 上限 {{ signal.maximum_size_gb }}</template>
                <template v-else-if="name === 'batch_jobs'">活动 {{ signal.active_count }} · 超时 {{ signal.stale_count }}</template>
                <template v-else-if="name === 'security'">
                  {{ signal.issue_codes.length ? signal.issue_codes.join("、") : "无问题" }}
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="card-pad faint">加载中…</p>

      <div class="card-foot">
        运行就绪是 GATE-03 的必要条件，但
        <b>production_default_switch_authorized 始终为 false</b>——本页不授予
        Core 默认切换权限。
      </div>
    </section>

    <!-- 集成状态：登录后可见的最小投影 -->
    <section class="card">
      <div class="card-head">
        <h2 class="card-title">集成与部署</h2>
      </div>
      <div v-if="integrations" class="card-pad">
        <dl class="signals">
          <div>
            <dt>评分模型</dt>
            <dd>
              <span class="chip" :class="integrations.llm.active ? 'chip-ok' : ''">
                {{ integrations.llm.active ? "真实 provider" : "Mock" }}
              </span>
            </dd>
          </div>
          <div>
            <dt>离线模式</dt>
            <dd><span class="chip">{{ integrations.offline_ready ? "就绪" : "否" }}</span></dd>
          </div>
          <div>
            <dt>在线表格</dt>
            <dd>
              <span class="chip" :class="integrations.sheets.active ? 'chip-ok' : ''">
                {{ integrations.sheets.active ? "已启用" : "Mock / 禁用" }}
              </span>
            </dd>
          </div>
          <div>
            <dt>存储</dt>
            <dd class="mono">{{ integrations.storage.provider }}</dd>
          </div>
        </dl>
      </div>
      <p v-else class="card-pad faint">加载中…</p>
    </section>

    <p v-if="!canOrg && !canPlatform" class="notice">
      当前角色没有运维视图权限。若需要，请联系组织或平台管理员。
    </p>
  </div>
</template>

<style scoped>
.signals {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 18px;
  margin: 0 0 14px;
}

.signals dt {
  font-size: 12.5px;
  color: var(--text-muted);
  margin-bottom: 6px;
}

.signals dd {
  margin: 0;
  font-size: 14px;
  font-weight: 550;
}

.signals dd.danger {
  color: var(--danger);
}

.detail {
  font-size: 12.5px;
}
</style>
