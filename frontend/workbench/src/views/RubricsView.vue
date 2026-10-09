<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, toRaw, watch } from "vue";

import { classifyInBatches } from "@/lib/classification-batches.js";
import { api, currentContextVersion, StaleContextError } from "@/api/client.js";
import RuleReviewPanel from "@/components/RuleReviewPanel.vue";
import AtomicRuleEditor from "@/components/AtomicRuleEditor.vue";
import { canPublishRubric, canSubmitReview, compilationReady, isScoringCompletenessIssue } from "@/lib/rubric-workflow.js";
import AiRuleDraftPanel from "@/components/AiRuleDraftPanel.vue";
import ParseCoveragePanel from "@/components/ParseCoveragePanel.vue";
import RuleAuditPanel from "@/components/RuleAuditPanel.vue";
import StructureSuggestionPanel from "@/components/StructureSuggestionPanel.vue";
import SourceReviewPanel from '@/components/SourceReviewPanel.vue';
import TableRecognitionPanel from '@/components/TableRecognitionPanel.vue';
import RubricImportWorkspace from "@/components/RubricImportWorkspace.vue";
import { importFilesError, stepOneGate } from "@/lib/parse-coverage.js";
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
 * 第一步统一文件来源与评分项表格；规则完整度在第二步、发布校验在第三步。
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
const sourcePreview = ref(null);
const reuploadPreview = ref(null);
const pendingReupload = ref({ rulesFile: null, templateFile: null });
const rubricReupload = ref({ preview: null, rulesFile: null, templateFile: null });
const sourceWorkspace = ref(null);
const nextEntryStep = ref(1);
const pendingScoreChecks = ref(0);
const importing = computed(() => importOpen.value || Boolean(store.activeImportSession));
const initialImport = computed(() => ({ ...importForm.value, total_score: 0, criteria: [] }));
const stepOneSession = computed(() => ({
  ...sourceWorkspace.value,
  ...editForm.value,
  criteria: (editForm.value?.criteria || []).map(item => ({
    ...(sourceWorkspace.value?.criteria || []).find(source => source.code === item.code),
    ...item,
  })),
}));

function beginImport() {
  if (operationBusy.value || importBusy.value) return;
  if (dirty.value && !window.confirm("有未保存的修改，放弃修改并导入新的评分标准？")) return;
  importOpen.value = true;
  libraryOpen.value = false;
  importError.value = null;
  error.value = null; reviewError.value = null; reviewNotice.value = null;
  applyError.value = null; applyNotice.value = null; parseError.value = null;
  sourcePreview.value = null;
  rulesFile.value = null;
  templateFile.value = null;
  importForm.value = { name: "", version: "v1.0", description: "" };
}

function updateFirstStep(changes) {
  if (importOpen.value && !store.activeImportSession) Object.assign(importForm.value, changes);
  else if (editForm.value) Object.assign(editForm.value, changes);
}

function updateExistingCriterion({ index, field, value }) {
  if (editForm.value?.criteria[index]) editForm.value.criteria[index][field] = value;
}

function previewExistingSource(document) {
  sourcePreview.value = { document, items: sourceWorkspace.value?.previews?.[document] || [] };
}

async function confirmFirstStep() {
  if (current.value?.status === "draft" && canEdit.value) {
    if (!(await saveAndValidate())) return;
  }
  await goStep(2);
}

async function pickRules(event) {
  const file = event.target.files?.[0] || null;
  if (!store.activeImportSession || !file) { rulesFile.value = file; return; }
  await previewImportReupload({ rulesFile: file, templateFile: null });
}

async function pickTemplate(event) {
  const file = event.target.files?.[0] || null;
  if (!store.activeImportSession || !file) { templateFile.value = file; return; }
  await previewImportReupload({ rulesFile: null, templateFile: file });
}

async function previewImportReupload(files) {
  importBusy.value = true;
  importError.value = null;
  try {
    reuploadPreview.value = await store.previewImportReupload(files);
    pendingReupload.value = files;
  } catch (err) {
    importError.value = err instanceof Error ? err.message : "重新上传解析失败";
  } finally { importBusy.value = false; }
}

async function confirmImportReupload() {
  if (!reuploadPreview.value) return;
  importBusy.value = true;
  importError.value = null;
  try {
    await store.confirmImportReupload({
      ...pendingReupload.value,
      fingerprint: reuploadPreview.value.fingerprint,
    });
    if (pendingReupload.value.rulesFile) rulesFile.value = pendingReupload.value.rulesFile;
    if (pendingReupload.value.templateFile) templateFile.value = pendingReupload.value.templateFile;
    reuploadPreview.value = null;
    pendingReupload.value = { rulesFile: null, templateFile: null };
    sourcePreview.value = null;
  } catch (err) {
    importError.value = err instanceof Error ? err.message : "替换导入草稿失败";
  } finally { importBusy.value = false; }
}

function cancelImportReupload() {
  reuploadPreview.value = null;
  pendingReupload.value = { rulesFile: null, templateFile: null };
}

async function previewImportSource(document) {
  importBusy.value = true;
  importError.value = null;
  try { sourcePreview.value = await store.previewImportSource(document); }
  catch (err) { importError.value = err instanceof Error ? err.message : "加载原文预览失败"; }
  finally { importBusy.value = false; }
}

async function resolveImportConflict({ conflict, decision }) {
  importBusy.value = true;
  importError.value = null;
  try {
    await store.resolveImportConflict(
      conflict.anchor_unit_id,
      decision,
      decision === "use_excel" ? "用户确认以 Excel 为准" : "用户确认采用 Word 内容",
    );
  } catch (err) {
    importError.value = err instanceof Error ? err.message : "处理来源冲突失败";
  } finally { importBusy.value = false; }
}

async function previewRubricReupload(kind, event) {
  const file = event.target.files?.[0] || null;
  if (!file || !selected.value) return;
  if (dirty.value) {
    importError.value = "请先保存当前评分项修改，再重新上传文件。";
    return;
  }
  const files = {
    rulesFile: kind === "rules" ? file : null,
    templateFile: kind === "template" ? file : null,
  };
  importBusy.value = true;
  importError.value = null;
  try {
    const rubricId = selected.value;
    const preview = await store.previewRubricReupload(rubricId, files);
    if (rubricId !== selected.value) return;
    rubricReupload.value = { preview, ...files };
  } catch (err) {
    importError.value = err instanceof Error ? err.message : "重新上传评分模板失败";
  } finally { importBusy.value = false; }
}

async function confirmRubricReupload() {
  if (!selected.value || !rubricReupload.value.preview) return;
  importBusy.value = true;
  importError.value = null;
  try {
    await store.confirmRubricReupload(selected.value, {
      fingerprint: rubricReupload.value.preview.fingerprint,
      rulesFile: rubricReupload.value.rulesFile,
      templateFile: rubricReupload.value.templateFile,
    });
    rubricReupload.value = { preview: null, rulesFile: null, templateFile: null };
    sourcePreview.value = null;
    await refreshDetail(selected.value);
  } catch (err) {
    importError.value = err instanceof Error ? err.message : "生成后继执行草稿失败";
  } finally { importBusy.value = false; }
}

// 表格结构识别失败（E1/E7）时的 AI 预检：先估算、确认后才调用模型，结果由用户确认后再导入。
const importStructure = ref({ available: false, estimate: null, result: null });

