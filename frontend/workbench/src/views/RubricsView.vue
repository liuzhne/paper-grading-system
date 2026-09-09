<script setup>
import { computed, onMounted, ref, watch } from "vue";

import { api, StaleContextError } from "@/api/client.js";
import { useRubricsStore } from "@/stores/rubrics.js";

/**
 * 评分标准（前端 v2 计划 §2、§6）。
 *
 * 从模板库进入单个标准的详情：基本信息与评分项 / 扣分细则完整度 / 校验与
 * 发布状态。UI 只做重组——不修改授权规则、分值、冻结版本与评分政策。
 *
 * 「阻断项」在这里的含义是**发布后评分时没有判据可用**，所以它排在最前面，
 * 并且直接点得到对应的评分项。
 */
const rubrics = ref([]);

// --- 导入（D-026：标准只能由导入产生，不提供空白新建）---------------------
const store = useRubricsStore();
const importOpen = ref(false);
const importBusy = ref(false);
const importError = ref(null);
const importForm = ref({ name: "", version: "v1.0", description: "" });
const rulesFile = ref(null);
const templateFile = ref(null);

function pickRules(event) {
  rulesFile.value = event.target.files?.[0] || null;
}

function pickTemplate(event) {
  templateFile.value = event.target.files?.[0] || null;
}

async function submitImport() {
  if (!rulesFile.value) {
    importError.value = "请先选择规则 Excel。";
    return;
  }
  importBusy.value = true;
  importError.value = null;
  try {
    const result = await store.importFiles({
      name: importForm.value.name,
      version: importForm.value.version,
      description: importForm.value.description,
      rulesFile: rulesFile.value,
      templateFile: templateFile.value,
    });
    importOpen.value = false;
    await loadRubrics();
    // 直接选中刚导入的那份，省去用户在列表里再找一次。
    if (result.rubric?.id) selected.value = result.rubric.id;
  } catch (err) {
    // 服务端的说明比「导入失败」有用得多：它会指出是文件类型不对还是解析不了。
    importError.value = err instanceof Error ? err.message : "导入失败";
  } finally {
    importBusy.value = false;
  }
}

async function cloneRubric(rubric) {
  try {
    const created = await store.clone(rubric.id, {
      name: `${rubric.name}（副本）`,
      version: "v1.0",
    });
    await loadRubrics();
    selected.value = created.id;
  } catch (err) {
    error.value = err instanceof Error ? err.message : "克隆失败";
  }
}
const selected = ref(null);
const coverage = ref(null);
const error = ref(null);
const loading = ref(false);

const current = computed(
  () => rubrics.value.find((r) => r.id === selected.value) ?? null,
);

const blocking = computed(
  () => coverage.value?.criteria.filter((c) => c.status === "missing") ?? [],
);
const pending = computed(
  () => coverage.value?.criteria.filter((c) => c.status === "pending_review") ?? [],
);

const STATUS = {
  complete: { label: "已按原文解析", tone: "chip-ok" },
  pending_review: { label: "待确认", tone: "chip-warn" },
  missing: { label: "缺少扣分细则 · 阻断", tone: "chip-danger" },
};

function visibilityLabel(value) {
  return { private: "仅自己", organization: "当前组织", system: "全平台" }[value] || value;
}

async function loadRubrics() {
  try {
    rubrics.value = (await api.get("/rubrics")) || [];
    selected.value = selected.value ?? rubrics.value[0]?.id ?? null;
  } catch (err) {
    if (!(err instanceof StaleContextError)) error.value = err?.message || "加载失败";
  }
}

async function loadCoverage(id) {
  if (!id) {
    coverage.value = null;
    return;
  }
  loading.value = true;
  error.value = null;
  try {
    coverage.value = await api.get(`/rubrics/${id}/rule-coverage`);
  } catch (err) {
    if (!(err instanceof StaleContextError)) {
      coverage.value = null;
      error.value = err?.message || "加载扣分细则完整度失败";
    }
  } finally {
    loading.value = false;
  }
}

watch(selected, (id) => loadCoverage(id));
onMounted(async () => {
  await loadRubrics();
  await loadCoverage(selected.value);
});
</script>

