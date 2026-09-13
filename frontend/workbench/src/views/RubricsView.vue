<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";

import { api, StaleContextError } from "@/api/client.js";
import RuleReviewPanel from "@/components/RuleReviewPanel.vue";
import AtomicRuleEditor from "@/components/AtomicRuleEditor.vue";
import { canPublishRubric, canSubmitReview } from "@/lib/rubric-workflow.js";
import AiRuleDraftPanel from "@/components/AiRuleDraftPanel.vue";
import { useRubricsStore } from "@/stores/rubrics.js";
import { useSessionStore } from "@/stores/session.js";
import { draftRows, rowKey } from "@/lib/ai-draft.js";
import { RouterLink, onBeforeRouteLeave } from "vue-router";

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
const session = useSessionStore();
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
    step.value = 2;
  } catch (err) {
    // 服务端的说明比「导入失败」有用得多：它会指出是文件类型不对还是解析不了。
    importError.value = err instanceof Error ? err.message : "导入失败";
  } finally {
    importBusy.value = false;
  }
}

// --- AI 起草缺失细则（D-027：必须绑自己的连接）--------------------------
const connections = ref([]);
const draftConnection = ref("");
const draftBusy = ref(false);
const draftError = ref(null);

async function loadConnections() {
  try {
    connections.value = (await api.get("/ai-connections")) || [];
  } catch (err) {
    if (!(err instanceof StaleContextError)) connections.value = [];
  }
}

async function generateMissingRules() {
  draftBusy.value = true;
  draftError.value = null;
  try {
    await store.draftRules(selected.value, {
      // 只给缺规则的那些评分项：AI 补缺失部分，用户原文表述保留。
      criteria: JSON.parse(JSON.stringify(generationCandidates.value)),
      connectionId: draftConnection.value || null,
    });
    if (!store.lastDraft.items.some((item) => item.draft)) draftError.value = "所选评分项的原文细则已可解析，请直接核对或修改规则，无需 AI 补全。";
    // 这里**不重新加载完整度**：起草端点 non-persistent，库里什么都没变，刷一次
    // 只会把用户刚拿到的建议换成一模一样的旧数字，看起来像什么都没发生。
    // 建议由下面的确认面板呈现，应用之后才刷新。
  } catch (err) {
    draftError.value = err instanceof Error ? err.message : "起草失败";
  } finally {
    draftBusy.value = false;
  }
}

// --- 确认并应用起草结果（V3-2 闭环）---------------------------------------
const applyBusy = ref(false);
const applyError = ref(null);
const applyNotice = ref(null);

async function applyDraft(excluded) {
  if (operationBusy.value || !editable.value) return;
  const id = selected.value;
  const items = store.lastDraft.items.filter((item) => item.criterion_code === activeCriterion.value?.code);
  const allItems = store.lastDraft.items;
  const appliedKeys = new Set(items.flatMap((item) => draftRows(item.draft)).filter((r) => !excluded.has(rowKey(r))).map(rowKey));
  applyBusy.value = true;
  applyError.value = null;
  applyNotice.value = null;
  let completed = 0;
  try {
    const full = await store.loadRubric(id);
    await store.applyDraftRules(id, {
      criteria: full.criteria || [],
      items,
      excluded,
      // recompile 要继承当前执行草稿；版本重名时服务端自己加 `-draft.N` 后缀。
      supersedesCompilationId: draft.value?.active_compilation?.id || null,
      version: full.version,
      name: full.name,
      totalScore: full.total_score,
    });
    store.lastDraft = { items: allItems.map((item) => ({ ...item, draft: {
      ...item.draft, rule_groups: (item.draft.rule_groups || []).map((group) => ({ ...group,
        rules: group.rules.map((rule, index) => ({ ...rule, draft_index: rule.draft_index ?? index })).filter((rule) =>
          !appliedKeys.has(`${item.criterion_code}::${group.group_code}::${rule.draft_index}`)),
      })).filter((group) => group.rules.length),
    } })).filter((item) => item.draft.rule_groups.length) };
    await refreshDetail(id);
    // Confirm only the rules just reviewed; other rules in the successor need their own review.
    const codes = new Set(items.map((item) => item.criterion_code));
    const newRules = (workspace.value?.rules || []).filter((rule) => {
      const criterion = editForm.value?.criteria.find((c) => c.id === rule.criterion_id);
      if (!criterion || !codes.has(criterion.code)) return false;
      const match = /\.deduct\.(\d+)\.v1$/.exec(rule.rule_code);
      const structured = match ? criterion.deduction_rules_structured[Number(match[1]) - 1] : null;
      return structured && appliedKeys.has(structured.draft_row_key);
    });
    if (newRules.length !== appliedKeys.size) throw new Error("新草稿中的规则与本次确认数量不一致，请重新核对。");
    for (const rule of newRules) {
      await api.post(`/rubrics/${id}/rules/${encodeURIComponent(rule.rule_code)}/confirm`, {
        compilation_id: workspace.value.compilation_id, rule_id: rule.id,
        content_token: rule.content_token, reason: "用户确认并应用 AI 起草条款",
      });
      completed += 1;
    }
    await refreshDetail(id);
    applyNotice.value = `已应用并确认 ${newRules.length} 条建议，生成了新的执行草稿。请核对其余条款；模板尚未发布。`;
  } catch (err) {
    applyError.value = `已确认 ${completed} 条，后续操作已停止：${err instanceof Error ? err.message : "应用失败"}。请核对当前执行草稿后重试。`;
    await refreshDetail(id);
  } finally {
    applyBusy.value = false;
  }
}