async function submitImport(structureOverride = null) {
  const invalid = importFilesError(rulesFile.value, templateFile.value);
  if (invalid) {
    importError.value = invalid;
    return;
  }
  importBusy.value = true;
  importError.value = null;
  try {
    await store.createImportSession({
      name: importForm.value.name,
      version: importForm.value.version,
      description: importForm.value.description,
      rulesFile: rulesFile.value,
      templateFile: templateFile.value,
      structureOverride,
    });
    importOpen.value = false;
    importStructure.value = { available: false, estimate: null, result: null };
    sourcePreview.value = null;
    reuploadPreview.value = null;
  } catch (err) {
    // 服务端的说明比「导入失败」有用得多：它会指出是文件类型不对还是解析不了。
    importError.value = err instanceof Error ? err.message : "导入失败";
    importStructure.value = { available: /未解析到有效评分项/.test(importError.value), estimate: null, result: null };
  } finally {
    importBusy.value = false;
  }
}

async function updateImportSession(changes) {
  importBusy.value = true;
  importError.value = null;
  try { await store.updateImportSession(changes); }
  catch (err) { importError.value = err instanceof Error ? err.message : "保存导入草稿失败"; }
  finally { importBusy.value = false; }
}

function updateImportCriterion({ index, field, value }) {
  const criteria = structuredClone(toRaw(store.activeImportSession?.criteria || []));
  criteria[index] = { ...criteria[index], [field]: value };
  return updateImportSession({ criteria });
}

function addImportCriterion() {
  const criteria = structuredClone(toRaw(store.activeImportSession?.criteria || []));
  const used = new Set(criteria.map((item) => item.code));
  let ordinal = criteria.length + 1;
  let code = `C${String(ordinal).padStart(2, "0")}`;
  while (used.has(code)) { ordinal += 1; code = `C${String(ordinal).padStart(2, "0")}`; }
  criteria.push({ code, name: "新评分项", max_score: 1, description: "", display_order: criteria.length,
    source_refs: [], parse_status: "manual", deleted: false });
  return updateImportSession({ criteria });
}

function deleteImportCriterion(index) {
  const criteria = structuredClone(toRaw(store.activeImportSession?.criteria || []));
  criteria[index] = { ...criteria[index], deleted: true };
  return updateImportSession({ criteria });
}

async function confirmImportSession() {
  if (importBusy.value || !store.activeImportSession?.id) return;
  importBusy.value = true;
  importError.value = null;
  try {
    // 同一会话只有一个确认动作；响应丢失后的重试必须复用收据键。
    const key = `confirm:${store.activeImportSession.id}`;
    const result = await store.confirmImportSession(key);
    nextEntryStep.value = 2;
    rubrics.value = [...rubrics.value.filter(item => item.id !== result.rubric.id), result.rubric];
    selected.value = result.rubric.id;
  } catch (err) {
    importError.value = err instanceof Error ? err.message : "确认评分项失败";
  } finally { importBusy.value = false; }
}

async function cancelImportSession() {
  if (!window.confirm("取消后将退出当前导入草稿。确定继续？")) return;
  importBusy.value = true;
  importError.value = null;
  try {
    await store.cancelImportSession();
    rulesFile.value = null;
    templateFile.value = null;
    sourcePreview.value = null;
    cancelImportReupload();
  } catch (err) {
    importError.value = err instanceof Error ? err.message : "取消导入失败";
  } finally { importBusy.value = false; }
}

async function previewImportStructure(dryRun) {
  importBusy.value = true;
  importError.value = null;
  try {
    const result = await store.previewImportStructure({
      rulesFile: rulesFile.value, templateFile: templateFile.value,
      connectionId: draftConnection.value || null, dryRun,
    });
    importStructure.value = dryRun
      ? { ...importStructure.value, estimate: result.estimate }
      : { ...importStructure.value, result };
  } catch (err) {
    importError.value = err instanceof Error ? err.message : "结构识别失败";
  } finally {
    importBusy.value = false;
  }
}

// --- 原文识别情况（解析台账）与 AI 兜底（解析重构方案 §8）---------------------
const parseState = ref(null);
const parseBusy = ref(false);
const classificationProgress = ref(null);
const parseError = ref(null);
const structureEstimate = ref(null);
const ruleReview = ref({ reviewed: false });
const ruleEstimate = ref(null);
const gate = computed(() => stepOneGate(parseState.value));

async function parseAction(action) {
  if (!selected.value) return;
  parseBusy.value = true;
  parseError.value = null;
  try {
    await action(selected.value);
  } catch (err) {
    if (!(err instanceof StaleContextError)) parseError.value = err instanceof Error ? err.message : "操作失败";
  } finally {
    parseBusy.value = false;
  }
}

const resolveUnits = (payload) => parseAction(async (id) => {
  await store.resolveUnits(id, payload);
  store.lastDraft = { items: [] };
  parseState.value = await store.loadParseCoverage(id);
  sourceWorkspace.value = await api.get(`/rubrics/${id}/source-workspace`);
});
const acceptSuggestions = (actions) => parseAction(async (id) => {
  const groups = new Map();
  for (const action of actions) {
    const key = `${action.action}:${action.criterionCode || ''}`;
    if (!groups.has(key)) groups.set(key, { ...action, unitIds: [] });
    groups.get(key).unitIds.push(...action.unitIds);
  }
  try { for (const action of groups.values()) await store.resolveUnits(id, action); }
  finally { store.lastDraft = { items: [] }; parseState.value = await store.loadParseCoverage(id); sourceWorkspace.value = await api.get(`/rubrics/${id}/source-workspace`); }
});
const classifyUnits = ({ unitIds }) => parseAction(async (id) => {
  const contextVersion = currentContextVersion();
  const connectionId = draftConnection.value || null;
  const isCurrent = () => selected.value === id && currentContextVersion() === contextVersion;
  // 厂商按 Key 限并发（免费档常见只允许 1 个），超出的请求会被 429 拒绝。
  const declared = connections.value.find(item => item.id === connectionId)?.provider_options?.max_concurrency;
  try {
    await classifyInBatches(unitIds, {
      concurrency: declared || 3,
      request: batch => store.classifyUnits(id, { unitIds: batch, connectionId }),
      isCurrent,
      onResult: result => { parseState.value = { ...parseState.value, unit_classifications: result }; },
      onProgress: progress => { classificationProgress.value = progress; },
    });
  } finally {
    if (isCurrent()) {
      if (classificationProgress.value) classificationProgress.value = { ...classificationProgress.value, running: false };
      const latest = await store.loadParseCoverage(id);
      if (isCurrent()) parseState.value = latest;
    }
  }
});
const suggestStructure = ({ dryRun }) => parseAction(async (id) => {
  const result = await store.suggestStructure(id, { connectionId: draftConnection.value || null, dryRun });
  if (dryRun) { structureEstimate.value = result.estimate; return; }
  structureEstimate.value = null;
  parseState.value = await store.loadParseCoverage(id);
});
const mergeStructure = (payload) => parseAction(async (id) => {
  await store.mergeStructure(id, { ...payload, fingerprint: parseState.value?.structure_suggestions?.fingerprint,
    reason: "确认合入 AI 识别的表格结构" });
  await refreshDetail(id);
});
const undoStructure = () => parseAction(async (id) => {
  await store.undoStructure(id, "撤销 AI 结构识别的合入");
  await refreshDetail(id);
});
const estimateReview = ({ scope }) => parseAction(async (id) => {
  ruleEstimate.value = await store.runRuleReview(id, { connectionId: null, scope, dryRun: true });
});
const runReview = ({ scope }) => parseAction(async (id) => {
  await store.runRuleReview(id, { connectionId: draftConnection.value || null, scope });
  ruleEstimate.value = null;
  ruleReview.value = await store.loadRuleReview(id);
});
const dismissFinding = ({ id: findingId, reason }) => parseAction(async (id) => {
  await store.dismissFinding(id, findingId, reason);
  ruleReview.value = await store.loadRuleReview(id);
});