<template>
  <div>
    <header class="page-head">
      <p class="page-eyebrow">标准与输出</p>
      <h1 class="page-title">评分标准</h1>
      <p class="page-sub">评分标准经审核发布后不可变；批次锁定的是某个已发布版本。</p>
    </header>

    <p v-if="error" class="notice notice-danger" role="alert">{{ error }}</p>

    <section v-if="importOpen" class="card card-pad import-panel">
      <h2 class="card-title">导入评分模板</h2>
      <p class="card-note">
        上传规则 Excel；可选附带一份带批注的 Word 模板。导入后默认仅自己可见，
        发布时再选择分享范围。
      </p>

      <form @submit.prevent="submitImport">
        <label for="imp-name">标准名称</label>
        <input id="imp-name" v-model="importForm.name" type="text" required />

        <label for="imp-version">版本</label>
        <input id="imp-version" v-model="importForm.version" type="text" required />

        <label for="imp-rules">规则 Excel（.xlsx / .xlsm）</label>
        <input id="imp-rules" type="file" accept=".xlsx,.xlsm" required @change="pickRules" />

        <label for="imp-template">Word 模板（可选，用于解析批注）</label>
        <input id="imp-template" type="file" accept=".docx" @change="pickTemplate" />

        <div class="row-actions">
          <button class="btn btn-primary" type="submit" :disabled="importBusy">
            {{ importBusy ? "导入中…" : "导入" }}
          </button>
          <button class="btn" type="button" @click="importOpen = false">取消</button>
        </div>
      </form>

      <p v-if="importError" class="notice notice-danger" role="alert">{{ importError }}</p>
    </section>

    <!-- 导入结果如实展示：warnings 是「你的 Excel 里哪几条没被识别」的唯一出口，
         吞掉它用户会以为全都导进去了，直到评分时才发现某项没有判据。 -->
    <section v-if="store.lastImport.rubricId" class="card card-pad">
      <h2 class="card-title">上次导入结果</h2>
      <p v-if="!store.lastImport.warnings.length" class="faint">没有警告。</p>
      <ul v-else class="warnings">
        <li v-for="(warning, index) in store.lastImport.warnings" :key="index">{{ warning }}</li>
      </ul>
      <dl v-if="store.lastImport.templateSummary" class="summary">
        <div v-for="(value, key) in store.lastImport.templateSummary" :key="key">
          <dt>{{ key }}</dt>
          <dd class="mono">{{ value }}</dd>
        </div>
      </dl>
    </section>

    <div class="layout">
      <!-- 模板库 -->
      <aside class="card library">
        <div class="card-head">
          <h2 class="card-title">模板库</h2>
          <button class="btn btn-sm btn-primary" type="button" @click="importOpen = !importOpen">
            导入评分模板
          </button>
        </div>
        <button
          v-for="rubric in rubrics"
          :key="rubric.id"
          class="lib-item"
          :class="{ active: rubric.id === selected }"
          type="button"
          @click="selected = rubric.id"
        >
          <div class="lib-name">{{ rubric.name }}</div>
          <div class="lib-meta">
            <span class="chip" :class="rubric.status === 'published' ? 'chip-ok' : 'chip-warn'">
              {{ rubric.version }} {{ rubric.status === "published" ? "已发布" : rubric.status }}
            </span>
            <span class="faint">{{ visibilityLabel(rubric.visibility) }}</span>
          </div>
        </button>
        <p v-if="!rubrics.length" class="card-pad faint">
          还没有评分标准。评分标准由导入规则 Excel 产生。
        </p>
      </aside>

      <div class="detail">
        <template v-if="current">
          <!-- 阻断项优先 -->
          <div v-if="blocking.length" class="notice notice-danger blockers">
            <p class="notice-title">
              存在 {{ blocking.length }} 个阻断项，处理后才能提交审核
            </p>
            <p>
              这些评分项没有可执行的扣分细则；评分到该项时没有判据可用。
            </p>
            <ul class="blocker-list">
              <li v-for="item in blocking" :key="item.criterion_id">
                <span class="mono">{{ item.code }}</span> {{ item.name }}
              </li>
            </ul>
          </div>

          <!-- 完整度 -->
          <section class="card card-pad">
            <h2 class="card-title">扣分细则完整度</h2>
            <p v-if="coverage" class="card-note">
              {{ coverage.complete_count }} 项已按原文解析完成，
              {{ coverage.pending_review_count }} 项有待确认规则，
              {{ coverage.blocking_count }} 项没有规则。
            </p>
            <dl v-if="coverage" class="cov">
              <div><dt>评分项</dt><dd class="mono">{{ coverage.total_criteria }}</dd></div>
              <div><dt>已完成</dt><dd class="mono">{{ coverage.complete_count }}</dd></div>
              <div><dt>待确认</dt><dd class="mono warn">{{ coverage.pending_review_count }}</dd></div>
              <div><dt>阻断</dt><dd class="mono danger">{{ coverage.blocking_count }}</dd></div>
            </dl>
            <p v-if="pending.length" class="faint field-hint">
              未确认的 AI 规则不会进入可执行评分版本；确认信息会记录确认人、时间与生成来源。
            </p>
          </section>

          <!-- 评分项 -->
          <section class="card">
            <div class="card-head">
              <h2 class="card-title">评分项</h2>
              <span class="faint mono">{{ current.version }}</span>
            </div>
            <div class="table-wrap">
              <table class="table">
                <thead>
                  <tr>
                    <th>编号</th>
                    <th>名称</th>
                    <th>满分</th>
                    <th>规则来源</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="item in coverage?.criteria || []" :key="item.criterion_id">
                    <td class="mono">{{ item.code }}</td>
                    <td>{{ item.name }}</td>
                    <td class="num">{{ item.max_score }}</td>
                    <td class="muted source">
                      <template v-if="item.rule_count">
                        原文 <b class="mono">{{ item.from_source_count }}</b>
                        · AI <b class="mono">{{ item.from_ai_count }}</b>
                        <span v-if="item.ai_pending_count" class="faint">
                          （{{ item.ai_pending_count }} 条待确认）
                        </span>
                      </template>
                      <span v-else class="faint">—</span>
                    </td>
                    <td>
                      <span class="chip" :class="STATUS[item.status].tone">
                        {{ STATUS[item.status].label }}
                      </span>
                    </td>
                  </tr>
                  <tr v-if="!coverage?.criteria.length && !loading">
                    <td class="table-empty" colspan="5">该标准还没有评分项。</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="card-foot">
              本页只展示标准结构与规则来源。导入、AI 起草与逐条审核仍在
              <a href="/">旧版模板中心</a> 完成——那部分生命周期端点未改动，
              尚未迁移到新界面。
            </div>
          </section>
        </template>
        <p v-else-if="!rubrics.length" class="notice">
          还没有评分标准。请先在旧版模板中心导入。
        </p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  gap: 20px;
  align-items: flex-start;
  flex-wrap: wrap;
}