function discardDraft() {
  store.lastDraft = { items: store.lastDraft.items.filter((item) => item.criterion_code !== activeCriterion.value?.code) };
  applyNotice.value = null;
}

// --- 校验与发布（D-029：编译产物 + 分享范围一次选定）---------------------
const draft = ref(null);
const publishBusy = ref(false);
const publishError = ref(null);
const chosenCompilation = ref(null);
const chosenVisibility = ref(null);

const canPublish = computed(
  () => canPublishRubric(current.value?.status, draft.value, chosenCompilation.value) && canEdit.value && !scoreFormError.value && !workspace.value?.structural_blockers?.length,
);

async function submitPublish() {
  publishBusy.value = true;
  publishError.value = null;
  try {
    await store.publish(selected.value, {
      compilationId: chosenCompilation.value,
      visibility: chosenVisibility.value,
      reason: "发布已确认模板",
    });
    await refreshDetail();
  } catch (err) {
    publishError.value = err instanceof Error ? err.message : "发布失败";
  } finally {
    publishBusy.value = false;
  }
}

async function cloneRubric(rubric) {
  try {
    const created = await store.clone(rubric.id, {
      name: `${rubric.name}（副本）`,
      version: `${rubric.version}-copy-${Date.now()}`,
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
  complete: { label: "已确认", tone: "chip-ok" },
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

const libraryOpen = ref(false);
const step = ref(2);
const selectedCriterion = ref(null);
const workspace = ref(null);
const reviewBusy = ref(false);
const reviewNotice = ref(null);
const reviewError = ref(null);
const excluded = ref(new Set());
const editForm = ref(null);
const editBaseline = ref("");
const saveBusy = ref(false);
const dirty = computed(() => editForm.value && JSON.stringify(editForm.value) !== editBaseline.value);
const scoreFormError = computed(() => {
  const form = editForm.value;
  if (!form) return null;
  if (!(Number(form.total_score) > 0) || form.criteria.some((c) => !(Number(c.max_score) > 0))) return "总分和各评分项满分必须大于 0。";
  const weights = form.criteria.map((c) => c.weight);
  const sum = form.criteria.reduce((total, c) => total + Number(c.max_score), 0);
  if (weights.every((w) => w == null) && Math.abs(sum - Number(form.total_score)) > 0.000001) return `当前按分值合计评分：评分项满分合计 ${sum} 分，与总分 ${form.total_score} 分不一致。请在第 1 步核对分值；系统不会自动调整。`;
  if (weights.some((w) => w == null) && weights.some((w) => w != null)) return "权重必须全部填写或全部留空。";
  return null;
});
const canEdit = computed(() => !session.authEnforced || session.isPlatformAdmin ||
  ["teacher", "org_admin"].includes(session.organizationRole));
const editable = computed(() => canEdit.value && current.value?.status === "draft" && !dirty.value);
const operationBusy = computed(() => reviewBusy.value || applyBusy.value || saveBusy.value || publishBusy.value || draftBusy.value);
const activeCriterion = computed(() => coverage.value?.criteria.find((c) => c.criterion_id === selectedCriterion.value));
const activeCriterionCode = computed(() => activeCriterion.value?.code);
const currentDraftItems = computed(() => store.lastDraft.items.filter((item) => item.criterion_code === activeCriterionCode.value));
const criterionRules = computed(() => workspace.value?.rules.filter((r) => r.criterion_id === selectedCriterion.value) || []);
const structuralMessages = {
  band_criterion_invalid: '同一评分项必须恰有一条计分分档规则，且不能混用扣分规则。请逐条核对评分方向与生效方式。',
  none_rule_invalid: '不计分规则不能保留分档或计分参数，请核对并清除不适用的字段。',
  global_policy_unsupported: '全局评分政策与当前总分或配置不一致，请核对后保存。',
  weight_policy_invalid: '评分项满分或权重与总分不一致，请在第 1 步核对。',
};
const activeIssues = computed(() => [...(draft.value?.active_compilation?.blockers || []),
  ...(workspace.value?.structural_blockers || []).map(issue => ({ ...issue, message: structuralMessages[issue.code] || issue.message, criterion_code: issue.identity?.criterion_code }))]);
const generationCandidates = computed(() => (editForm.value?.criteria || []).filter((criterion) =>
  !(criterion.deduction_rules_structured || []).length && (
    blocking.value.some((item) => item.code === criterion.code) ||
    (draft.value?.active_compilation?.blockers || []).some((issue) => issue.criterion_code === criterion.code && ['MISSING_EXECUTABLE_SCORING_MODE', 'SEVERITY_CONFIRMATION_REQUIRED', 'DEDUCTION_RULES_MISSING', 'MISSING_DEDUCTION_RULES'].includes(issue.code)))));
const reviewReady = computed(() => !dirty.value && !scoreFormError.value && !activeIssues.value.length && !blocking.value.length && canEdit.value && canSubmitReview(current.value?.status, draft.value));
const editableCriterion = computed(() => editForm.value?.criteria.find((c) => c.code === activeCriterion.value?.code));
let loadSequence = 0;

async function refreshDetail(id = selected.value) {
  if (!id) return;
  const sequence = ++loadSequence;
  loading.value = true;
  error.value = null;
  try {
    const [nextCoverage, nextDraft, nextWorkspace, full] = await Promise.all([
      api.get(`/rubrics/${id}/rule-coverage`), store.loadExecutionDraft(id),
      api.get(`/rubrics/${id}/review-workspace`), store.loadRubric(id),
    ]);
    if (sequence !== loadSequence || id !== selected.value) return;
    if (nextWorkspace.compilation_id !== nextDraft.active_compilation?.id && nextWorkspace.compilation_id) {
      throw new Error("执行草稿已变化，请重新加载后核对。");
    }
    coverage.value = nextCoverage;
    draft.value = nextDraft;
    workspace.value = nextWorkspace;
    chosenCompilation.value = nextDraft.active_compilation?.id || null;
    rubrics.value = rubrics.value.map((r) => r.id === id ? full : r);
    if (!nextCoverage.criteria.some((c) => c.criterion_id === selectedCriterion.value)) {
      selectedCriterion.value = nextCoverage.criteria[0]?.criterion_id || null;
    }
    editForm.value = { name: full.name, version: full.version, description: full.description,
      total_score: full.total_score, criteria: structuredClone(full.criteria || []) };
    if (nextWorkspace.atomic_editing) editForm.value.atomic_rules = nextWorkspace.rules.map((rule) => ({
      id: rule.id, content_token: rule.content_token, criterion_id: rule.criterion_id,
      rule_code: rule.rule_code, changes: Object.fromEntries([
        'name','rule_text','direction','effect_type','max_points','repeat_policy','cap_points',
        'judge_type','checker_key','checker_params','evidence_policy','positive_example',
        'negative_example','boundary_example','strictness','applies_to','mutex_group',
        'depends_on_rule_codes','levels',
      ].map((key) => [key, structuredClone(rule[key])])),
    }));
    editBaseline.value = JSON.stringify(editForm.value);
  } catch (err) {
    if (sequence !== loadSequence || id !== selected.value) return;
    workspace.value = null;
    draft.value = null;
    error.value = err?.message || "加载条款失败，请重试。";
  } finally {
    if (sequence === loadSequence) loading.value = false;
  }
}
function selectRubric(id) {
  if (operationBusy.value) return;
  if ((dirty.value || store.lastDraft.items.length) && !window.confirm("有未保存的修改，放弃修改并切换模板？")) return;
  selected.value = id;
  libraryOpen.value = false;
}
function locateIssue(issue) {
  const target = coverage.value?.criteria.find((c) => c.code === issue.criterion_code);
  if (target) selectedCriterion.value = target.criterion_id;
  step.value = 2;
}
function addDeduction() {
  editableCriterion.value.scoring_mode = "deductive";
  editableCriterion.value.deduction_rules_structured.push({ trigger: "", points: null, severity: "", source: "user_text", confirmed: true });
}
function toggleExcluded(rule) {
  const next = new Set(excluded.value);
  next.has(rule.id) ? next.delete(rule.id) : next.add(rule.id);
  excluded.value = next;
}
async function confirmRules(rules) {
  if (operationBusy.value || !editable.value) return;
  const id = selected.value;
  const compilationId = workspace.value?.compilation_id;
  reviewBusy.value = true;
  reviewError.value = null;
  reviewNotice.value = null;
  let completed = 0;
  try {
    for (const rule of rules) {
      if (rule.status === "approved" || excluded.value.has(rule.id)) continue;
      await api.post(`/rubrics/${id}/rules/${encodeURIComponent(rule.rule_code)}/confirm`, {
        compilation_id: compilationId, rule_id: rule.id, content_token: rule.content_token,
        reason: "用户核对条款内容与来源后确认",
      });
      completed += 1;
    }
    reviewNotice.value = `已确认 ${completed} 条规则，已保存。模板尚未发布。`;
  } catch (err) {
    reviewError.value = `已完成 ${completed} 条，后续操作已停止：${err?.message || "确认失败"}。请核对刷新后的状态再重试。`;
  } finally {
    await refreshDetail(id);
    reviewBusy.value = false;
  }
}
async function submitReview() {
  if (!reviewReady.value || operationBusy.value) return;
  reviewBusy.value = true;
  reviewError.value = null;
  try {
    await api.post(`/rubrics/${selected.value}/submit-review`);
    await refreshDetail();
    step.value = 3;
    reviewNotice.value = "模板已提交审核，请核对发布版本与分享范围。";
  } catch (err) { reviewError.value = err?.message || "提交审核失败"; }
  finally { reviewBusy.value = false; }
}
async function returnToDraft() {
  reviewBusy.value = true;
  try { await api.post(`/rubrics/${selected.value}/return-to-draft`); await refreshDetail(); step.value = 2; }
  catch (err) { reviewError.value = err?.message || "退回草稿失败"; }
  finally { reviewBusy.value = false; }
}
async function confirmLink(link) {
  reviewBusy.value = true;
  reviewError.value = null;
  try {
    await api.post(`/rubrics/${selected.value}/template-links/${link.id}/review`, {
      decision: "confirmed", reason: "用户核对模板映射后确认",
    });
    await refreshDetail();
  } catch (err) { reviewError.value = err?.message || "模板映射确认失败"; }
  finally { reviewBusy.value = false; }
}
async function saveAndValidate() {
  if (!canEdit.value || operationBusy.value || current.value?.status !== "draft") return;
  const invalidField = document.querySelector('.atomic-editor :invalid');
  if (invalidField instanceof HTMLInputElement || invalidField instanceof HTMLTextAreaElement) {
    invalidField.reportValidity();
    reviewError.value = "请先修正原子规则编辑中的无效输入。";
    return;
  }
  reviewError.value = null;
  if (scoreFormError.value) { reviewError.value = scoreFormError.value; step.value = 1; return; }
  saveBusy.value = true;
  try {
    if (dirty.value) {
      const payload = JSON.parse(JSON.stringify(editForm.value));
      for (const criterion of payload.criteria) {
        for (const rule of criterion.deduction_rules_structured || []) {
          if (typeof rule.trigger === "string") rule.match = rule.trigger;
        }
      }
      await api.post(`/rubrics/${selected.value}/recompile`, {
        ...payload, supersedes_compilation_id: draft.value?.active_compilation?.id,
        reason: "用户修改评分标准并保存重新校验",
      });
      excluded.value = new Set();
      store.lastDraft = { items: [] };
    }
    await refreshDetail();
    reviewNotice.value = activeIssues.value.length ? `已保存，仍有 ${activeIssues.value.length} 个校验阻断，请逐项处理。` : "已保存并取得当前校验结果；条款确认与模板发布分别进行。";
  } catch (err) { reviewError.value = err?.message || "保存失败，修改已保留。"; }
  finally { saveBusy.value = false; }
}
watch(selected, async (id) => {
  workspace.value = null; coverage.value = null; draft.value = null;
  selectedCriterion.value = null; chosenVisibility.value = null;
  excluded.value = new Set(); editForm.value = null; reviewError.value = null; reviewNotice.value = null;
  store.lastDraft = { items: [] }; applyNotice.value = null; applyError.value = null;
  step.value = 2;
  await refreshDetail(id);
});
function beforeUnload(event) {
  if (!dirty.value && !store.lastDraft.items.length && !operationBusy.value) return;
  event.preventDefault(); event.returnValue = "";
}
onBeforeRouteLeave(() => {
  if (operationBusy.value) return false;
  if (dirty.value || store.lastDraft.items.length) return window.confirm("有未保存修改或未应用 AI 建议，离开后可能丢失。继续离开？");
});
watch(() => session.organizationId, async (next, previous) => {
  if (next === previous) return;
  loadSequence += 1;
  selected.value = null; rubrics.value = []; workspace.value = null; coverage.value = null;
  draft.value = null; editForm.value = null; store.reset();
  await loadRubrics(); await loadConnections();
});
onMounted(async () => { window.addEventListener("beforeunload", beforeUnload); await loadRubrics(); await loadConnections(); });
onUnmounted(() => { loadSequence += 1; window.removeEventListener("beforeunload", beforeUnload); });
</script>

<template>
  <div>
    <header class="page-head">
      <p class="page-eyebrow">标准与输出 · 评分标准</p>
      <div class="heading-row">
        <div><h1 class="page-title">{{ current?.name || '评分标准' }}</h1>
          <p class="page-sub"><span v-if="current" class="chip chip-warn">{{ draft?.active_compilation?.version?.version || current.version }} · {{ { draft: '草稿', review: '审核中', published: '已发布' }[current.status] || current.status }}</span>
            {{ dirty ? '有未保存的修改 · 当前校验结果来自上一个执行草稿' : '条款确认即时保存；发布后版本不可变。' }}</p>
        </div>
        <div class="head-actions">
          <button class="btn" :disabled="operationBusy" @click="libraryOpen = !libraryOpen">模板库</button>
          <button class="btn" :disabled="operationBusy || !canEdit" @click="importOpen = !importOpen">导入评分模板</button>
          <button v-if="current?.status === 'draft'" class="btn" :disabled="operationBusy || loading || !canEdit" @click="saveAndValidate">{{ saveBusy ? '保存中…' : '保存并重新校验' }}</button>
          <button v-if="current?.status === 'draft'" class="btn btn-primary" :disabled="operationBusy || !reviewReady" @click="submitReview">提交模板审核</button>
        </div>
      </div>
    </header>
    <p v-if="error" class="notice notice-danger" role="alert">{{ error }} <button class="btn btn-sm" @click="refreshDetail()">重新加载</button></p>
    <p v-if="reviewError" class="notice notice-danger" role="alert">{{ reviewError }}</p>
    <p v-if="reviewNotice" class="notice" role="status">{{ reviewNotice }}</p>
    <p v-if="applyError" class="notice notice-danger" role="alert">{{ applyError }}</p>
    <p v-if="applyNotice" class="notice" role="status">{{ applyNotice }}</p>
    <section v-if="importOpen" class="card card-pad import-panel">
      <h2 class="card-title">导入评分模板</h2>
      <p class="card-note">
        上传规则 Excel；可选附带一份带批注的 Word 模板。导入后默认仅自己可见，
        发布时再选择分享范围。
      </p>

      <form @submit.prevent="submitImport">
        <div class="form-grid">
          <label class="field">
            <span class="field-label">标准名称</span>
            <input v-model="importForm.name" class="input" type="text" required />
          </label>
          <label class="field">
            <span class="field-label">版本</span>
            <input v-model="importForm.version" class="input" type="text" required />
          </label>
        </div>

        <label class="field">
          <span class="field-label">规则 Excel（.xlsx / .xlsm）</span>
          <input type="file" accept=".xlsx,.xlsm" required @change="pickRules" />
        </label>

        <label class="field">
          <span class="field-label">Word 模板（可选，用于解析批注）</span>
          <input type="file" accept=".docx" @change="pickTemplate" />
          <span class="field-hint">带批注的模板可以把评语映射到评分项。</span>
        </label>

        <div class="form-actions">
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


    <section v-if="libraryOpen || !current" class="card library-menu">
      <div class="card-head"><h2 class="card-title">模板库</h2></div>
      <button v-for="rubric in rubrics" :key="rubric.id" class="lib-item" :class="{ active: rubric.id === selected }" :disabled="operationBusy" @click="selectRubric(rubric.id)">
        <strong>{{ rubric.name }}</strong> <span class="chip">{{ rubric.version }} · {{ {draft:'草稿',review:'审核中',published:'已发布'}[rubric.status] || rubric.status }}</span> <span class="faint">{{ visibilityLabel(rubric.visibility) }}</span>
      </button>
      <p v-if="!rubrics.length" class="card-pad faint">还没有评分标准。请导入规则 Excel 开始。</p>
    </section>
    <template v-if="current">
      <nav class="card steps" aria-label="评分标准编辑步骤">
        <button v-for="(label, index) in ['基本信息与评分项', '评分规则', '校验与发布']" :key="label" class="step" :class="{ active: step === index + 1 }" :aria-current="step === index + 1 ? 'step' : undefined" @click="step = index + 1"><span class="step-number">{{ index + 1 }}</span>{{ label }}</button>
      </nav>
      <div v-if="activeIssues.length || draft?.ambiguity" class="notice notice-danger blockers" role="alert">
        <strong>存在 {{ activeIssues.length }} 个校验阻断，处理后才能提交审核。</strong>
        <p v-if="draft?.ambiguity">当前存在多个执行草稿，请先恢复为唯一活动版本。</p>
        <div v-for="(issue, index) in activeIssues" :key="index" class="issue-row">
          <span>{{ issue.criterion_code }} {{ issue.message || issue.code || issue }}</span>
          <button v-if="issue.criterion_code" class="btn btn-sm" @click="locateIssue(issue)">去修改 {{ issue.criterion_code }} →</button>
        </div>
      </div>
      <div v-if="blocking.length" class="notice notice-danger">
        <strong>存在 {{ blocking.length }} 个缺少规则的评分项</strong>
        <p>评分到该项时没有判据可用，请补齐细则并重新校验。</p>
        <button v-for="item in blocking" :key="item.criterion_id" class="btn btn-sm" @click="locateIssue({ criterion_code: item.code })">去修改 {{ item.code }} →</button>
      </div>
      <p v-if="scoreFormError && step !== 1" class="notice notice-warn">{{ scoreFormError }}</p>
      <p v-if="loading" class="notice" role="status">正在加载条款与校验结果…</p>
      <div v-else class="editor-layout">
        <aside class="card criteria-nav">
          <div class="card-head"><h2 class="card-title">评分项 · {{ coverage?.total_criteria || 0 }}</h2></div>
          <button v-for="item in coverage?.criteria || []" :key="item.criterion_id" class="lib-item" :class="{ active: item.criterion_id === selectedCriterion, missing: item.status === 'missing' }" :disabled="operationBusy" @click="selectedCriterion = item.criterion_id">
            <div class="criterion-title"><span class="faint mono">{{ item.code }}</span><strong>{{ item.name }}</strong><span class="score">{{ item.max_score }} 分</span></div>
            <p :class="item.status === 'missing' ? 'danger' : item.status === 'pending_review' ? 'warn' : 'faint'">{{ item.rule_count }} 条规则 · {{ STATUS[item.status]?.label || item.status }}</p><p class="faint">原文 {{ item.from_source_count }} · AI {{ item.from_ai_count }}</p>
          </button>
        </aside>
        <div class="detail">
          <section v-if="step === 1 && editForm" class="card card-pad">
            <h2 class="card-title">基本信息与评分项</h2>
            <div class="form-grid">
              <label class="field"><span class="field-label">标准名称</span><input v-model="editForm.name" class="input" :disabled="!canEdit || current.status !== 'draft' || operationBusy" /></label>
              <label class="field"><span class="field-label">版本</span><input v-model="editForm.version" class="input" :disabled="!canEdit || current.status !== 'draft' || operationBusy" /></label>
            </div>
            <label class="field"><span class="field-label">总分</span><input v-model.number="editForm.total_score" type="number" min="0.01" step="0.01" class="input" :disabled="!canEdit || current.status !== 'draft' || operationBusy" /></label>
            <p v-if="scoreFormError" class="notice notice-warn">{{ scoreFormError }}</p>
            <label class="field"><span class="field-label">标准说明</span><textarea v-model="editForm.description" class="input" :disabled="!canEdit || current.status !== 'draft' || operationBusy" /></label>
            <template v-if="editableCriterion">
              <h3>{{ editableCriterion.code }} · {{ editableCriterion.name }}</h3>
              <label class="field"><span class="field-label">评分项名称</span><input v-model="editableCriterion.name" class="input" :disabled="!canEdit || current.status !== 'draft' || operationBusy" /></label>
              <label class="field"><span class="field-label">评分项满分</span><input v-model.number="editableCriterion.max_score" type="number" min="0.01" step="0.01" class="input" :disabled="!canEdit || current.status !== 'draft' || operationBusy" /></label>
              <label class="field"><span class="field-label">评分项说明</span><textarea v-model="editableCriterion.description" class="input" :disabled="!canEdit || current.status !== 'draft' || operationBusy" /></label>
              <p class="faint">满分 {{ editableCriterion.max_score }} 分；总分 {{ editForm.total_score }} 分。修改并保存后需要核对新草稿的条款。</p>
            </template>
          </section>
          <template v-if="step === 2">
            <section class="card card-pad coverage-panel">
              <h2 class="card-title">扣分细则完整度</h2>
              <p class="card-note">{{ coverage?.complete_count || 0 }} 项规则已确认，{{ coverage?.pending_review_count || 0 }} 项有待确认规则，{{ coverage?.blocking_count || 0 }} 项没有规则。</p>
              <p class="faint">缺少规则的评分项数量与执行草稿校验阻断数量分别统计。AI 只补缺失部分，你的原文表述会保留。</p>
              <div v-if="generationCandidates.length" class="draft-box">
                <label class="field"><span class="field-label">用哪个 AI 连接起草</span><select v-model="draftConnection" class="select"><option value="">请选择</option><option v-for="item in connections" :key="item.id" :value="item.id">{{ item.name }} · {{ item.model_name }}</option></select></label>
                <p v-if="!connections.length" class="notice notice-warn">你还没有可用的 AI 连接。请先在 <RouterLink :to="{ name: 'account' }">账户与连接</RouterLink> 绑定一个。</p>
                <button class="btn" :disabled="operationBusy || !draftConnection || !editable" @click="generateMissingRules">{{ draftBusy ? '起草中…' : `生成全部缺失细则（${generationCandidates.length}）` }}</button>
                <p v-if="draftError" class="notice notice-danger" role="alert">{{ draftError }}</p>
              </div>
            </section>
            <RuleReviewPanel v-if="activeCriterion && workspace" :criterion="activeCriterion" :rules="criterionRules" :excluded="excluded" :busy="operationBusy" :editable="editable" @confirm="confirmRules([$event])" @confirm-all="confirmRules" @exclude="toggleExcluded" />
            <AiRuleDraftPanel :items="currentDraftItems" :busy="operationBusy || !editable" @apply="applyDraft" @discard="discardDraft" />
            <AtomicRuleEditor v-if="editForm?.atomic_rules && current.status === 'draft'" :rules="editForm.atomic_rules.filter(r => r.criterion_id === selectedCriterion)" :disabled="!canEdit || operationBusy" />
            <details v-else-if="editableCriterion && current.status === 'draft'" class="card card-pad">
              <summary>修改 {{ activeCriterion?.code }} 的评分细则</summary>
              <p class="faint">保存会生成新的执行草稿；新条款需重新核对确认。</p>
              <label class="field"><span class="field-label">评分方式</span><select v-model="editableCriterion.scoring_mode" class="select" :disabled="!canEdit || operationBusy"><option value="deductive">按细则扣分</option><option value="banded">按档位评分</option><option value="review_only">仅人工复核</option><option v-if="!['deductive','banded','review_only'].includes(editableCriterion.scoring_mode)" :value="editableCriterion.scoring_mode">{{ editableCriterion.scoring_mode }}（当前方式，请核对执行草稿）</option></select></label>
              <label class="field"><span class="field-label">评分说明</span><textarea v-model="editableCriterion.description" class="input" :disabled="!canEdit || operationBusy" /></label>
              <label v-for="(_, index) in editableCriterion.deduction_rules" :key="index" class="field"><span class="field-label">原文细则 {{ index + 1 }}</span><textarea v-model="editableCriterion.deduction_rules[index]" class="input" :disabled="!canEdit || operationBusy" /></label>
              <div v-for="(rule, index) in editableCriterion.deduction_rules_structured" :key="index" class="rule-edit">
                <label class="field"><span class="field-label">触发条件 {{ index + 1 }}</span><input v-model="rule.trigger" class="input" :disabled="!canEdit || operationBusy" /></label>
                <label class="field"><span class="field-label">扣分</span><input v-model.number="rule.points" type="number" min="0" :max="editableCriterion.max_score" class="input" :disabled="!canEdit || operationBusy" /></label>
                <label class="field"><span class="field-label">严重程度</span><select v-model="rule.severity" class="select" :disabled="!canEdit || operationBusy"><option value="">未指定</option><option value="minor">轻微</option><option value="moderate">中等</option><option value="severe">严重</option></select></label>
              </div>
              <button class="btn" :disabled="!canEdit || operationBusy" @click="addDeduction()">添加扣分细则</button>
            </details>
          </template>
          <section v-if="step === 3" class="card card-pad">
            <h2 class="card-title">校验与发布</h2>
            <p class="card-note">发布后版本与分享范围一起冻结；扩大范围需克隆为新版本。</p>
            <label class="field"><span class="field-label">发布哪一份执行草稿</span><select v-model="chosenCompilation" class="select" :disabled="operationBusy"><option :value="null">请选择</option><option v-for="item in draft?.compilations || []" :key="item.id" :value="item.id">{{ item.status === 'validated' ? '校验通过' : item.status === 'blocked' ? '存在阻断' : item.status }} · {{ item.blocker_count }} 个阻断 · {{ item.created_at }}</option></select></label>
            <label class="field"><span class="field-label">分享给谁</span><select v-model="chosenVisibility" class="select" :disabled="operationBusy || current.status === 'published'"><option :value="null">保持当前（{{ visibilityLabel(current.visibility) }}）</option><option value="organization">本组织</option><option value="system" :disabled="!session.isPlatformAdmin">所有人{{ session.isPlatformAdmin ? '' : '（需平台管理员）' }}</option></select></label>
            <div v-if="draft?.active_compilation?.template_links?.length" class="template-links">
              <h3>模板映射核对</h3><p class="faint">规则确认与模板映射确认分别记录，请核对模板来源后操作。</p>
              <div v-for="link in workspace?.template_links || []" :key="link.id" class="issue-row"><div><span>{{ link.rule_code }} · {{ link.review_status === 'confirmed' ? '已确认' : '待核对' }}</span><p>{{ link.section_path?.join(' / ') }} · {{ link.text }}</p><p class="faint">{{ link.rationale }}</p></div><button v-if="link.review_status === 'pending'" class="btn" :disabled="operationBusy || !editable" @click="confirmLink(link)">确认模板映射</button></div>
            </div>
            <p v-if="current.status === 'draft'" class="faint">请先完成条款与模板映射确认，再提交模板审核。</p>
            <p v-if="!canPublish && current.status === 'review'" class="notice notice-warn">当前版本尚不满足发布条件，请核对条款、映射与校验阻断。</p>
            <div class="head-actions">
              <button v-if="current.status === 'review'" class="btn" :disabled="operationBusy || !canEdit" @click="returnToDraft">退回草稿</button>
              <button v-if="current.status !== 'published'" class="btn btn-primary" :disabled="operationBusy || !canPublish || dirty" @click="submitPublish">{{ publishBusy ? '发布中…' : '发布' }}</button>
              <button v-if="current.status === 'published'" class="btn" :disabled="operationBusy || !canEdit" @click="cloneRubric(current)">复制为新版本</button>
            </div>
            <p v-if="publishError" class="notice notice-danger" role="alert">{{ publishError }}</p>
          </section>
        </div>
      </div>
    </template>
  </div>
</template>
<style scoped>
.heading-row, .head-actions, .criterion-title, .issue-row { display: flex; align-items: center; gap: 12px; }
.heading-row, .issue-row { justify-content: space-between; }
.head-actions { flex-wrap: wrap; }
.steps { display: flex; padding: 6px; margin: 20px 0; }
.step { flex: 1; border: 0; background: transparent; padding: 12px 16px; text-align: left; display: flex; align-items: center; gap: 10px; color: #777d78; border-radius: 8px; cursor: pointer; }
.step.active { background: #f0f4f2; color: #185e52; font-weight: 600; }
.step-number { border: 2px solid currentColor; border-radius: 50%; width: 24px; height: 24px; display: inline-flex; align-items: center; justify-content: center; }
.editor-layout { display: grid; grid-template-columns: 270px minmax(0, 1fr); gap: 22px; align-items: start; }
.criteria-nav { overflow: hidden; }
.detail > .card { margin-bottom: 0; }
.detail { min-width: 0; display: flex; flex-direction: column; gap: 20px; }
.lib-item { display: block; width: 100%; border: 0; border-bottom: 1px solid #eeefeb; border-left: 2px solid transparent; background: white; padding: 16px 20px; text-align: left; cursor: pointer; }
.lib-item.active { border-left-color: #185e52; background: #f0f4f2; }
.lib-item.missing { border-left-color: #b83a2d; }
.lib-item p { margin: 6px 0 0; font-size: 13px; }
.criterion-title { gap: 8px; flex-wrap: wrap; }
.score { margin-left: auto; color: #929791; font-size: 13px; }
.library-menu { margin-bottom: 20px; }
.form-grid, .rule-edit { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 16px; }
.rule-edit { grid-template-columns: 2fr 1fr 1fr; }
.field { margin: 14px 0; }
textarea { min-height: 95px; resize: vertical; }
.warn { color: #a86a1b; }.danger { color: #b83a2d; }
.issue-row { margin-top: 10px; }
@media(max-width: 1100px) { .heading-row { align-items: flex-start; flex-direction: column; } .editor-layout { grid-template-columns: 230px minmax(0,1fr); } }
@media(max-width: 760px) { .editor-layout { grid-template-columns: minmax(0,1fr); } .criteria-nav { max-height: 260px; overflow: auto; } .steps { flex-direction: column; } .form-grid,.rule-edit { grid-template-columns: 1fr; } }
</style>