/** 第一步核对评分项与来源冲突，第二步归类原文并确认评分规则。 */
async function goStep(target) {
  if (operationBusy.value || importBusy.value || loading.value) return;
  if (!canEdit.value || current.value?.status !== "draft") {
    step.value = target;
    return;
  }
  if (target > 1 && pendingScoreChecks.value) {
    parseError.value = `请先核对 ${pendingScoreChecks.value} 个从文字提取的分值。`;
    return;
  }
  if (target > 1 && rubricReupload.value.preview) {
    importError.value = "请先确认或取消本次文件替换，再进入下一步。";
    return;
  }
  if (target > 1 && step.value === 1 && dirty.value && !(await saveAndValidate())) return;
  if (target > 1 && scoreFormError.value) {
    reviewError.value = scoreFormError.value;
    step.value = 1;
    return;
  }
  if (target > 1 && gate.value.conflicts) {
    step.value = 1;
    parseError.value = `请先在第 1 步处理 ${gate.value.conflicts} 个来源冲突。`;
    return;
  }
  if (target === 3 && (gate.value.blocking || store.lastDraft.items.some(item => draftRows(item.draft).length))) {
    step.value = 2;
    parseError.value = "请先处理疑似规则，并应用或丢弃待确认的 AI 建议。";
    return;
  }
  if (target === 2 && step.value === 1) {
    sourceMode.value = Boolean(parseState.value?.coverage?.unclaimed?.length);
    sourceFilter.value = "pending";
  }
  parseError.value = null;
  step.value = target;
}

// --- AI 起草缺失细则（D-027：必须绑自己的连接）--------------------------
const connections = ref([]);
const draftConnection = ref("");
const draftBusy = ref(false);
const draftError = ref(null);

async function loadConnections() {
  try {
    connections.value = ((await api.get("/ai-connections")) || []).filter((item) => item.status === "active");
    draftConnection.value = connections.value[0]?.id || "";
  } catch (err) {
    if (!(err instanceof StaleContextError)) connections.value = [];
  }
}