.library {
  flex: 1 1 260px;
}

.detail {
  flex: 999 1 460px;
  min-width: 0;
}

.lib-item {
  display: block;
  width: 100%;
  text-align: left;
  border: 0;
  border-top: 1px solid var(--border-row);
  border-left: 2px solid transparent;
  background: var(--surface);
  padding: 12px 18px;
  cursor: pointer;
}

.lib-item:hover {
  background: var(--surface-muted);
}

.lib-item.active {
  background: var(--accent-surface);
  border-left-color: var(--accent);
}

.lib-name {
  font-size: 13.5px;
  font-weight: 550;
  margin-bottom: 6px;
}

.lib-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  flex-wrap: wrap;
}

.blockers {
  margin-bottom: 20px;
}

.blocker-list {
  margin: 8px 0 0;
  padding-left: 20px;
  line-height: 1.9;
}

.cov {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 16px;
  margin: 16px 0 0;
}

.cov dt {
  font-size: 12.5px;
  color: var(--text-muted);
  margin-bottom: 6px;
}

.cov dd {
  margin: 0;
  font-size: 20px;
  font-weight: 500;
}

.cov dd.warn {
  color: var(--warn-ink);
}

.cov dd.danger {
  color: var(--danger);
}

.source {
  font-size: 12.5px;
}
</style>