async function generateMissingRules(candidates = generationCandidates.value) {
  if (!Array.isArray(candidates)) candidates = generationCandidates.value;
  draftBusy.value = true;
  draftError.value = null;
  try {
    await store.draftRules(selected.value, {
      // 只给缺规则的那些评分项：AI 补缺失部分，用户原文表述保留。
      criteria: JSON.parse(JSON.stringify(candidates)),
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
    applyNotice.value = `已应用 ${appliedKeys.size} 条建议并形成最终规则草稿。请统一确认原文规则和 AI 规则；模板尚未发布。`;
  } catch (err) {
    applyError.value = `AI 建议应用失败：${err instanceof Error ? err.message : "应用失败"}。请核对当前执行草稿后重试。`;
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

async function loadRubrics(preferredId = null) {
  try {
    rubrics.value = (await api.get("/rubrics")) || [];
    selected.value = preferredId ?? selected.value ?? rubrics.value[0]?.id ?? null;
  } catch (err) {
    if (!(err instanceof StaleContextError)) error.value = err?.message || "加载失败";
  }
}

const libraryOpen = ref(false);
const step = ref(1);
const selectedCriterion = ref(null);
const sourceMode = ref(true);
const sourceFilter = ref("pending");
const stepDetail = ref(null);
const assignedSources = computed(() => [...(sourceWorkspace.value?.previews?.word || []), ...(sourceWorkspace.value?.previews?.excel || [])].filter(u => (u.locator?.review?.claimed_by || []).includes(`${activeCriterion.value?.code}.manual`)));
// 归入的原文默认只展示第一条，其余折叠，避免来源多时页面过长；切换评分项后重新折叠。
const sourcesExpanded = ref(false);
const visibleAssignedSources = computed(() => sourcesExpanded.value ? assignedSources.value : assignedSources.value.slice(0, 1));
watch(selectedCriterion, () => { sourcesExpanded.value = false; });
function selectRuleCriterion(id) { selectedCriterion.value = id; sourceMode.value = false; }
function adjacentCriterion(delta) {
  const list = coverage.value?.criteria || [];
  const index = list.findIndex(c => c.criterion_id === selectedCriterion.value);
  if (list[index + delta]) selectRuleCriterion(list[index + delta].criterion_id);
}

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
  if (!form.name?.trim()) return "请填写标准名称。";
  if (!Number.isInteger(Number(form.total_score)) || form.criteria.some((c) => !Number.isInteger(Number(c.max_score)))) return "总分和各评分项满分必须为整数。";
  if (form.criteria.some(c => !c.name?.trim())) return "请填写每个评分项的名称。";
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
const operationBusy = computed(() => importBusy.value || reviewBusy.value || applyBusy.value || saveBusy.value || publishBusy.value || draftBusy.value || parseBusy.value);
const activeCriterion = computed(() => coverage.value?.criteria.find((c) => c.criterion_id === selectedCriterion.value));
const activeCriterionCode = computed(() => activeCriterion.value?.code);
const currentDraftItems = computed(() => store.lastDraft.items.filter((item) => item.criterion_code === activeCriterionCode.value));
const hasPendingAiDraft = computed(() => currentDraftItems.value.some((item) => draftRows(item.draft).length));
const criterionRules = computed(() => workspace.value?.rules.filter((r) => r.criterion_id === selectedCriterion.value) || []);
const structuralMessages = {
  band_criterion_invalid: '同一评分项必须恰有一条计分分档规则，且不能混用扣分规则。请逐条核对评分方向与生效方式。',
  none_rule_invalid: '不计分规则不能保留分档或计分参数，请核对并清除不适用的字段。',
  global_policy_unsupported: '全局评分政策与当前总分或配置不一致，请核对后保存。',
  weight_policy_invalid: '评分项满分或权重与总分不一致，请在第 1 步核对。',
};
const activeIssues = computed(() => [...(draft.value?.active_compilation?.blockers || []),
  ...(workspace.value?.structural_blockers || []).map(issue => ({ ...issue, message: structuralMessages[issue.code] || issue.message, criterion_code: issue.identity?.criterion_code }))]);
const incompleteScoringIssues = computed(() => activeIssues.value.filter(isScoringCompletenessIssue));
const missingRuleCodes = computed(() => new Set([
  ...blocking.value.map(c => c.code),
  ...incompleteScoringIssues.value.map(issue => issue.criterion_code).filter(Boolean),
]));
const generationCandidates = computed(() => (editForm.value?.criteria || []).filter((criterion) =>
  !store.lastDraft.items.some((item) => item.criterion_code === criterion.code && item.draft) &&
  !(criterion.deduction_rules_structured || []).length && (
    blocking.value.some((item) => item.code === criterion.code) ||
    incompleteScoringIssues.value.some((issue) => issue.criterion_code === criterion.code &&
      ['criterion_rules_missing', 'criterion_numeric_scoring_missing'].includes(issue.code)) ||
    (draft.value?.active_compilation?.blockers || []).some((issue) => issue.criterion_code === criterion.code && ['MISSING_EXECUTABLE_SCORING_MODE', 'SEVERITY_CONFIRMATION_REQUIRED', 'DEDUCTION_RULES_MISSING', 'MISSING_DEDUCTION_RULES'].includes(issue.code)))));
const reviewReady = computed(() => !dirty.value && !scoreFormError.value && !activeIssues.value.length && !blocking.value.length && canEdit.value && canSubmitReview(current.value?.status, draft.value));
const editableCriterion = computed(() => editForm.value?.criteria.find((c) => c.code === activeCriterion.value?.code));
const pendingRules = computed(() => (workspace.value?.rules || []).filter(r => r.status !== 'approved'));
const pendingMappings = computed(() => (workspace.value?.template_links || []).filter(link => link.review_status !== 'confirmed'));
// 第 2 步底部阻断项：与「保存规则，下一步」的禁用条件逐项对应，点击直接打开能处理它的位置。
function revealStepDetail() { nextTick(() => stepDetail.value?.scrollIntoView?.({ block: "start", behavior: "smooth" })); }
function openSourceFilter(filter) { sourceFilter.value = filter; sourceMode.value = true; revealStepDetail(); }
// 与筛选状态放在面板内部时一致：离开「待归类原文」后再进入，回到「待处理」。
watch(sourceMode, (on) => { if (!on) sourceFilter.value = "pending"; });
function openRuleCriterion(id) { selectRuleCriterion(id); revealStepDetail(); }
const stepTwoBlockers = computed(() => {
  const items = [];
  if (gate.value.blocking) items.push({ key: "units", tone: "danger", label: `待归类 · ${gate.value.blocking} 条疑似规则`, open: () => openSourceFilter("blocking") });
  const criteria = coverage.value?.criteria || [];
  const draftCodes = new Set(store.lastDraft.items.filter(item => draftRows(item.draft).length).map(item => item.criterion_code));
  // 按类别汇总成一个胶囊并跳到该类第一项：评分项多时底栏不被撑高，逐项状态由左侧导航标出。
  const groups = [
    { key: "missing", tone: "danger", match: c => missingRuleCodes.value.has(c.code), text: (n) => n > 1 ? ` 等 ${n} 项缺少完整计分细则` : " 缺少完整计分细则" },
    { key: "drafts", tone: "warn", match: c => !missingRuleCodes.value.has(c.code) && draftCodes.has(c.code), text: (n) => n > 1 ? ` 等 ${n} 项 AI 建议待应用或丢弃` : " · AI 建议待应用或丢弃" },
    { key: "rules", tone: "warn", match: c => pendingRules.value.some(rule => rule.criterion_id === c.criterion_id), text: (n, count) => n > 1 ? ` 等 ${n} 项共 ${count} 条规则待确认` : ` · ${count} 条规则待确认` },
  ];
  for (const group of groups) {
    const hits = criteria.filter(group.match);
    if (!hits.length) continue;
    const count = pendingRules.value.filter(rule => hits.some(c => c.criterion_id === rule.criterion_id)).length;
    items.push({ key: group.key, tone: group.tone, label: `${hits[0].code}${group.text(hits.length, count)}`, open: () => openRuleCriterion(hits[0].criterion_id) });
  }
  // 无法对应到评分项的阻断仍要显示原因，否则按钮禁用却看不到为什么。
  const known = new Set(criteria.map(c => c.code));
  const orphanRules = pendingRules.value.filter(rule => !criteria.some(c => c.criterion_id === rule.criterion_id)).length;
  if (orphanRules) items.push({ key: "orphan-rules", tone: "warn", label: `${orphanRules} 条规则待确认` });
  const orphanIssues = incompleteScoringIssues.value.filter(issue => !known.has(issue.criterion_code)).length;
  if (orphanIssues) items.push({ key: "orphan-issues", tone: "danger", label: `${orphanIssues} 个计分完整性问题` });
  const orphanDrafts = [...draftCodes].filter(code => !known.has(code)).length;
  if (orphanDrafts) items.push({ key: "orphan-drafts", tone: "warn", label: `${orphanDrafts} 项 AI 建议待应用或丢弃` });
  return items;
});
const validationReady = computed(() => compilationReady(draft.value, draft.value?.active_compilation?.id) && !activeIssues.value.length && !scoreFormError.value && !blocking.value.length);
const completedSteps = computed(() => [
  current.value?.status === "published" || Boolean(editForm.value && !dirty.value && !scoreFormError.value && !gate.value.conflicts && !pendingScoreChecks.value),
  current.value?.status === "published" || Boolean(reviewReady.value && !gate.value.blocking && !gate.value.conflicts),
  current.value?.status === "published",
]);
function goToPendingRules() {
  if (pendingRules.value.length) selectRuleCriterion(pendingRules.value[0].criterion_id);
  goStep(2);
}
async function goToIncompleteRules() {
  const code = incompleteScoringIssues.value[0]?.criterion_code;
  if (current.value?.status === 'review') await returnToDraft();
  if (current.value?.status !== 'draft') return;
  const criterion = coverage.value?.criteria.find((item) => item.code === code);
  if (criterion) selectRuleCriterion(criterion.criterion_id);
  goStep(2);
}
let loadSequence = 0;

async function refreshDetail(id = selected.value) {
  if (!id) return;
  const sequence = ++loadSequence;
  loading.value = true;
  error.value = null;
  try {
    const [nextCoverage, nextDraft, nextWorkspace, full, nextParse, nextReview, nextSources] = await Promise.all([
      api.get(`/rubrics/${id}/rule-coverage`), store.loadExecutionDraft(id),
      api.get(`/rubrics/${id}/review-workspace`), store.loadRubric(id),
      store.loadParseCoverage(id), store.loadRuleReview(id),
      api.get(`/rubrics/${id}/source-workspace`),
    ]);
    if (sequence !== loadSequence || id !== selected.value) return;
    if (nextWorkspace.compilation_id !== nextDraft.active_compilation?.id && nextWorkspace.compilation_id) {
      throw new Error("执行草稿已变化，请重新加载后核对。");
    }
    coverage.value = nextCoverage;
    sourceWorkspace.value = nextSources;
    parseState.value = nextParse;
    ruleReview.value = nextReview || { reviewed: false };
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
    return true;
  } catch (err) {
    if (sequence !== loadSequence || id !== selected.value) return;
    workspace.value = null;
    draft.value = null;
    error.value = err?.message || "加载条款失败，请重试。";
    return false;
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
  if (target) selectRuleCriterion(target.criterion_id);
  goStep(2);
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
  try { await api.post(`/rubrics/${selected.value}/return-to-draft`); if (await refreshDetail()) step.value = 2; }
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
  if (!canEdit.value || operationBusy.value || current.value?.status !== "draft") return false;
  const invalidField = document.querySelector('.atomic-editor :invalid');
  if (invalidField instanceof HTMLInputElement || invalidField instanceof HTMLTextAreaElement) {
    invalidField.reportValidity();
    reviewError.value = "请先修正原子规则编辑中的无效输入。";
    return false;
  }
  reviewError.value = null;
  if (scoreFormError.value) { reviewError.value = scoreFormError.value; step.value = 1; return false; }
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
    if (!(await refreshDetail())) return false;
    reviewNotice.value = step.value === 1 ? "评分项已保存，请在下一步核对评分规则。" : activeIssues.value.length ? `已保存，仍有 ${activeIssues.value.length} 个校验阻断，请逐项处理。` : "已保存并取得当前校验结果；条款确认与模板发布分别进行。";
    return true;
  } catch (err) { reviewError.value = err?.message || "保存失败，修改已保留。"; return false; }
  finally { saveBusy.value = false; }
}
watch(selected, async (id) => {
  classificationProgress.value = null;
  workspace.value = null; coverage.value = null; draft.value = null;
  selectedCriterion.value = null; sourceMode.value = true; sourceFilter.value = "pending"; chosenVisibility.value = null;
  excluded.value = new Set(); editForm.value = null; reviewError.value = null; reviewNotice.value = null;
  store.lastDraft = { items: [] }; applyNotice.value = null; applyError.value = null;
  parseState.value = null; structureEstimate.value = null; ruleEstimate.value = null; parseError.value = null;
  sourceWorkspace.value = null; sourcePreview.value = null;
  rubricReupload.value = { preview: null, rulesFile: null, templateFile: null };
  step.value = nextEntryStep.value;
  nextEntryStep.value = 1;
  await refreshDetail(id);
  if (gate.value.blocked || (parseState.value?.triggers || []).length) step.value = 1;
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
      <p class="page-eyebrow">标准与输出</p>
      <div class="heading-row">
        <div><h1 class="page-title">{{ importing ? store.activeImportSession?.name || importForm.name || '导入评分标准' : editForm?.name || current?.name || '评分标准' }}</h1>
          <p class="page-sub" v-if="importing"><span class="chip chip-warn">待确认</span> 上传文件并核对评分项，确认后进入评分规则。</p>
          <p v-else class="page-sub"><span v-if="current" class="chip chip-warn">{{ draft?.active_compilation?.version?.version || current.version }} · {{ { draft: '草稿', review: '审核中', published: '已发布' }[current.status] || current.status }}</span>
            {{ dirty ? '有未保存的修改 · 保存后重新校验' : step === 1 ? '核对评分标准文档与评分项，再配置评分规则。' : '条款确认即时保存；发布后版本不可变。' }}</p>
        </div>
        <div class="head-actions">
          <button v-if="!importing" class="btn" :disabled="operationBusy" @click="libraryOpen = !libraryOpen">模板库</button>
          <button v-if="!importing" class="btn btn-primary" :disabled="operationBusy || !canEdit" @click="beginImport">新建评分标准</button>
          <button v-if="!importing && current?.status === 'draft' && step !== 1" class="btn" :disabled="operationBusy || loading || !canEdit" @click="saveAndValidate">{{ saveBusy ? '保存中…' : '保存并重新校验' }}</button>
          <button v-if="!importing && current?.status === 'draft' && step === 2" class="btn btn-primary" :disabled="operationBusy || loading" @click="goStep(3)">前往校验与发布</button>
        </div>
      </div>
    </header>
    <p v-if="error" class="notice notice-danger" role="alert">{{ error }} <button class="btn btn-sm" @click="refreshDetail()">重新加载</button></p>
    <p v-if="reviewError && !importing" class="notice notice-danger" role="alert">{{ reviewError }}</p>
    <p v-if="reviewNotice && !importing" class="notice" role="status">{{ reviewNotice }}</p>
    <p v-if="applyError" class="notice notice-danger" role="alert">{{ applyError }}</p>
    <p v-if="applyNotice" class="notice" role="status">{{ applyNotice }}</p>
    <p v-if="parseError" class="notice notice-danger" role="alert">{{ parseError }}</p>
    <nav v-if="importing" class="card steps" aria-label="评分标准编辑步骤">
      <button v-for="(label, index) in ['基本信息与评分项', '评分规则', '校验与发布']" :key="label" class="step" :class="{ active: index === 0 }" :aria-current="index === 0 ? 'step' : undefined" :disabled="index !== 0"><span class="step-number">{{ index + 1 }}</span>{{ label }}</button>
    </nav>
    <RubricImportWorkspace v-if="store.activeImportSession" :session="store.activeImportSession"
      :busy="importBusy" :rules-file="rulesFile" :template-file="templateFile"
      :source-preview="sourcePreview" :reupload-preview="reuploadPreview"
      @pick-rules="pickRules" @pick-template="pickTemplate" @update-session="updateImportSession"
      @update-criterion="updateImportCriterion" @add-criterion="addImportCriterion"
      @delete-criterion="deleteImportCriterion" @confirm="confirmImportSession"
      @preview-source="previewImportSource" @close-source-preview="sourcePreview = null"
      @resolve-conflict="resolveImportConflict" @confirm-reupload="confirmImportReupload"
      @cancel-reupload="cancelImportReupload" @cancel="cancelImportSession" />
    <p v-if="store.activeImportSession && importError" class="notice notice-danger" role="alert">{{ importError }}</p>
    <section v-if="importOpen && !store.activeImportSession" class="import-panel">
      <RubricImportWorkspace :session="initialImport" initial :busy="importBusy"
        :rules-file="rulesFile" :template-file="templateFile" @pick-rules="pickRules" @pick-template="pickTemplate"
        @update-session="updateFirstStep" @confirm="submitImport()" @cancel="importOpen = false" />
      <div v-if="importStructure.available" class="import-structure" data-test="import-structure">
        <p class="faint">表格结构没有被自动识别。可以让 AI 识别表头与列用途（只识别结构、不改写原文），确认后再导入。</p>
        <label class="field"><span class="field-label">用哪个 AI 连接</span>
          <select v-model="draftConnection" class="select"><option value="">请选择</option><option v-for="item in connections" :key="item.id" :value="item.id">{{ item.name }} · {{ item.model_name }}</option></select>
        </label>
        <button v-if="!importStructure.estimate" class="btn" :disabled="importBusy" @click="previewImportStructure(true)">估算识别规模</button>
        <template v-else-if="!importStructure.result">
          <p class="notice">将发送约 {{ importStructure.estimate.chars }} 字符，调用 {{ importStructure.estimate.calls }} 次模型。</p>
          <button class="btn btn-primary" :disabled="importBusy || !draftConnection" @click="previewImportStructure(false)">确认调用 AI 识别结构</button>
        </template>
        <template v-else>
          <p>按识别出的结构，将导入 {{ importStructure.result.preview.length }} 个评分项：</p>
          <ul><li v-for="item in importStructure.result.preview" :key="item.row_number">第 {{ item.row_number }} 行 · {{ item.name }}（{{ item.max_score }} 分）</li></ul>
          <button class="btn btn-primary" :disabled="importBusy" @click="submitImport(importStructure.result.override)">按此结构导入</button>
        </template>
      </div>

      <p v-if="importError" class="notice notice-danger" role="alert">{{ importError }}</p>
    </section>

    <!-- 导入结果如实展示：warnings 是「你的 Excel 里哪几条没被识别」的唯一出口，
         吞掉它用户会以为全都导进去了，直到评分时才发现某项没有判据。 -->
    <details v-if="store.lastImport.rubricId === selected && !importing && step === 2 && store.lastImport.warnings.length" class="card card-pad">
      <summary>查看导入提示</summary>
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
    </details>


    <section v-if="!importing && (libraryOpen || !current)" class="card library-menu">
      <div class="card-head"><h2 class="card-title">模板库</h2></div>
      <button v-for="rubric in rubrics" :key="rubric.id" class="lib-item" :class="{ active: rubric.id === selected }" :disabled="operationBusy" @click="selectRubric(rubric.id)">
        <strong>{{ rubric.name }}</strong> <span class="chip">{{ rubric.version }} · {{ {draft:'草稿',review:'审核中',published:'已发布'}[rubric.status] || rubric.status }}</span> <span class="faint">{{ visibilityLabel(rubric.visibility) }}</span>
      </button>
      <p v-if="!rubrics.length" class="card-pad faint">还没有评分标准。请上传 Word 文档或 Excel 评分表开始。</p>
    </section>
    <template v-if="current && !importing">
      <nav class="card steps" aria-label="评分标准编辑步骤">
        <button v-for="(label, index) in ['基本信息与评分项', '评分规则', '校验与发布']" :key="label" class="step" :class="{ active: step === index + 1, complete: completedSteps[index] }" :disabled="operationBusy || loading" :aria-label="`${index + 1} ${label}`" :aria-current="step === index + 1 ? 'step' : undefined" @click="goStep(index + 1)"><span class="step-number" aria-hidden="true">{{ completedSteps[index] ? '✓' : index + 1 }}</span>{{ label }}</button>
      </nav>
      <div v-if="step === 3 && (activeIssues.length || draft?.ambiguity)" class="notice notice-danger blockers" role="alert">
        <strong>存在 {{ activeIssues.length }} 个校验阻断，处理后才能提交审核。</strong>
        <p v-if="draft?.ambiguity">当前存在多个执行草稿，请先恢复为唯一活动版本。</p>
        <div v-for="(issue, index) in activeIssues" :key="index" class="issue-row">
          <span>{{ issue.criterion_code }} {{ issue.message || issue.code || issue }}</span>
          <button v-if="issue.criterion_code" class="btn btn-sm" @click="locateIssue(issue)">去修改 {{ issue.criterion_code }} →</button>
        </div>
      </div>
      <div v-if="step === 2 && blocking.length" class="notice notice-warn">
        <strong>存在 {{ blocking.length }} 个缺少规则的评分项</strong>
        <p>评分到该项时没有判据可用，请补齐细则并重新校验。</p>
        <button v-for="item in blocking" :key="item.criterion_id" class="btn btn-sm" @click="locateIssue({ criterion_code: item.code })">去修改 {{ item.code }} →</button>
      </div>
      <p v-if="scoreFormError && step !== 1" class="notice notice-warn">{{ scoreFormError }}</p>
      <p v-if="loading" class="notice" role="status">正在加载条款与校验结果…</p>
      <template v-if="step === 1 && editForm">
        <RubricImportWorkspace :key="selected" :session="stepOneSession" persisted recognition :parse-state="parseState" :readonly="!canEdit || current.status !== 'draft'"
          :busy="operationBusy || importBusy" :source-preview="sourcePreview" :reupload-preview="rubricReupload.preview"
          :blocking-message="scoreFormError || (gate.blocked ? `请先核对 ${gate.conflicts} 个来源冲突。` : '')"
          @update-session="updateFirstStep" @update-criterion="updateExistingCriterion" @confirm="confirmFirstStep" @save="saveAndValidate" @score-check="pendingScoreChecks = $event"
          @preview-source="previewExistingSource" @close-source-preview="sourcePreview = null"
          @pick-rules="previewRubricReupload('rules', $event)" @pick-template="previewRubricReupload('template', $event)"
          @confirm-reupload="confirmRubricReupload"
          @cancel-reupload="rubricReupload = { preview: null, rulesFile: null, templateFile: null }">
          <template #table-analysis>
            <TableRecognitionPanel :state="parseState" :previews="sourceWorkspace?.previews?.excel || []" :busy="operationBusy" :editable="editable" :connection="connections.find(c => c.id === draftConnection)" :estimate="structureEstimate" @suggest-structure="suggestStructure" />
          </template>
          <template #analysis>
            <StructureSuggestionPanel :suggestion="parseState?.structure_suggestions" :busy="operationBusy"
              :editable="editable" @merge="mergeStructure" @undo="undoStructure" />
          </template>
          <template #source-analysis>
            <ParseCoveragePanel :state="parseState" :criteria="coverage?.criteria || []" :busy="operationBusy" :editable="editable" conflicts-only @resolve="resolveUnits" />
          </template>
          <template #issues><p v-if="importError" class="notice notice-danger" role="alert">{{ importError }}</p></template>
        </RubricImportWorkspace>
      </template>
      <div v-else class="editor-layout" :class="{ 'release-layout': step === 3 }">
        <aside v-if="step === 2" class="card criteria-nav">
          <div class="card-head"><h2 class="card-title">规则来源与评分项</h2></div>
          <button class="lib-item source-nav" :class="{ active: sourceMode }" :disabled="operationBusy" @click="sourceMode = true"><strong>待归类原文 {{ parseState?.coverage?.unclaimed?.length || 0 }}</strong><p :class="gate.blocking ? 'danger' : 'faint'">{{ gate.blocking }} 条疑似规则必须处理</p></button>
          <button v-for="item in coverage?.criteria || []" :key="item.criterion_id" class="lib-item" :class="{ active: !sourceMode && item.criterion_id === selectedCriterion, missing: missingRuleCodes.has(item.code) }" :disabled="operationBusy" @click="selectRuleCriterion(item.criterion_id)">
            <div class="criterion-title"><span class="faint mono">{{ item.code }}</span><strong>{{ item.name }}</strong><span class="score">{{ item.max_score }} 分</span></div>
            <p :class="missingRuleCodes.has(item.code) ? 'danger' : item.status === 'pending_review' ? 'warn' : 'faint'">{{ missingRuleCodes.has(item.code) ? '缺少完整计分细则' : `${item.rule_count} 条规则 · ${STATUS[item.status]?.label || item.status}` }}</p><p class="faint">原文 {{ item.from_source_count }} · AI {{ item.from_ai_count }}</p>
          </button>
        </aside>
        <div ref="stepDetail" class="detail">
          <template v-if="step === 2">
            <template v-if="sourceMode">
              <section class="card card-pad"><h2 class="card-title">待归类原文</h2><p class="card-note">把文档要求归入已有评分项，作为规则来源；归类不会新增评分项或自动产生扣分。</p></section>
              <SourceReviewPanel v-model:filter="sourceFilter" :progress="classificationProgress" :state="parseState" :previews="[...(sourceWorkspace?.previews?.word || []), ...(sourceWorkspace?.previews?.excel || [])]" :criteria="coverage?.criteria || []" :busy="operationBusy" :editable="editable" :connection="connections.find(c => c.id === draftConnection)" @resolve="resolveUnits" @classify="classifyUnits" @accept-suggestions="acceptSuggestions" />
            </template>
            <template v-else>
            <section v-if="activeCriterion" class="card card-pad criterion-overview">
              <div><p class="faint mono">{{ activeCriterion.code }} · 满分 {{ activeCriterion.max_score }} 分</p><h2>{{ activeCriterion.name }}</h2><p class="faint">逐条核对规则及其来源，确认后才能用于评分。</p></div>
              <div class="actions"><button class="btn" :disabled="operationBusy || selectedCriterion === coverage?.criteria[0]?.criterion_id" @click="adjacentCriterion(-1)">上一项</button><button class="btn" :disabled="operationBusy || selectedCriterion === coverage?.criteria.at(-1)?.criterion_id" @click="adjacentCriterion(1)">下一项</button></div>
            </section>
            <section class="card rule-sources" data-test="rule-sources">
              <div class="card-head"><h2 class="card-title">规则来源</h2><span class="faint">AI 根据评分说明、原文细则及归入的内容起草</span></div>
              <div class="rule-source-columns"><div><h3>评分说明</h3><p>{{ editableCriterion?.description || '未提供评分说明' }}</p><p v-for="(text,i) in editableCriterion?.deduction_rules || []" :key="i">{{ text }}</p><p class="faint">修改评分说明请返回第 1 步。</p></div><div><h3>归入的原文要求 · {{ assignedSources.length }} 个单元</h3><button class="btn btn-sm" :disabled="operationBusy" @click="sourceMode = true">从待归类添加</button><article v-for="unit in visibleAssignedSources" :key="unit.unit_id"><div class="source-row"><span class="mono faint">{{ unit.unit_id }}</span><button v-if="unit.locator?.review?.claimed_by?.includes(`${activeCriterion.code}.manual`)" class="btn btn-sm" :disabled="operationBusy || !editable" @click="resolveUnits({unitIds:[unit.unit_id], action:'restore', reason:'用户移出规则来源，重新归类'})">移出</button></div><p>{{ unit.text }}</p></article><button v-if="assignedSources.length > 1" class="btn btn-sm sources-toggle" type="button" data-test="toggle-assigned-sources" :aria-expanded="sourcesExpanded" @click="sourcesExpanded = !sourcesExpanded">{{ sourcesExpanded ? '收起' : `展开其余 ${assignedSources.length - 1} 个单元` }}</button><p v-if="!assignedSources.length" class="faint">还没有归入原文。</p><p class="faint">移出只撤销原文归类；已生成细则请另行核对，未应用的 AI 建议将清除。</p></div></div>
            </section>
            <section class="card card-pad coverage-panel"><h2 class="card-title">扣分细则</h2><p class="faint">AI 连接：{{ connections.find(c => c.id === draftConnection)?.name || '未启用，请到账户与连接配置' }}。AI 建议须人工核对后应用与确认。</p><div class="actions"><button class="btn" :disabled="operationBusy || !draftConnection || !editable || !editableCriterion || hasPendingAiDraft" @click="generateMissingRules([editableCriterion])">{{ draftBusy ? '起草中…' : 'AI 根据规则来源起草' }}</button><button v-if="generationCandidates.length" class="btn" :disabled="operationBusy || !draftConnection || !editable" @click="generateMissingRules()">生成全部缺失细则（{{ generationCandidates.length }}）</button></div><p v-if="draftError" class="notice notice-danger" role="alert">{{ draftError }}</p></section>
            <AiRuleDraftPanel :items="currentDraftItems" :busy="operationBusy || !editable" @apply="applyDraft" @discard="discardDraft" />
            <RuleReviewPanel v-if="activeCriterion && workspace" :criterion="activeCriterion" :rules="criterionRules" :excluded="excluded" :busy="operationBusy" :editable="editable"
              :defer-confirmation="hasPendingAiDraft" @confirm="confirmRules([$event])" @confirm-all="confirmRules" @exclude="toggleExcluded" />
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
            <footer class="card card-pad rules-footer"><div class="footer-blockers" data-test="step-two-blockers"><strong>{{ stepTwoBlockers.length ? '进入校验与发布前需处理' : '评分规则已处理完成，可以进入校验与发布' }}</strong><div v-if="stepTwoBlockers.length" class="blocker-chips"><template v-for="item in stepTwoBlockers" :key="item.key"><button v-if="item.open" type="button" class="blocker-chip" :class="item.tone" :disabled="operationBusy" @click="item.open()">{{ item.label }}</button><span v-else class="blocker-chip" :class="item.tone">{{ item.label }}</span></template></div></div><div class="actions"><button class="btn" :disabled="operationBusy" @click="goStep(1)">上一步</button><button class="btn" :disabled="operationBusy || !canEdit" @click="saveAndValidate">保存草稿</button><button class="btn btn-primary" :disabled="operationBusy || !!gate.blocking || !!blocking.length || !!pendingRules.length || !!incompleteScoringIssues.length || store.lastDraft.items.some(item => draftRows(item.draft).length)" @click="goStep(3)">保存规则，下一步</button></div></footer>
          </template>
          <section v-if="step === 3" class="card card-pad" data-test="release-panel">
            <h2 class="card-title">{{ current.status === 'draft' ? '发布前检查' : current.status === 'review' ? '确认并发布' : '评分标准已发布' }}</h2>
            <p class="card-note">{{ current.status === 'draft' ? '核对条款与模板映射，通过校验后提交模板审核。提交审核不会自动发布。' : current.status === 'review' ? '模板已提交审核。请核对本次发布版本与分享范围，确认后发布。' : '此版本与分享范围已冻结；后续修改请复制为新版本。' }}</p>
            <div v-if="current.status !== 'published' && incompleteScoringIssues.length" class="notice notice-danger" role="alert" data-test="scoring-completeness-blockers">
              <p>评分细则不完整，暂不能发布。每个评分项须有具体细则：扣分制须有扣分标准，等级制须有档位分值和判定说明。</p>
              <ul><li v-for="issue in incompleteScoringIssues" :key="`${issue.code}:${issue.field_path}`">{{ issue.criterion_code }} · {{ issue.message }}</li></ul>
              <p>可返回第 2 步使用 AI 生成建议或手动补全；应用后统一确认，重新校验通过才能发布。</p>
              <button class="btn" :disabled="operationBusy || !canEdit" @click="goToIncompleteRules">{{ current.status === 'review' ? '退回草稿并补全细则' : '去补全细则（可使用 AI）' }}</button>
            </div>
            <template v-if="current.status === 'draft'">
              <div class="issue-row"><span>条款核对</span><span class="chip" :class="pendingRules.length ? 'chip-warn' : 'chip-ok'">{{ pendingRules.length ? `${pendingRules.length} 条待确认` : '全部已确认' }}</span></div>
              <div class="issue-row"><span>模板映射</span><span class="chip" :class="pendingMappings.length ? 'chip-warn' : 'chip-ok'">{{ pendingMappings.length ? `${pendingMappings.length} 项待核对` : workspace?.template_links?.length ? '全部已确认' : '无需核对' }}</span></div>
              <div class="issue-row"><span>规则校验</span><span class="chip" :class="validationReady && !dirty ? 'chip-ok' : 'chip-warn'">{{ dirty ? '修改尚未保存' : validationReady ? '校验通过' : '尚未通过校验' }}</span></div>
              <p v-if="!canEdit" class="notice notice-warn">当前账号没有审核与发布权限，请联系管理员处理。</p>
              <p v-else-if="dirty" class="notice notice-warn">请先保存修改并重新校验，再核对更新后的条款。</p>
              <p v-else-if="reviewReady" class="notice">条款、模板映射与校验均已完成。下一步：提交模板审核。</p>
              <p v-else class="notice notice-warn">请完成上方未通过的检查项。条款确认后会自动更新检查结果。</p>
            </template>
            <template v-else>
            <label class="field"><span class="field-label">发布哪一份执行草稿</span><select v-model="chosenCompilation" class="select" :disabled="operationBusy || current.status === 'published'"><option :value="null">请选择</option><option v-for="item in draft?.compilations || []" :key="item.id" :value="item.id" :disabled="item.id !== draft?.active_compilation?.id">{{ item.status === 'validated' ? '校验通过' : item.status === 'blocked' ? '存在阻断' : item.status }} · {{ item.blocker_count }} 个阻断 · {{ item.created_at }}</option></select></label>
            <label class="field"><span class="field-label">分享给谁</span><select v-model="chosenVisibility" class="select" :disabled="operationBusy || current.status === 'published'"><option :value="null">保持当前（{{ visibilityLabel(current.visibility) }}）</option><option value="organization">本组织</option><option value="system" :disabled="!session.isPlatformAdmin">所有人{{ session.isPlatformAdmin ? '' : '（需平台管理员）' }}</option></select></label>
            </template>
            <div v-if="draft?.active_compilation?.template_links?.length" class="template-links">
              <h3>模板映射核对</h3><p class="faint">规则确认与模板映射确认分别记录，请核对模板来源后操作。</p>
              <div v-for="link in workspace?.template_links || []" :key="link.id" class="issue-row"><div><span>{{ link.rule_code }} · {{ link.review_status === 'confirmed' ? '已确认' : '待核对' }}</span><p>{{ link.section_path?.join(' / ') }} · {{ link.text }}</p><p class="faint">{{ link.rationale }}</p></div><button v-if="link.review_status === 'pending'" class="btn" :disabled="operationBusy || !editable" @click="confirmLink(link)">确认模板映射</button></div>
            </div>

            <p v-if="!canPublish && current.status === 'review'" class="notice notice-warn">当前版本尚不满足发布条件，请核对条款、映射与校验阻断。</p>
            <div class="head-actions">
              <template v-if="current.status === 'draft'">
                <button v-if="dirty" class="btn btn-primary" :disabled="operationBusy || !canEdit" @click="saveAndValidate">{{ saveBusy ? '保存中…' : '保存并重新校验' }}</button>
                <button v-else-if="pendingRules.length" class="btn" :disabled="operationBusy" @click="goToPendingRules">去核对条款（{{ pendingRules.length }}）</button>
                <button v-else-if="!validationReady" class="btn" :disabled="operationBusy" @click="scoreFormError ? step = 1 : goStep(2)">查看并处理校验问题</button>
                <button class="btn" :class="reviewReady ? 'btn-primary' : ''" :disabled="operationBusy || !reviewReady" @click="submitReview">{{ reviewBusy ? '提交中…' : '提交模板审核' }}</button>
              </template>
              <button v-if="current.status === 'review'" class="btn" :disabled="operationBusy || !canEdit" @click="returnToDraft">退回草稿</button>
              <button v-if="current.status === 'review'" class="btn btn-primary" :disabled="operationBusy || !canPublish || dirty" @click="submitPublish">{{ publishBusy ? '发布中…' : '发布' }}</button>
              <button v-if="current.status === 'published'" class="btn" :disabled="operationBusy || !canEdit" @click="cloneRubric(current)">复制为新版本</button>
            </div>
            <p v-if="publishError" class="notice notice-danger" role="alert">{{ publishError }}</p>
          </section>
          <RuleAuditPanel v-if="step === 3 && current.status !== 'published'" :review="ruleReview" :estimate="ruleEstimate"
            :busy="operationBusy" :editable="canEdit" :connection-id="draftConnection"
            @estimate="estimateReview" @run="runReview" @dismiss="dismissFinding" />
        </div>
      </div>
    </template>
  </div>
</template>
<style scoped>
.heading-row, .head-actions, .criterion-title, .issue-row { display: flex; align-items: center; gap: 12px; }
.heading-row, .issue-row { justify-content: space-between; }
.head-actions { flex-wrap: wrap; }
/* 间距与字号对齐 Claude Design「评分标准」原型：步骤条 5px 容器 + 9px 14px 项、正文 13.5px、说明 12–12.5px。 */
.steps { display: flex; padding: 5px; margin: 16px 0; }
.step { flex: 1; border: 0; background: transparent; padding: 9px 14px; text-align: left; display: flex; align-items: center; gap: 9px; color: #777d78; border-radius: 7px; cursor: pointer; font-size: 13.5px; }
.step.active { background: #f0f4f2; color: #185e52; font-weight: 600; }
.step-number { border: 1.5px solid currentColor; border-radius: 50%; width: 20px; height: 20px; display: inline-flex; align-items: center; justify-content: center; flex: none; font-family: var(--font-mono); font-size: 11px; }
.criterion-overview,.rules-footer{display:flex;justify-content:space-between;gap:20px;align-items:center}.rule-source-columns{display:grid;grid-template-columns:1fr 1fr}.rule-source-columns>div{padding:16px 20px;min-width:0}.rule-source-columns>div+div{border-left:1px solid #e7e8e3}.rule-source-columns h3{font-size:13.5px;margin:0 0 10px}.rule-source-columns p{line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere}.rule-source-columns article{border:1px solid #e4e7e1;border-radius:9px;margin-top:10px;padding:10px 14px}.rule-source-columns article p{margin:6px 0 0}.criterion-overview h2{font-size:18px;margin:4px 0}.criterion-overview p{margin:0}.detail>.card.card-pad,.rules-footer{padding:16px 20px}.sources-toggle{margin-top:10px}.source-row{display:flex;justify-content:space-between;gap:10px}.rules-footer{position:sticky;bottom:0;z-index:4;flex-wrap:wrap}.footer-blockers{flex:1 1 320px;min-width:0}.blocker-chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.blocker-chip{display:inline-flex;align-items:center;gap:7px;min-height:30px;max-width:100%;padding:4px 12px;border-radius:20px;border:1px solid #f0e6d2;background:#fdfaf3;color:#8a5a12;font:inherit;font-size:12.5px;text-align:left;overflow-wrap:anywhere}.blocker-chip::before{content:"";flex:none;width:6px;height:6px;border-radius:50%;background:currentColor}.blocker-chip.danger{border-color:#f0dcd8;background:#fdf7f6;color:#a3372b}button.blocker-chip{cursor:pointer}button.blocker-chip:disabled{cursor:not-allowed;opacity:.6}.actions{display:flex;gap:10px;flex-wrap:wrap}@media(max-width:1279px){.editor-layout:not(.release-layout){grid-template-columns:minmax(0,1fr)}.criteria-nav{max-height:260px;overflow:auto}.rules-footer{position:static;flex-wrap:wrap}}@media(max-width:760px){.rule-source-columns{grid-template-columns:1fr}.criterion-overview{flex-wrap:wrap}.rule-source-columns>div+div{border-left:0;border-top:1px solid #e7e8e3}}
.editor-layout { display: grid; grid-template-columns: 252px minmax(0, 1fr); gap: 18px; align-items: start; }
.editor-layout.release-layout { grid-template-columns: minmax(0, 1fr); }
.step.complete .step-number { background: #175c50; border-color: #175c50; color: white; }
.step:disabled { cursor: default; opacity: .65; }
.head-actions { gap: 8px; }
.criteria-nav { overflow: hidden; }
.detail > .card { margin-bottom: 0; }
.detail { min-width: 0; display: flex; flex-direction: column; gap: 16px; }
.lib-item { display: block; width: 100%; border: 0; border-bottom: 1px solid #eeefeb; border-left: 2px solid transparent; background: white; padding: 11px 16px; text-align: left; cursor: pointer; }
.lib-item.active { border-left-color: #185e52; background: #f0f4f2; }
.lib-item.missing { border-left-color: #b83a2d; }
.lib-item p { margin: 3px 0 0; font-size: 12px; }
.criterion-title { gap: 8px; flex-wrap: wrap; font-size: 13.5px; }
.criterion-title .mono { font-size: 12px; }
.score { margin-left: auto; color: #929791; font-size: 12px; }
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
